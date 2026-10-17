"""Import confirmed legacy task appointments without importing driver data.

Run against a test copy first::

    python3 tools/import_driver_appointments.py --source /path/to/dispatch.db \
        --target /path/to/monitor-test.db --dry-run

Only the legacy check-in ID, task number, business date, appointment time and
match confirmation are read. The source database is always opened read-only.
Existing target appointments, including manual appointments, are never changed.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
SOURCE_LABEL = "旧调度已确认任务约定"
SITE_CODE = "BWI.H"
LEGACY_PATHS = (
    Path.home() / "Library/Application Support/BWIH Dispatch/dispatch.db",
    Path.home() / "Desktop/调度框架/dispatch.db",
)
APPOINTMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_appointments (
    task_no TEXT PRIMARY KEY,
    scheduled_arrival_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_record_id TEXT,
    imported_at TEXT NOT NULL
)
"""


class InvalidAppointment(ValueError):
    """An appointment cannot be converted without guessing its meaning."""


def appointment_utc(business_date, scheduled_time):
    """Resolve the old 03:00 business-day boundary to one UTC instant.

    Ambiguous fall-back times and nonexistent spring-forward times have no
    trustworthy instant in the source and are rejected rather than adjusted.
    """
    day_text = str(business_date or "").strip()
    clock_text = str(scheduled_time or "").strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day_text):
        raise InvalidAppointment("invalid_schedule")
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock_text):
        raise InvalidAppointment("invalid_schedule")
    try:
        day = date.fromisoformat(day_text)
        clock = time.fromisoformat(clock_text)
        if clock < time(3):
            day += timedelta(days=1)
        naive = datetime.combine(day, clock)
        candidates = set()
        for fold in (0, 1):
            utc = naive.replace(tzinfo=EASTERN, fold=fold).astimezone(timezone.utc)
            if utc.astimezone(EASTERN).replace(tzinfo=None) == naive:
                candidates.add(utc)
    except (ValueError, OverflowError):
        raise InvalidAppointment("invalid_schedule") from None
    if not candidates:
        raise InvalidAppointment("dst_nonexistent")
    if len(candidates) != 1:
        raise InvalidAppointment("dst_ambiguous")
    return next(iter(candidates)).isoformat(timespec="seconds")


def _readonly(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def _same_instant(left, right):
    try:
        left_dt = datetime.fromisoformat(left)
        right_dt = datetime.fromisoformat(right)
        return (left_dt.tzinfo is not None and right_dt.tzinfo is not None
                and left_dt == right_dt)
    except (TypeError, ValueError):
        return False


def import_appointments(source, target, *, dry_run=False):
    """Return aggregate counts; no task, driver or other personal data is logged.

    A confirmed task is imported only if all of its confirmed rows describe
    the same valid appointment and a BWI-related segment already exists in
    the target. Source arrival times and historical scores are never read.
    """
    source = Path(source).expanduser().resolve(strict=True)
    target = Path(target).expanduser().resolve(strict=True)
    if source.samefile(target):
        raise ValueError("Source and target must be different database files.")
    summary = dict.fromkeys((
        "source_rows", "unconfirmed_rows", "invalid_task_rows", "confirmed_tasks",
        "invalid_schedule_rows", "dst_nonexistent_rows", "dst_ambiguous_rows",
        "invalid_schedule_tasks", "conflicting_schedule_tasks", "unmatched_tasks",
        "already_present", "existing_conflicts", "would_import", "imported",
    ), 0)
    summary["dry_run"] = bool(dry_run)
    groups = defaultdict(list)
    source_db = _readonly(source)
    try:
        # Deliberately do not SELECT *, join drivers, or read actual arrivals.
        rows = source_db.execute(
            "SELECT id, date, scheduled_time, dms_task_id, dms_match_confirmed "
            "FROM checkins ORDER BY id"
        )
        for record_id, day, clock, task, confirmed in rows:
            summary["source_rows"] += 1
            if confirmed != 1:
                summary["unconfirmed_rows"] += 1
                continue
            task = str(task or "").strip()
            if not re.fullmatch(r"MT[0-9]+", task):
                summary["invalid_task_rows"] += 1
                continue
            try:
                scheduled = appointment_utc(day, clock)
            except InvalidAppointment as exc:
                summary[str(exc) + "_rows"] += 1
                scheduled = None
            groups[task].append((record_id, scheduled))
    finally:
        source_db.close()

    summary["confirmed_tasks"] = len(groups)
    target_db = (_readonly(target) if dry_run else
                 sqlite3.connect(target.as_uri() + "?mode=rw", uri=True))
    try:
        if not dry_run:
            # Lock before checking existing rows so another writer cannot be
            # overwritten between the conflict check and insertion.
            target_db.execute("BEGIN IMMEDIATE")
        existing_tasks = {row[0] for row in target_db.execute(
            "SELECT DISTINCT task_no FROM segments WHERE origin=? OR destination=?",
            (SITE_CODE, SITE_CODE),
        )}
        has_table = target_db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_appointments'"
        ).fetchone()
        existing = dict(target_db.execute(
            "SELECT task_no, scheduled_arrival_at FROM driver_appointments"
        )) if has_table else {}
        if not dry_run:
            target_db.execute(APPOINTMENT_SCHEMA)
        imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for task, appointments in groups.items():
            schedules = {scheduled for _, scheduled in appointments}
            if None in schedules:
                summary["invalid_schedule_tasks"] += 1
                continue
            if len(schedules) != 1:
                summary["conflicting_schedule_tasks"] += 1
                continue
            if task not in existing_tasks:
                summary["unmatched_tasks"] += 1
                continue
            scheduled = next(iter(schedules))
            if task in existing:
                summary["already_present" if _same_instant(existing[task], scheduled)
                        else "existing_conflicts"] += 1
                continue
            summary["would_import"] += 1
            if not dry_run:
                target_db.execute(
                    "INSERT INTO driver_appointments(task_no, scheduled_arrival_at, source, "
                    "source_record_id, imported_at) VALUES (?,?,?,?,?)",
                    (task, scheduled, SOURCE_LABEL,
                     ",".join(str(record_id) for record_id, _ in appointments), imported_at),
                )
                summary["imported"] += 1
        if not dry_run:
            target_db.commit()
    except Exception:
        if not dry_run:
            target_db.rollback()
        raise
    finally:
        target_db.close()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Legacy dispatch.db; always read-only")
    parser.add_argument("--target", type=Path, required=True,
                        help="Existing monitor database; use a test copy first")
    parser.add_argument("--dry-run", action="store_true", help="Read-only preview; writes nothing")
    args = parser.parse_args(argv)
    source = args.source or next((p for p in LEGACY_PATHS if p.is_file()), None)
    if source is None:
        parser.error("Legacy database not found; supply --source.")
    try:
        summary = import_appointments(source, args.target, dry_run=args.dry_run)
    except (OSError, sqlite3.Error, ValueError) as exc:
        # Do not print SQLite row values or paths supplied by database content.
        parser.exit(1, "Import failed; no changes committed (" + type(exc).__name__ + ").\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
