"""Scope, scoring, exact identity aggregation, and atomic audited score changes. Synthetic data only."""
import json
import math
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from bwi_monitor import driver_arrivals, driver_scores, rules
from bwi_monitor.driver_scores import ConflictError, NotFoundError, ValidationError
from bwi_monitor.store import Store

NOW = datetime(2026, 10, 17, 22, tzinfo=timezone.utc)


def leg(sid=1, task="MT-SCORE-1", day=date(2026, 10, 17), **changes):
    value = {"id": sid, "taskNo": task, "placeOfOrigin": "BWI.H", "destination": "ORF01", "sortNum": 1,
             "taskName": "BWI.H-ORF01(23:00)", "licensePlateNo": "SYNTHETIC-PLATE",
             "plannedDepartureTime": (day + timedelta(days=1)).isoformat() + " 23:00:00"}
    value.update(changes)
    return value


def arrival(seg=None, late=False):
    seg = seg or leg()
    actual = "2026-10-17T21:01:00+00:00" if late else "2026-10-17T21:00:00+00:00"
    return driver_arrivals.view(seg, {"actual_arrival_utc": actual},
                                {"scheduled_arrival_at": "2026-10-17T21:00:00+00:00", "source": "synthetic appointment"})


def review(version=0, **changes):
    body = {"route_ok": True, "return_status": "not_required", "manual_deduction": 0, "manual_reason": "", "version": version}
    body.update(changes)
    return body


class ScopeAndScoringTests(unittest.TestCase):
    def test_only_first_leg_of_four_bwi_origin_routes_is_in_scope(self):
        for destination in ("ORF01", "RIC01", "DCA01", "IAD01"):
            self.assertTrue(driver_scores.in_scope(leg(destination=destination, taskName=f"BWI.H-{destination}")))
        for changes in ({"placeOfOrigin": "RIC01"}, {"destination": "BWI.H"}, {"destination": "JFK.H"},
                        {"destination": "BWI01"}, {"_validity": "UNVERIFIED"}, {"_validity": "VOIDED"},
                        {"taskStatus": 6}, {"taskStatus": "6"}, {"sortNum": None}, {"sortNum": "bad"},
                        {"sortNum": True}, {"sortNum": 2}, {"sortNum": float("nan")},
                        {"taskName": "CNO.H-BWI.H-ORF01"}, {"taskName": "BWI.HH-ORF01"},
                        {"taskNo": None}, {"taskNo": ""}, {"taskNo": "   "}, {"taskNo": 123}):
            with self.subTest(changes=changes):
                self.assertFalse(driver_scores.in_scope(leg(**changes)))
        self.assertTrue(driver_scores.in_scope(leg(sortNum="1")))
        self.assertFalse(driver_scores.in_scope({"placeOfOrigin": "BWI.H", "destination": "ORF01"}))

    def test_previous_foreign_origin_excludes_but_later_return_leg_does_not(self):
        outbound = leg()
        inbound = leg(2, placeOfOrigin="RIC01", destination="BWI.H", sortNum=0)
        self.assertFalse(driver_scores.in_task_scope(outbound, [outbound, inbound]))
        inbound["sortNum"] = None
        self.assertFalse(driver_scores.in_task_scope(outbound, [outbound, inbound]))
        inbound["sortNum"] = 2
        self.assertTrue(driver_scores.in_task_scope(outbound, [outbound, inbound]))
        inbound.update(taskNo="MT-UNRELATED", sortNum=1)
        self.assertTrue(driver_scores.in_task_scope(outbound, [outbound, inbound]))

    def test_first_legs_must_agree_on_route_and_operating_day(self):
        outbound = leg()
        for conflicting in (leg(2, destination="RIC01"), leg(2, destination="JFK.H"),
                            leg(2, day=date(2026, 10, 18)), leg(2, plannedDepartureTime=None)):
            with self.subTest(conflicting=conflicting):
                self.assertFalse(driver_scores.in_task_scope(outbound, [outbound, conflicting]))
                self.assertFalse(driver_scores.in_task_scope(conflicting, [outbound, conflicting]))
                self.assertTrue(driver_scores.in_task_scope(outbound, [outbound, dict(conflicting, _validity="VOIDED")]))
                self.assertTrue(driver_scores.in_task_scope(outbound, [outbound, dict(conflicting, taskStatus=6)]))
        self.assertTrue(driver_scores.in_task_scope(outbound, [outbound, leg(2)]))
        # A single missing date remains visible as undated, but cannot be included in a driver's average.
        undated = leg(plannedDepartureTime=None)
        self.assertTrue(driver_scores.in_task_scope(undated, [undated]))

    def test_late_return_manual_and_route_rules(self):
        self.assertEqual(driver_scores.task_view(leg(), arrival())["score"], 100)
        late = driver_scores.task_view(leg(), arrival(late=True))
        self.assertEqual((late["score"], late["deductions"]["arrival"]), (55, 45))
        self.assertEqual(driver_scores.task_view(leg(), arrival(late=True), review=review(return_status="partial"))["score"], 45)
        self.assertEqual(driver_scores.task_view(leg(), arrival(late=True), review=review(return_status="failed"))["score"], 30)
        self.assertEqual(driver_scores.task_view(leg(), arrival(late=True),
                         review=review(return_status="failed", manual_deduction=40, manual_reason="confirmed"))["score"], 0)
        wrong = driver_scores.task_view(leg(), {}, review=review(route_ok=False, return_status="pending"))
        self.assertEqual((wrong["score"], wrong["status"]), (0, "scored"))
        self.assertEqual(wrong["review"]["version"], 0)

    def test_missing_arrival_appointment_or_return_confirmation_never_defaults_to_100(self):
        for value, rev in (({}, review()), (driver_arrivals.view(leg(), {"actual_arrival_utc": NOW.isoformat()}), review()),
                           (arrival(), review(return_status="pending"))):
            with self.subTest(arrival=value, review=rev):
                result = driver_scores.task_view(leg(), value, review=rev)
                self.assertEqual((result["status"], result["score"]), ("pending", None))
                self.assertTrue(result["pending_reasons"])
        out = driver_scores.task_view(leg(destination="JFK.H"), arrival())
        self.assertEqual((out["status"], out["score"]), ("out_of_scope", None))

    def test_no_reliable_identity_is_derived_from_name_or_plate(self):
        result = driver_scores.task_view(leg(), arrival(), {"driver_name": "Test Name", "vehicle_plate": "P"})
        self.assertIsNone(result["driver_key"])
        self.assertEqual(result["identity_status"], "pending_identity")
        self.assertEqual(result["score"], 100)  # task result remains visible, but cannot become somebody's driver average


class ScoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "synthetic.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.store.db.executescript(driver_scores.SCHEMA)
        self.scores = driver_scores.DriverScores(self.store.db)
        self.segs = [leg()]
        self.store.upsert_segments(self.segs, NOW)

    def profile(self, task="MT-SCORE-1", key="driver:synthetic-1", name="Synthetic Driver", phone="test-phone", **changes):
        values = {"task_no": task, "driver_key": key, "driver_name": name, "driver_phone": phone,
                  "supplier_name": "Synthetic Supplier", "vehicle_plate": "TEST", "source": "TMS司机资料", "updated_at": NOW.isoformat()}
        values.update(changes)
        self.store.db.execute("INSERT OR REPLACE INTO driver_task_profiles(" + ",".join(values) + ") VALUES(" +
                              ",".join("?" for _ in values) + ")", tuple(values.values()))

    def board(self, arrivals=None, appointments=None, now=NOW):
        return self.scores.board(self.store.segments(), arrivals or {}, appointments or {}, now)


class ScorePersistenceTests(ScoreFixture):
    def test_review_versions_audit_and_restart(self):
        first = self.scores.save_review(1, review(return_status="partial"), NOW)
        second = self.scores.save_review(1, review(1, manual_deduction=3.5, manual_reason="Verified issue"), NOW + timedelta(minutes=1))
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["updated_at_et"], "2026-10-17T18:01")
        with self.assertRaises(ConflictError):
            self.scores.save_review(1, review(1, route_ok=False), NOW)
        events = self.store.db.execute("SELECT * FROM driver_score_review_events ORDER BY version").fetchall()
        self.assertEqual(len(events), 2)
        self.assertIsNone(events[0]["previous_json"])
        self.assertEqual(json.loads(events[1]["previous_json"])["return_status"], "partial")
        other = Store(self.path)
        self.addCleanup(other.db.close)
        self.assertEqual(driver_scores.DriverScores(other.db).get_review("MT-SCORE-1"), second)

    def test_review_validation_and_scope_rejection_write_nothing(self):
        bodies = [review(route_ok=1), review(return_status="unknown"), review(manual_deduction=True),
                  review(manual_deduction=-1), review(manual_deduction=101), review(manual_deduction=math.inf),
                  review(manual_deduction=math.nan), review(manual_deduction=1, manual_reason=" "),
                  review(version=True), review(version=-1), {"version": 0}, review(extra="unknown")]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                self.scores.save_review(1, body, NOW)
        with self.assertRaises(NotFoundError):
            self.scores.save_review(999, review(), NOW)
        self.store.db.execute("UPDATE segments SET validity='UNVERIFIED' WHERE id=1")
        with self.assertRaises(ValidationError):
            self.scores.save_review(1, review(), NOW)
        self.store.db.execute("UPDATE segments SET validity=NULL WHERE id=1")
        self.profile(source="TMS任务已取消")
        with self.assertRaises(ValidationError):
            self.scores.save_review(1, review(), NOW)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM driver_score_reviews").fetchone()[0], 0)
        self.assertFalse(self.store.db.in_transaction)

    def test_foreign_origin_sibling_is_rejected_even_if_selected_leg_looks_like_first(self):
        self.store.upsert_segments([leg(2, placeOfOrigin="EWR.H", destination="BWI.H", sortNum=1)], NOW)
        with self.assertRaises(ValidationError):
            self.scores.save_review(1, review(), NOW)
        self.assertEqual(self.board()["tasks"], [])

    def test_conflicting_first_legs_block_arrival_review_override_and_existing_score(self):
        self.profile()
        self.scores.save_review(1, review(route_ok=False), NOW)
        for conflicting in (leg(2, destination="RIC01"), leg(2, day=date(2026, 10, 18))):
            with self.subTest(conflicting=conflicting):
                self.store.upsert_segments([conflicting], NOW)
                self.assertEqual((self.board()["tasks"], self.board()["drivers"]), ([], []))
                with self.assertRaises(ValidationError):
                    self.scores.save_review(1, review(1), NOW)
                with self.assertRaises(ValidationError):
                    self.store.driver_arrivals.save(1, "2026-10-17T17:00", 0, NOW)
                with self.assertRaises(NotFoundError):
                    self.scores.override("driver:synthetic-1", {"manual_score": 80, "reason": "Test", "version": 0}, NOW)
        self.assertEqual(self.scores.get_review("MT-SCORE-1")["version"], 1)

    def test_review_audit_failure_rolls_back_and_existing_transaction_is_preserved(self):
        self.store.db.execute("CREATE TRIGGER fail_review_audit BEFORE INSERT ON driver_score_review_events "
                              "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.scores.save_review(1, review(), NOW)
        self.assertEqual(self.scores.get_review("MT-SCORE-1")["version"], 0)
        self.store.db.execute("BEGIN")
        with self.assertRaises(RuntimeError):
            self.scores.save_review(1, review(), NOW)
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.rollback()

    def test_two_connections_cannot_overwrite_the_same_review_version(self):
        other = Store(self.path)
        self.addCleanup(other.db.close)
        ready = threading.Barrier(2)

        def save(repo):
            ready.wait()
            try:
                return repo.save_review(1, review(), NOW)["version"]
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(save, (self.scores, driver_scores.DriverScores(other.db))))
        self.assertCountEqual(values, [1, "conflict"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM driver_score_review_events").fetchone()[0], 1)

    def test_manual_total_override_restore_and_unknown_identity(self):
        self.profile()
        manual = self.scores.override("driver:synthetic-1", {"manual_score": 83.5, "reason": "Confirmed adjustment", "version": 0}, NOW)
        driver = self.board()["drivers"][0]
        self.assertEqual((driver["score"], driver["auto_score"], driver["override"]["version"]), (83.5, None, 1))
        self.assertEqual(manual["manual_score"], 83.5)
        with self.assertRaises(ConflictError):
            self.scores.override("driver:synthetic-1", {"manual_score": 10, "reason": "Stale write", "version": 0}, NOW)
        self.scores.override("driver:synthetic-1", {"manual_score": None, "reason": "Restore automatic", "version": 1}, NOW)
        restored = self.board()["drivers"][0]
        self.assertIsNone(restored["score"])
        self.assertEqual(restored["override"]["version"], 2)
        events = self.store.db.execute("SELECT action,current_json FROM driver_score_override_events ORDER BY version").fetchall()
        self.assertEqual([e["action"] for e in events], ["set", "restore"])
        self.assertIsNone(json.loads(events[1]["current_json"])["manual_score"])
        with self.assertRaises(NotFoundError):
            self.scores.override("driver:unknown", {"manual_score": 80, "reason": "Test", "version": 0}, NOW)
        for value in (None, "", 123):
            with self.subTest(key=value), self.assertRaises(ValidationError):
                self.scores.override(value, {"manual_score": 80, "reason": "Test", "version": 0}, NOW)

    def test_override_requires_reason_and_a_valid_score_and_is_atomic(self):
        self.profile()
        for score, reason in ((80, ""), (None, ""), (True, "Test"), (-1, "Test"), (101, "Test"), (math.nan, "Test")):
            with self.subTest(score=score, reason=reason), self.assertRaises(ValidationError):
                self.scores.override("driver:synthetic-1", {"manual_score": score, "reason": reason, "version": 0}, NOW)
        self.store.db.execute("CREATE TRIGGER fail_override_audit BEFORE INSERT ON driver_score_override_events "
                              "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.scores.override("driver:synthetic-1", {"manual_score": 80, "reason": "Test", "version": 0}, NOW)
        self.assertEqual(self.scores.get_override("driver:synthetic-1")["version"], 0)

    def test_two_connections_cannot_overwrite_the_same_total_override_version(self):
        self.profile()
        other = Store(self.path)
        self.addCleanup(other.db.close)
        ready = threading.Barrier(2)

        def save(repo):
            ready.wait()
            try:
                return repo.override("driver:synthetic-1", {"manual_score": 80, "reason": "Test", "version": 0}, NOW)["version"]
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(save, (self.scores, driver_scores.DriverScores(other.db))))
        self.assertCountEqual(values, [1, "conflict"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM driver_score_override_events").fetchone()[0], 1)

    def test_inactive_profile_excludes_existing_task_scores_and_overrides(self):
        self.profile()
        self.scores.save_review(1, review(route_ok=False), NOW)
        self.scores.override("driver:synthetic-1", {"manual_score": 95, "reason": "Test", "version": 0}, NOW)
        for source in ("TMS任务已取消", "TMS任务范围不符"):
            self.profile(source=source)
            self.assertEqual((self.board()["tasks"], self.board()["drivers"]), ([], []))
            self.assertEqual(driver_scores.task_view(leg(), arrival(), {"source": source})["status"], "out_of_scope")
            with self.assertRaises(NotFoundError):
                self.scores.override("driver:synthetic-1", {"manual_score": None, "reason": "Restore", "version": 1}, NOW)

    def test_scoring_and_writes_leave_tms_and_alert_tables_unchanged(self):
        self.profile()
        original = [tuple(r) for r in self.store.db.execute("SELECT * FROM segments")]
        self.scores.save_review(1, review(return_status="partial"), NOW)
        self.scores.override("driver:synthetic-1", {"manual_score": 70, "reason": "Test", "version": 0}, NOW)
        self.board()
        self.assertEqual(original, [tuple(r) for r in self.store.db.execute("SELECT * FROM segments")])
        for table in ("alerts", "alert_events", "notifications", "notification_keys"):
            self.assertEqual(self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)


class DriverWindowTests(ScoreFixture):
    def test_driver_average_retains_one_decimal_from_unrounded_task_scores(self):
        records, appointments = {}, {}
        for sid, task, late in ((1, "MT-SCORE-1", False), (2, "MT-TWO", True), (3, "MT-THREE", True)):
            self.store.upsert_segments([leg(sid, task)], NOW)
            self.profile(task)
            records[task] = {"actual_arrival_utc": "2026-10-17T21:01:00+00:00" if late else "2026-10-17T21:00:00+00:00"}
            appointments[task] = {"scheduled_arrival_at": "2026-10-17T21:00:00+00:00"}
        self.scores.save_review(3, review(manual_deduction=1, manual_reason="Confirmed"), NOW)
        driver = self.board(records, appointments)["drivers"][0]
        self.assertEqual((driver["auto_score"], driver["score"], driver["scored_task_count"]), (69.7, 69.7, 3))

    def test_midnight_to_0500_uses_previous_operating_day(self):
        self.store.upsert_segments([leg(2, "MT-NEXT-DAY", date(2026, 10, 18)),
                                   leg(3, "MT-FIRST-DAY", date(2026, 9, 18))], NOW)
        for task in ("MT-SCORE-1", "MT-NEXT-DAY", "MT-FIRST-DAY"):
            self.profile(task)
        before = self.board(now=datetime(2026, 10, 18, 8, 59, tzinfo=timezone.utc))  # 04:59 EDT
        after = self.board(now=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc))  # 05:00 EDT
        self.assertEqual((before["window_start"], before["window_end"]), ("2026-09-18", "2026-10-17"))
        self.assertEqual((after["window_start"], after["window_end"]), ("2026-09-19", "2026-10-18"))
        self.assertCountEqual(before["drivers"][0]["task_nos"], ["MT-SCORE-1", "MT-FIRST-DAY"])
        self.assertCountEqual(after["drivers"][0]["task_nos"], ["MT-SCORE-1", "MT-NEXT-DAY"])

    def test_full_task_list_deduplication_and_30_day_eastern_window(self):
        cases = [(1, "MT-TODAY", date(2026, 10, 17), False), (2, "MT-FIRST", date(2026, 9, 18), True),
                 (3, "MT-OLD", date(2026, 9, 17), False), (4, "MT-FUTURE", date(2026, 10, 18), True),
                 (5, "MT-PENDING", date(2026, 10, 16), None)]
        self.store.db.execute("DELETE FROM segments")
        records, appointments = {}, {}
        for sid, task, day, late in cases:
            self.store.upsert_segments([leg(sid, task, day)], NOW)
            self.profile(task, name="Newest Name" if task == "MT-FUTURE" else "Current Name", phone=task)
            if late is not None:
                records[task] = {"actual_arrival_utc": "2026-10-17T21:01:00+00:00" if late else "2026-10-17T21:00:00+00:00"}
                appointments[task] = {"scheduled_arrival_at": "2026-10-17T21:00:00+00:00"}
        self.store.upsert_segments([leg(6, "MT-TODAY"), leg(7, "MT-TODAY", sortNum=2,
                                  placeOfOrigin="ORF01", destination="BWI.H")], NOW)
        # 01:00 UTC on the 18th is still October 17 in New York.
        board = self.board(records, appointments, datetime(2026, 10, 18, 1, tzinfo=timezone.utc))
        self.assertEqual((board["window_start"], board["window_end"]), ("2026-09-18", "2026-10-17"))
        self.assertEqual(len(board["tasks"]), 5)
        self.assertEqual(next(t for t in board["tasks"] if t["task_no"] == "MT-TODAY")["segment_ids"], [1, 6])
        driver = board["drivers"][0]
        self.assertEqual((driver["auto_score"], driver["scored_task_count"], driver["pending_task_count"]), (77.5, 2, 1))
        self.assertCountEqual(driver["task_nos"], ["MT-TODAY", "MT-FIRST", "MT-PENDING"])
        self.assertEqual((driver["name"], driver["phone"]), ("Newest Name", "MT-FUTURE"))

    def test_same_name_plate_and_unknown_identities_never_merge(self):
        for sid, task, key in ((1, "MT-SCORE-1", "id:one"), (2, "MT-TWO", "id:two"), (3, "MT-UNKNOWN", None)):
            self.store.upsert_segments([leg(sid, task)], NOW)
            self.profile(task, key=key, name="Same Name", vehicle_plate="SAME-PLATE")
        board = self.board()
        self.assertEqual(len(board["tasks"]), 3)
        self.assertEqual(len(board["drivers"]), 2)
        self.assertEqual(board["unidentified_task_count"], 1)
        self.assertTrue(all(d["score"] is None and d["pending_task_count"] == 1 for d in board["drivers"]))
        self.assertCountEqual([d["driver_key"] for d in board["drivers"]], ["id:one", "id:two"])

    def test_missing_operating_date_is_visible_but_not_counted(self):
        self.store.upsert_segments([leg(plannedDepartureTime=None)], NOW)
        self.profile()
        board = self.board()
        self.assertIsNone(board["tasks"][0]["op_date"])
        self.assertEqual((board["drivers"][0]["scored_task_count"], board["drivers"][0]["undated_task_count"]), (0, 1))
        self.assertIsNone(board["drivers"][0]["auto_score"])


if __name__ == "__main__":
    unittest.main()
