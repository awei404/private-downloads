"""Task-based driver scores for confirmed BWI-origin branch runs; no TMS writes or notifications."""
import json
import math
from datetime import datetime, timedelta, timezone

from . import departures, driver_arrivals, rules
from .driver_arrivals import ConflictError, NotFoundError, ValidationError

DESTINATIONS = frozenset(("ORF01", "RIC01", "DCA01", "IAD01"))
RETURN_DEDUCTIONS = {"not_required": 0, "completed": 0, "partial": 10, "failed": 25, "pending": None}
INACTIVE_PROFILE_SOURCES = frozenset(("TMS任务已取消", "TMS任务范围不符"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_task_profiles (
    task_no TEXT PRIMARY KEY, driver_key TEXT, driver_name TEXT NOT NULL DEFAULT '',
    driver_phone TEXT NOT NULL DEFAULT '', supplier_name TEXT NOT NULL DEFAULT '',
    vehicle_plate TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS driver_task_profiles_driver ON driver_task_profiles(driver_key);
CREATE TABLE IF NOT EXISTS driver_score_reviews (
    task_no TEXT PRIMARY KEY, source_segment_id INTEGER NOT NULL,
    route_ok INTEGER NOT NULL DEFAULT 1 CHECK(route_ok IN (0,1)),
    return_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK(return_status IN ('not_required','completed','partial','failed','pending')),
    manual_deduction REAL NOT NULL DEFAULT 0 CHECK(manual_deduction >= 0 AND manual_deduction <= 100),
    manual_reason TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS driver_score_review_events (
    id INTEGER PRIMARY KEY, task_no TEXT NOT NULL, source_segment_id INTEGER NOT NULL,
    version INTEGER NOT NULL, previous_json TEXT, current_json TEXT NOT NULL, at TEXT NOT NULL,
    UNIQUE(task_no,version)
);
CREATE TABLE IF NOT EXISTS driver_score_overrides (
    driver_key TEXT PRIMARY KEY, manual_score REAL CHECK(manual_score >= 0 AND manual_score <= 100),
    reason TEXT NOT NULL, version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS driver_score_override_events (
    id INTEGER PRIMARY KEY, driver_key TEXT NOT NULL, action TEXT NOT NULL,
    version INTEGER NOT NULL, previous_json TEXT, current_json TEXT NOT NULL, at TEXT NOT NULL,
    UNIQUE(driver_key,version)
);
"""


def _order(seg):
    value = seg.get("sortNum")
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def in_scope(seg):
    """Only the first leg of a non-voided task originating at BWI and leaving for one of four branches."""
    if not seg or not _task_no(seg) or seg.get("placeOfOrigin") != "BWI.H" or seg.get("destination") not in DESTINATIONS:
        return False
    if seg.get("_validity") or seg.get("taskStatus") in (6, "6") or _order(seg) != 1:
        return False
    name = seg.get("taskName")
    if name:
        if not isinstance(name, str) or name.split("-", 1)[0].strip() != "BWI.H":
            return False
    return True


def profile_is_active(profile):
    return dict(profile or {}).get("source") not in INACTIVE_PROFILE_SOURCES


def _op_date(seg):
    try:
        matched = departures.match(seg)
        return str(matched["op_date"]) if matched and matched.get("op_date") else None
    except (ValueError, TypeError, OverflowError, rules.BadTime):
        return None


def in_task_scope(seg, task_segments, profile=None):
    if not in_scope(seg) or not profile_is_active(profile):
        return False
    first_leg = (seg.get("destination"), _op_date(seg))
    for other in task_segments:
        if other.get("taskNo") != seg.get("taskNo"):
            continue
        if other.get("_validity") or other.get("taskStatus") in (6, "6"):
            continue
        if other.get("placeOfOrigin") != "BWI.H":
            order = _order(other)
            if order is None or order <= 1:
                return False  # conflicting/unknown origin, never infer a branch task from an intermediate BWI stop
        elif _order(other) == 1 and (other.get("destination"), _op_date(other)) != first_leg:
            return False  # never choose an arbitrary route or operating day from conflicting cached first legs
    return True


def _iso(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _et(value):
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
        return dt.astimezone(rules.EASTERN).strftime("%Y-%m-%dT%H:%M") if dt and dt.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _task_no(seg):
    value = seg.get("taskNo") if seg else None
    return value if isinstance(value, str) and value.strip() else None


def _driver_key(profile):
    value = profile.get("driver_key")
    return value if isinstance(value, str) and value.strip() else None


def review_view(task_no, row=None):
    row = dict(row or {})
    return {"task_no": task_no, "source_segment_id": row.get("source_segment_id"),
            "route_ok": bool(row.get("route_ok", True)), "return_status": row.get("return_status", "not_required"),
            "manual_deduction": row.get("manual_deduction", 0), "manual_reason": row.get("manual_reason", ""),
            "version": row.get("version", 0), "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"), "updated_at_et": _et(row.get("updated_at"))}


def override_view(driver_key, row=None):
    row = dict(row or {})
    return {"driver_key": driver_key, "manual_score": row.get("manual_score"), "reason": row.get("reason", ""),
            "version": row.get("version", 0), "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"), "updated_at_et": _et(row.get("updated_at"))}


def task_view(seg, arrival, profile=None, review=None):
    """One task result. Identity is supplied only by the verified profile cache, never derived from names/plates."""
    task_no, profile, arrival = _task_no(seg), dict(profile or {}), dict(arrival or {})
    review = review_view(task_no, review)
    profile.setdefault("task_no", task_no)
    key = _driver_key(profile)
    profile["driver_key"] = key
    scoped = in_scope(seg) and task_no is not None and profile_is_active(profile)
    op_date = _op_date(seg) if scoped else None
    try:
        manual_departure = _et(rules.seg_time(seg, "manualActualDepartureTimeStrFormat"))
    except (ValueError, TypeError, rules.BadTime):
        manual_departure = None
    reasons, codes = [], []
    if arrival.get("status") == "unrecorded" or not arrival.get("actual_arrival_utc"):
        reasons.append("司机实际到场时间待登记")
        codes.append("arrival_missing")
    elif arrival.get("status") not in ("on_time", "late") or not arrival.get("scheduled_arrival_et"):
        reasons.append("约定到场时间待确认")
        codes.append("appointment_missing")
    if review["return_status"] == "pending":
        reasons.append("回货情况待确认")
        codes.append("return_pending")
    arrival_deduction = 45 if arrival.get("status") == "late" else 0 if arrival.get("status") == "on_time" else None
    deductions = {"arrival": arrival_deduction, "return": RETURN_DEDUCTIONS.get(review["return_status"]),
                  "manual": review["manual_deduction"], "route": 0 if review["route_ok"] else 100}
    score, status = None, "pending"
    if not scoped:
        status = "out_of_scope"
    elif not review["route_ok"]:
        score, status = 0, "scored"  # wrong route is a definitive zero even if other details remain pending
    elif not reasons:
        score, status = max(0, 100 - sum(deductions.values())), "scored"
    return {
        "task_no": task_no, "segment_id": seg.get("id"), "in_scope": scoped, "op_date": op_date,
        "route": seg.get("destination"), "destination": seg.get("destination"),
        "plate": profile.get("vehicle_plate") or seg.get("licensePlateNo") or "",
        "supplier": profile.get("supplier_name") or seg.get("supplierName") or "",
        "name": profile.get("driver_name") or "", "phone": profile.get("driver_phone") or "", "driver_key": key,
        "profile": profile, "identity_status": "identified" if key else "pending_identity",
        "arrival": arrival, "review": review, "status": status, "score": score, "base_score": 100,
        "deductions": deductions, "pending_reasons": reasons, "pending_codes": codes,
        "scheduled_arrival_et": arrival.get("scheduled_arrival_et"),
        "actual_arrival_et": arrival.get("actual_arrival_et"), "manual_departure_et": manual_departure,
        "late_minutes": arrival.get("late_minutes"),
    }


def _version(value):
    if type(value) is not int or not 0 <= value < 9223372036854775807:
        raise ValidationError("缺少有效记录版本，请重新读取后再保存。")
    return value


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}须为 0 到 100 的数字。")
    try:
        valid = math.isfinite(value) and 0 <= value <= 100
    except OverflowError:
        valid = False
    if not valid:
        raise ValidationError(f"{label}须为 0 到 100 的数字。")
    return value


def _reason(value, required=False):
    if not isinstance(value, str) or len(value) > 500:
        raise ValidationError("原因须为不超过 500 字的文字。")
    value = value.strip()
    if required and not value:
        raise ValidationError("请填写人工调整原因。")
    return value


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) if value is not None else None


class DriverScores:
    """Shares its Store connection; HTTP callers serialize it with the existing handler lock."""
    def __init__(self, db):
        self.db = db

    def get_review(self, task_no):
        row = self.db.execute("SELECT * FROM driver_score_reviews WHERE task_no=?", (task_no,)).fetchone()
        return review_view(task_no, row)

    def get_override(self, driver_key):
        row = self.db.execute("SELECT * FROM driver_score_overrides WHERE driver_key=?", (driver_key,)).fetchone()
        return override_view(driver_key, row)

    def snapshot(self):
        return {
            "profiles": {r["task_no"]: dict(r) for r in self.db.execute("SELECT * FROM driver_task_profiles")},
            "reviews": {r["task_no"]: dict(r) for r in self.db.execute("SELECT * FROM driver_score_reviews")},
            "overrides": {r["driver_key"]: dict(r) for r in self.db.execute("SELECT * FROM driver_score_overrides")},
        }

    def _segments(self, task_no):
        out = []
        for row in self.db.execute("SELECT data,validity FROM segments WHERE task_no=?", (task_no,)):
            seg = json.loads(row["data"])
            if row["validity"]:
                seg["_validity"] = row["validity"]
            out.append(seg)
        return out

    def _source(self, segment_id):
        if type(segment_id) is not int or not 0 <= segment_id < 9223372036854775808:
            raise ValidationError("任务航段编号无效。")
        row = self.db.execute("SELECT data,validity FROM segments WHERE id=?", (segment_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务航段不存在。")
        seg = json.loads(row["data"])
        if row["validity"]:
            seg["_validity"] = row["validity"]
        task_no = _task_no(seg)
        profile = self.db.execute("SELECT * FROM driver_task_profiles WHERE task_no=?", (task_no,)).fetchone()
        if not task_no or not in_task_scope(seg, self._segments(task_no), profile):
            raise ValidationError("仅可评分由 BWI 始发至 ORF01、RIC01、DCA01 或 IAD01 的有效任务。")
        return seg

    def _begin(self):
        if self.db.in_transaction:
            raise RuntimeError("driver score update must own its transaction")
        self.db.execute("BEGIN IMMEDIATE")

    def save_review(self, segment_id, body, now):
        required = {"route_ok", "return_status", "manual_deduction", "manual_reason", "version"}
        if not isinstance(body, dict) or set(body) != required:
            raise ValidationError("任务评分请求字段不完整或包含未知字段。")
        if type(body["route_ok"]) is not bool:
            raise ValidationError("线路是否正确须为布尔值。")
        if not isinstance(body["return_status"], str) or body["return_status"] not in RETURN_DEDUCTIONS:
            raise ValidationError("回货状态无效。")
        deduction = _number(body["manual_deduction"], "人工扣分")
        reason, version = _reason(body["manual_reason"], deduction > 0), _version(body["version"])
        at = _iso(now)
        if self.db.in_transaction:
            raise RuntimeError("driver score update must own its transaction")
        with self.db:
            self._begin()
            seg = self._source(segment_id)
            task_no = seg["taskNo"]
            old = self.get_review(task_no)
            if version != old["version"]:
                raise ConflictError("任务评分已被修改，请重新读取后再保存。")
            self.db.execute(
                "INSERT INTO driver_score_reviews(task_no,source_segment_id,route_ok,return_status,manual_deduction,"
                "manual_reason,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(task_no) DO UPDATE SET "
                "source_segment_id=excluded.source_segment_id,route_ok=excluded.route_ok,return_status=excluded.return_status,"
                "manual_deduction=excluded.manual_deduction,manual_reason=excluded.manual_reason,version=excluded.version,"
                "updated_at=excluded.updated_at",
                (task_no, segment_id, int(body["route_ok"]), body["return_status"], deduction, reason, version + 1, at, at))
            current = self.get_review(task_no)
            self.db.execute("INSERT INTO driver_score_review_events(task_no,source_segment_id,version,previous_json,"
                            "current_json,at) VALUES(?,?,?,?,?,?)",
                            (task_no, segment_id, version + 1, _dump(old) if version else None, _dump(current), at))
        return current

    def override(self, driver_key, body, now):
        if not isinstance(driver_key, str) or not driver_key.strip() or len(driver_key) > 300:
            raise ValidationError("可靠司机身份标识缺失，无法调整司机总分。")
        if not isinstance(body, dict) or set(body) != {"manual_score", "reason", "version"}:
            raise ValidationError("司机总分请求字段不完整或包含未知字段。")
        score = body["manual_score"]
        if score is not None:
            score = _number(score, "人工总分")
        reason, version, at = _reason(body["reason"], True), _version(body["version"]), _iso(now)
        if self.db.in_transaction:
            raise RuntimeError("driver score update must own its transaction")
        with self.db:
            self._begin()
            profiles = self.db.execute("SELECT task_no,source FROM driver_task_profiles WHERE driver_key=?", (driver_key,)).fetchall()
            profiles = [p for p in profiles if profile_is_active(p)]
            if not any(any(in_task_scope(seg, legs) for seg in legs)
                       for legs in (self._segments(p["task_no"]) for p in profiles)):
                raise NotFoundError("未找到关联有效支线任务的可靠司机身份。")
            old = self.get_override(driver_key)
            if version != old["version"]:
                raise ConflictError("司机总分已被修改，请重新读取后再保存。")
            self.db.execute(
                "INSERT INTO driver_score_overrides(driver_key,manual_score,reason,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(driver_key) DO UPDATE SET manual_score=excluded.manual_score,"
                "reason=excluded.reason,version=excluded.version,updated_at=excluded.updated_at",
                (driver_key, score, reason, version + 1, at, at))
            current = self.get_override(driver_key)
            self.db.execute("INSERT INTO driver_score_override_events(driver_key,action,version,previous_json,current_json,at) "
                            "VALUES(?,?,?,?,?,?)", (driver_key, "restore" if score is None else "set", version + 1,
                                                   _dump(old) if version else None, _dump(current), at))
        return current

    def board(self, segs, arrival_records, appointments, now):
        """All in-scope tasks and averages for 30 operating dates, using the existing 05:00 Eastern boundary."""
        from . import shift  # loaded at call time: shift itself imports Store helpers
        _iso(now)
        today = shift.reporting_date(now)
        start = today - timedelta(days=29)
        snapshot = self.snapshot()
        by_task = {}
        for seg in segs:
            task_no = _task_no(seg)
            if task_no:
                by_task.setdefault(task_no, []).append(seg)
        tasks = []
        for task_no, legs in by_task.items():
            candidates = [s for s in legs if in_task_scope(s, legs, snapshot["profiles"].get(task_no))]
            if not candidates:
                continue
            candidates.sort(key=lambda s: str(s.get("id") or ""))
            seg = candidates[0]
            arrival = driver_arrivals.view(seg, arrival_records.get(task_no), appointments.get(task_no))
            task = task_view(seg, arrival, snapshot["profiles"].get(task_no), snapshot["reviews"].get(task_no))
            task["segment_ids"] = [s.get("id") for s in candidates]
            task["destinations"] = sorted({s["destination"] for s in candidates})
            task["in_score_window"] = bool(task["op_date"] and str(start) <= task["op_date"] <= str(today))
            tasks.append(task)
        tasks.sort(key=lambda t: (t["op_date"] or "", t["task_no"]), reverse=True)
        by_driver = {}
        for task in tasks:
            if task["driver_key"]:
                by_driver.setdefault(task["driver_key"], []).append(task)
        drivers = []
        for key, all_tasks in by_driver.items():
            window_tasks = [t for t in all_tasks if t["in_score_window"]]
            scored = [t for t in window_tasks if t["score"] is not None]
            automatic = round(sum(t["score"] for t in scored) / len(scored), 1) if scored else None
            # Latest task supplies its own identity display; never borrow a phone/name from a similarly named driver.
            latest = max(all_tasks, key=lambda t: (t["op_date"] or "", t["profile"].get("updated_at") or "", t["task_no"]))
            override = override_view(key, snapshot["overrides"].get(key))
            manual = override["manual_score"]
            drivers.append({
                "driver_key": key, "name": latest["name"], "phone": latest["phone"],
                "supplier": latest["supplier"], "plate": latest["plate"], "profile": latest["profile"],
                "profile_task_no": latest["task_no"], "auto_score": automatic, "manual_score": manual,
                "score": manual if manual is not None else automatic, "override": override,
                "scored_task_count": len(scored), "pending_task_count": len(window_tasks) - len(scored),
                "window_task_count": len(window_tasks), "task_nos": [t["task_no"] for t in window_tasks],
                "all_task_nos": [t["task_no"] for t in all_tasks],
                "undated_task_count": sum(t["op_date"] is None for t in all_tasks),
                "status": "manual" if manual is not None else "scored" if automatic is not None else "pending",
            })
        drivers.sort(key=lambda d: (d["name"], d["driver_key"]))
        return {"tasks": tasks, "drivers": drivers, "window_start": str(start), "window_end": str(today),
                "unidentified_task_count": sum(t["driver_key"] is None for t in tasks)}
