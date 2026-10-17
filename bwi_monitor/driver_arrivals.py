"""Local driver arrival records, independent of TMS timestamps and alert rules."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
SITE_CODE = "BWI.H"
SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_arrivals (
    task_no TEXT NOT NULL, site_code TEXT NOT NULL,
    source_segment_id INTEGER NOT NULL, actual_arrival_utc TEXT,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (task_no, site_code)
);
CREATE TABLE IF NOT EXISTS driver_arrival_events (
    id INTEGER PRIMARY KEY, task_no TEXT NOT NULL, site_code TEXT NOT NULL,
    source_segment_id INTEGER NOT NULL, action TEXT NOT NULL,
    version INTEGER NOT NULL, previous_actual_arrival_utc TEXT,
    actual_arrival_utc TEXT, at TEXT NOT NULL,
    UNIQUE (task_no, site_code, version)
);
CREATE TABLE IF NOT EXISTS driver_appointments (
    task_no TEXT PRIMARY KEY, scheduled_arrival_at TEXT NOT NULL,
    source TEXT NOT NULL, source_record_id TEXT, imported_at TEXT NOT NULL
);
"""


class ValidationError(ValueError):
    pass


class NotFoundError(ValidationError):
    pass


class ConflictError(ValidationError):
    pass


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_actual_arrival(value, now):
    """Accept an unambiguous Eastern wall-clock minute, never a browser-local time."""
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        raise ValidationError("请填写完整到场日期和时间，格式为 YYYY-MM-DDTHH:MM（美国东部时间）。")
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
        candidates = set()
        for fold in (0, 1):
            candidate = naive.replace(tzinfo=EASTERN, fold=fold).astimezone(timezone.utc)
            if candidate.astimezone(EASTERN).replace(tzinfo=None) == naive:
                candidates.add(candidate)
    except (ValueError, OverflowError):
        raise ValidationError("到场日期或时间无效。") from None
    if not candidates:
        raise ValidationError("该东部时间因夏令时跳转不存在，请核对到场时间。")
    if len(candidates) != 1:
        raise ValidationError("该东部时间在夏令时回拨时重复出现，无法唯一确定，请核对记录。")
    actual = candidates.pop()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    if actual > now.astimezone(timezone.utc):
        raise ValidationError("实际到场时间不能晚于当前时间。")
    return _iso(actual)


def eligible(seg):
    from .driver_scores import in_scope
    return in_scope(seg)


def _aware(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None and dt.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _et(dt):
    return dt.astimezone(EASTERN).strftime("%Y-%m-%dT%H:%M") if dt else None


def view(seg, record=None, appointment=None):
    """Display values and the existing 100/55 lateness item, never a total driver score."""
    record, appointment = dict(record or {}), dict(appointment or {})
    actual = _aware(record.get("actual_arrival_utc"))
    scheduled = _aware(appointment.get("scheduled_arrival_at"))
    status, score, late_minutes = "unrecorded", None, None
    if actual:
        if scheduled is None:
            status = "pending_schedule"
        else:
            seconds_late = (actual - scheduled).total_seconds()
            late_minutes = max(0, int(seconds_late // 60))
            status, score = ("late", 55) if seconds_late > 0 else ("on_time", 100)
    return {
        "task_no": (seg or {}).get("taskNo"), "site_code": SITE_CODE, "eligible": eligible(seg),
        "source_segment_id": record.get("source_segment_id"),
        "actual_arrival_et": _et(actual), "actual_arrival_utc": record.get("actual_arrival_utc"),
        "version": record.get("version", 0), "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"), "updated_at_et": _et(_aware(record.get("updated_at"))),
        "scheduled_arrival_et": _et(scheduled),
        "score": score, "late_minutes": late_minutes, "status": status,
        "schedule_source": appointment.get("source") if scheduled else None,
        "schedule_source_record_id": appointment.get("source_record_id") if scheduled else None,
    }


class DriverArrivals:
    """Uses its Store's connection; HTTP callers hold Handler.lock for the whole operation."""
    def __init__(self, db):
        self.db = db

    def record(self, task_no):
        return self.db.execute("SELECT * FROM driver_arrivals WHERE task_no=? AND site_code=?",
                               (task_no, SITE_CODE)).fetchone()

    def appointment(self, task_no):
        return self.db.execute("SELECT * FROM driver_appointments WHERE task_no=?", (task_no,)).fetchone()

    def view(self, seg):
        if not eligible(seg):
            return view(seg)
        return view(seg, self.record(seg["taskNo"]), self.appointment(seg["taskNo"]))

    def snapshot(self):
        """Read each local table once when decorating a dashboard snapshot."""
        records = {r["task_no"]: dict(r) for r in self.db.execute(
            "SELECT * FROM driver_arrivals WHERE site_code=?", (SITE_CODE,))}
        appointments = {r["task_no"]: dict(r) for r in self.db.execute("SELECT * FROM driver_appointments")}
        return records, appointments

    def save(self, segment_id, actual_arrival_et, version, now):
        if type(version) is not int or not 0 <= version < 9223372036854775807:
            raise ValidationError("缺少有效记录版本，请重新读取记录后再保存。")
        actual = parse_actual_arrival(actual_arrival_et, now)
        if self.db.in_transaction:
            raise RuntimeError("driver arrival save must own its transaction")
        with self.db:
            # Serialize independent connections before checking the optimistic version.
            self.db.execute("BEGIN IMMEDIATE")
            source = self.db.execute("SELECT data,validity FROM segments WHERE id=?", (segment_id,)).fetchone()
            if source is None:
                raise NotFoundError("任务航段不存在，请刷新后重试。")
            seg = json.loads(source["data"])
            seg["_validity"] = source["validity"]
            from .driver_scores import in_task_scope
            siblings = []
            for row in self.db.execute("SELECT data,validity FROM segments WHERE task_no=?", (seg.get("taskNo"),)):
                sibling = json.loads(row["data"])
                sibling["_validity"] = row["validity"]
                siblings.append(sibling)
            profile = self.db.execute("SELECT * FROM driver_task_profiles WHERE task_no=?", (seg.get("taskNo"),)).fetchone()
            if not in_task_scope(seg, siblings, dict(profile) if profile else None):
                raise ValidationError("仅登记 BWI 始发至 ORF、RIC、DCA、IAD 的有效支线任务。")
            task_no = seg["taskNo"]
            old = self.record(task_no)
            if version != (old["version"] if old else 0):
                raise ConflictError("记录已被其他操作修改，请重新读取最新记录后再保存。")
            at, next_version = _iso(now), version + 1
            self.db.execute(
                "INSERT INTO driver_arrivals(task_no,site_code,source_segment_id,actual_arrival_utc,version,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_no,site_code) DO UPDATE SET "
                "source_segment_id=excluded.source_segment_id,actual_arrival_utc=excluded.actual_arrival_utc,"
                "version=excluded.version,updated_at=excluded.updated_at",
                (task_no, SITE_CODE, segment_id, actual, next_version, at, at))
            self.db.execute(
                "INSERT INTO driver_arrival_events(task_no,site_code,source_segment_id,action,version,"
                "previous_actual_arrival_utc,actual_arrival_utc,at) VALUES(?,?,?,?,?,?,?,?)",
                (task_no, SITE_CODE, segment_id, "clear" if actual is None else "update" if old else "create",
                 next_version, old["actual_arrival_utc"] if old else None, actual, at))
            result = self.view(seg)
        return result
