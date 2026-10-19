from contextlib import closing
"""Legacy appointments are explicit task data, never inferred route defaults."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.import_driver_appointments import (
    APPOINTMENT_SCHEMA, InvalidAppointment, SOURCE_LABEL, appointment_utc, import_appointments, main,
)


class LegacyTimeConversionTests(unittest.TestCase):
    def test_business_date_boundary_and_midnight(self):
        self.assertEqual(appointment_utc("2026-10-16", "02:59"), "2026-10-17T06:59:00+00:00")
        self.assertEqual(appointment_utc("2026-10-16", "03:00"), "2026-10-16T07:00:00+00:00")
        self.assertEqual(appointment_utc("2026-12-31", "00:00"), "2027-01-01T05:00:00+00:00")

    def test_dst_ambiguous_and_nonexistent_times_are_rejected(self):
        for day, clock, reason in (("2026-03-07", "02:30", "dst_nonexistent"),
                                   ("2026-10-31", "01:30", "dst_ambiguous")):
            with self.subTest(day=day), self.assertRaisesRegex(InvalidAppointment, reason):
                appointment_utc(day, clock)
        self.assertEqual(appointment_utc("2026-03-08", "03:00"), "2026-03-08T07:00:00+00:00")
        self.assertEqual(appointment_utc("2026-10-31", "02:30"), "2026-11-01T07:30:00+00:00")

    def test_invalid_date_or_time_never_normalized(self):
        for day, clock in (("2026-02-30", "12:00"), ("2026-10-16", "24:00"),
                           ("2026-10-16", "9:00"), ("2026-10-16", "12:00:00"),
                           ("2026-W41-5", "12:00"), ("", ""), ("9999-12-31", "01:00")):
            with self.subTest(day=day, clock=clock), self.assertRaises(InvalidAppointment):
                appointment_utc(day, clock)


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "legacy.db"
        self.target = Path(self.temp.name) / "monitor.db"
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE checkins(id INTEGER PRIMARY KEY, date TEXT, scheduled_time TEXT, "
                       "dms_task_id TEXT, dms_match_confirmed INTEGER, "
                       "arrival_time TEXT, score_given REAL, notes TEXT)")
        with closing(sqlite3.connect(self.target)) as db, db:
            db.execute("CREATE TABLE segments(id INTEGER PRIMARY KEY, task_no TEXT, "
                       "origin TEXT, destination TEXT, data TEXT)")

    def add_source(self, task="MT1", day="2026-10-16", clock="12:00", confirmed=1):
        with closing(sqlite3.connect(self.source)) as db, db:
            return db.execute("INSERT INTO checkins(date,scheduled_time,dms_task_id,dms_match_confirmed,"
                              "arrival_time,score_given,notes) VALUES (?,?,?,?,?,?,?)",
                              (day, clock, task, confirmed, "14:00", 0, "DO NOT COPY PERSONAL DATA")).lastrowid

    def add_target(self, task="MT1", origin="BWI.H", destination="EWR.H"):
        with closing(sqlite3.connect(self.target)) as db, db:
            db.execute("INSERT INTO segments(task_no,origin,destination,data) VALUES (?,?,?,?)",
                       (task, origin, destination, '{"untouched":true}'))

    def appointments(self):
        with closing(sqlite3.connect(self.target)) as db, db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM driver_appointments ORDER BY task_no")]

    def test_only_appointment_fields_and_idempotent_multi_segment_import(self):
        first_id = self.add_source()
        second_id = self.add_source()
        self.add_target()
        self.add_target(origin="EWR.H", destination="BWI.H")
        source_before = self.source.read_bytes()
        summary = import_appointments(self.source, self.target)
        self.assertEqual(summary["imported"], 1)
        record = self.appointments()[0]
        self.assertEqual(record["source"], SOURCE_LABEL)
        self.assertEqual(record["source_record_id"], f"{first_id},{second_id}")
        self.assertEqual(record["scheduled_arrival_at"], "2026-10-16T16:00:00+00:00")
        self.assertEqual(datetime.fromisoformat(record["imported_at"]).tzinfo, timezone.utc)
        self.assertEqual(set(record), {"task_no", "scheduled_arrival_at", "source", "source_record_id", "imported_at"})
        self.assertNotIn("PERSONAL", json.dumps(record))
        repeated = import_appointments(self.source, self.target)
        self.assertEqual((repeated["already_present"], repeated["imported"]), (1, 0))
        self.assertEqual(self.appointments(), [record])
        self.assertEqual(self.source.read_bytes(), source_before)
        with closing(sqlite3.connect(self.target)) as db, db:
            self.assertEqual(db.execute("SELECT DISTINCT data FROM segments").fetchall(), [('{"untouched":true}',)])
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='driver_arrivals'").fetchone())

    def test_dry_run_does_not_create_schema_or_change_files(self):
        self.add_source()
        self.add_target()
        before = (self.source.read_bytes(), self.target.read_bytes())
        summary = import_appointments(self.source, self.target, dry_run=True)
        self.assertEqual((summary["would_import"], summary["imported"]), (1, 0))
        self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), before)

    def test_unconfirmed_invalid_and_non_bwi_tasks_are_not_imported(self):
        self.add_source("MT1", confirmed=0)
        self.add_source("MT2", confirmed=2)
        self.add_source("MTBAD")
        self.add_source("MT3")
        self.add_source("MT4")
        self.add_target("MT1")
        self.add_target("MT2")
        self.add_target("MT3", origin="JFK.H", destination="EWR.H")
        summary = import_appointments(self.source, self.target)
        self.assertEqual(summary["unconfirmed_rows"], 2)
        self.assertEqual(summary["invalid_task_rows"], 1)
        self.assertEqual(summary["unmatched_tasks"], 2)
        self.assertEqual(summary["imported"], 0)

    def test_conflicting_and_invalid_confirmed_rows_block_whole_task(self):
        self.add_source("MT1")
        self.add_source("MT1", clock="13:00")
        self.add_source("MT2")
        self.add_source("MT2", clock="")
        self.add_source("MT3", day="2026-03-07", clock="02:30")
        self.add_source("MT4", day="2026-10-31", clock="01:30")
        for task in ("MT1", "MT2", "MT3", "MT4"):
            self.add_target(task)
        summary = import_appointments(self.source, self.target)
        self.assertEqual(summary["conflicting_schedule_tasks"], 1)
        self.assertEqual(summary["invalid_schedule_tasks"], 3)
        self.assertEqual(summary["invalid_schedule_rows"], 1)
        self.assertEqual(summary["dst_nonexistent_rows"], 1)
        self.assertEqual(summary["dst_ambiguous_rows"], 1)
        self.assertEqual(self.appointments(), [])

    def test_existing_manual_appointment_never_overwritten(self):
        self.add_source()
        self.add_target()
        import_appointments(self.source, self.target)
        with closing(sqlite3.connect(self.target)) as db, db:
            db.execute("UPDATE driver_appointments SET scheduled_arrival_at=?, source=?, source_record_id=NULL",
                       ("2026-10-16T17:00:00+00:00", "人工约定"))
        existing = self.appointments()
        summary = import_appointments(self.source, self.target)
        self.assertEqual((summary["existing_conflicts"], summary["imported"]), (1, 0))
        self.assertEqual(self.appointments(), existing)
        with closing(sqlite3.connect(self.target)) as db, db:
            db.execute("UPDATE driver_appointments SET scheduled_arrival_at=?", ("2026-10-16T12:00:00-04:00",))
        existing = self.appointments()
        self.assertEqual(import_appointments(self.source, self.target)["already_present"], 1)
        self.assertEqual(self.appointments(), existing)

    def test_unconfirmed_conflicting_schedule_does_not_override_confirmed(self):
        self.add_source()
        self.add_source(clock="13:00", confirmed=0)
        self.add_target()
        self.assertEqual(import_appointments(self.source, self.target)["imported"], 1)
        self.assertEqual(self.appointments()[0]["scheduled_arrival_at"], "2026-10-16T16:00:00+00:00")

    def test_insert_failure_rolls_back_the_entire_import(self):
        for task in ("MT1", "MT2"):
            self.add_source(task)
            self.add_target(task)
        with closing(sqlite3.connect(self.target)) as db, db:
            db.execute(APPOINTMENT_SCHEMA)
            db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON driver_appointments "
                       "WHEN NEW.task_no='MT2' BEGIN SELECT RAISE(ABORT, 'test rejection'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            import_appointments(self.source, self.target)
        self.assertEqual(self.appointments(), [])

    def test_cli_output_contains_only_summary_counts(self):
        self.add_source()
        self.add_target()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--source", str(self.source), "--target", str(self.target), "--dry-run"]), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["would_import"], 1)
        self.assertTrue(all(isinstance(value, (int, bool)) for value in summary.values()))
        self.assertNotIn("MT1", output.getvalue())

    def test_source_target_identity_and_missing_target_rejected(self):
        with self.assertRaises(ValueError):
            import_appointments(self.source, self.source)
        hardlink = Path(self.temp.name) / "same.db"
        os.link(self.source, hardlink)  # Path.hardlink_to needs Python 3.10; macOS ships 3.9
        with self.assertRaises(ValueError):
            import_appointments(self.source, hardlink)
        with self.assertRaises(FileNotFoundError):
            import_appointments(self.source, Path(self.temp.name) / "missing.db")


if __name__ == "__main__":
    unittest.main()
