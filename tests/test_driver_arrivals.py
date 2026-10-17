"""Driver arrival persistence, scoring, isolation, and HTTP boundary tests; no live services."""
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import Message
from types import SimpleNamespace
from unittest import mock

from bwi_monitor import config, driver_arrivals, rules, web
from bwi_monitor.store import Store

NOW = datetime(2026, 10, 17, 22, tzinfo=timezone.utc)


def segment(sid=1, task="MT-DRIVER-TEST", origin="BWI.H", destination="ORF01", **kw):
    return dict(id=sid, taskNo=task, placeOfOrigin=origin, destination=destination, sortNum=sid,
                plannedDepartureTime="2026-10-18 23:00:00", plannedArrivalTime="2026-10-19 03:00:00", **kw)


class ArrivalTimeTests(unittest.TestCase):
    def test_eastern_time_is_converted_to_utc(self):
        self.assertEqual(driver_arrivals.parse_actual_arrival("2026-10-17T17:00", NOW),
                         "2026-10-17T21:00:00+00:00")
        self.assertEqual(driver_arrivals.parse_actual_arrival("2026-01-17T17:00", NOW),
                         "2026-01-17T22:00:00+00:00")
        self.assertEqual(driver_arrivals.parse_actual_arrival("2026-10-17T18:00", NOW), NOW.isoformat())

    def test_invalid_incomplete_or_future_times_are_rejected(self):
        for value in ("17:00", "2026-02-30T17:00", "2026-10-17 17:00", "2026-10-17T17:00Z",
                      "2026-10-17T17:00:00", "2026-10-17T18:01", "", 123, [], True):
            with self.subTest(value=value), self.assertRaises(driver_arrivals.ValidationError):
                driver_arrivals.parse_actual_arrival(value, NOW)
        self.assertIsNone(driver_arrivals.parse_actual_arrival(None, NOW))

    def test_dst_gap_and_ambiguity_are_rejected(self):
        later = datetime(2026, 12, 1, tzinfo=timezone.utc)
        for value in ("2026-03-08T02:30", "2026-11-01T01:30"):
            with self.subTest(value=value), self.assertRaises(driver_arrivals.ValidationError):
                driver_arrivals.parse_actual_arrival(value, later)
        self.assertEqual(driver_arrivals.parse_actual_arrival("2026-11-01T02:30", later),
                         "2026-11-01T07:30:00+00:00")


class ArrivalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "test.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.segs = [segment(), segment(2, origin="RIC01", destination="BWI.H"),
                     segment(3, task="MT-OTHER", origin="EWR.H", destination="JFK.H")]
        self.store.upsert_segments(self.segs, NOW)
        self.arrivals = self.store.driver_arrivals

    def appointment(self, value="2026-10-17T21:00:00+00:00"):
        self.store.db.execute("INSERT OR REPLACE INTO driver_appointments VALUES(?,?,?,?,?)",
                              ("MT-DRIVER-TEST", value, "legacy_dispatch.checkins", "42", NOW.isoformat()))

    def test_scoped_task_create_update_clear_and_audit_survive_restart(self):
        first = self.arrivals.save(1, "2026-10-17T17:00", 0, NOW)
        self.assertEqual(first["version"], 1)
        self.assertFalse(self.arrivals.view(self.segs[1])["eligible"])
        with self.assertRaises(driver_arrivals.ValidationError):
            self.arrivals.save(2, "2026-10-17T17:05", 1, NOW)
        updated = self.arrivals.save(1, "2026-10-17T17:05", 1, NOW + timedelta(minutes=1))
        self.assertEqual((updated["version"], updated["source_segment_id"]), (2, 1))
        self.assertEqual(updated["created_at"], first["created_at"])
        self.assertNotEqual(updated["updated_at"], first["updated_at"])
        self.assertEqual(updated["updated_at_et"], "2026-10-17T18:01")
        cleared = self.arrivals.save(1, None, 2, NOW + timedelta(minutes=2))
        self.assertEqual((cleared["version"], cleared["status"], cleared["actual_arrival_et"]), (3, "unrecorded", None))
        with self.assertRaises(driver_arrivals.ConflictError):
            self.arrivals.save(1, "2026-10-17T17:15", 2, NOW)
        other = Store(self.path)
        self.addCleanup(other.db.close)
        self.assertEqual(other.driver_arrivals.view(self.segs[0]), cleared)
        audit = self.store.db.execute("SELECT * FROM driver_arrival_events ORDER BY version").fetchall()
        self.assertEqual([r["action"] for r in audit], ["create", "update", "clear"])
        self.assertEqual(audit[2]["previous_actual_arrival_utc"], updated["actual_arrival_utc"])
        self.assertIsNone(audit[2]["actual_arrival_utc"])

    def test_scoring_uses_only_exact_appointment_and_full_dates(self):
        missing = self.arrivals.save(1, "2026-10-17T17:00", 0, NOW)
        self.assertEqual((missing["status"], missing["score"], missing["scheduled_arrival_et"]),
                         ("pending_schedule", None, None))
        self.appointment()
        exact = self.arrivals.view(self.segs[0])
        self.assertEqual((exact["status"], exact["score"], exact["late_minutes"]), ("on_time", 100, 0))
        late = self.arrivals.save(1, "2026-10-17T17:01", 1, NOW)
        self.assertEqual((late["status"], late["score"], late["late_minutes"]), ("late", 55, 1))
        early = self.arrivals.save(1, "2026-10-16T17:01", 2, NOW)
        self.assertEqual((early["status"], early["score"], early["late_minutes"]), ("on_time", 100, 0))
        self.appointment("2026-10-17T04:00:00+00:00")  # midnight Eastern
        overnight = self.arrivals.save(1, "2026-10-17T00:05", 3, NOW)
        self.assertEqual((overnight["score"], overnight["late_minutes"]), (55, 5))
        self.appointment("2026-10-17T04:00:00")  # untrusted malformed import must not become a guessed schedule
        self.assertEqual(self.arrivals.view(self.segs[0])["status"], "pending_schedule")

    def test_missing_record_appointment_is_visible_without_a_score(self):
        self.appointment()
        value = self.arrivals.view(self.segs[0])
        self.assertEqual((value["version"], value["status"], value["score"]), (0, "unrecorded", None))
        self.assertEqual(value["scheduled_arrival_et"], "2026-10-17T17:00")

    def test_tms_snapshots_and_rule_evaluation_are_unchanged(self):
        before = [tuple(r) for r in self.store.db.execute("SELECT * FROM segments ORDER BY id")]
        findings = rules.evaluate(self.store.segment(1), NOW)
        self.arrivals.save(1, "2026-10-17T17:00", 0, NOW)
        self.assertEqual(before, [tuple(r) for r in self.store.db.execute("SELECT * FROM segments ORDER BY id")])
        self.assertEqual(findings, rules.evaluate(self.store.segment(1), NOW))
        for table in ("alerts", "alert_events", "notifications", "notification_keys"):
            self.assertEqual(self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_invalid_version_and_segment_do_not_write(self):
        for version in (None, True, "0", -1, 0.5, 9223372036854775807):
            with self.subTest(version=version), self.assertRaises(driver_arrivals.ValidationError):
                self.arrivals.save(1, "2026-10-17T17:00", version, NOW)
        with self.assertRaises(driver_arrivals.NotFoundError):
            self.arrivals.save(99, "2026-10-17T17:00", 0, NOW)
        with self.assertRaises(driver_arrivals.ValidationError):
            self.arrivals.save(3, "2026-10-17T17:00", 0, NOW)
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM driver_arrival_events").fetchone()[0], 0)

    def test_failed_audit_write_rolls_back_arrival(self):
        self.store.db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON driver_arrival_events "
                              "BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.arrivals.save(1, "2026-10-17T17:00", 0, NOW)
        self.assertIsNone(self.arrivals.record("MT-DRIVER-TEST"))
        self.assertFalse(self.store.db.in_transaction)

    def test_two_connections_cannot_overwrite_the_same_version(self):
        other = Store(self.path)
        self.addCleanup(other.db.close)
        ready = threading.Barrier(2)

        def save(repo):
            ready.wait()
            try:
                return repo.save(1, "2026-10-17T17:00", 0, NOW)["version"]
            except driver_arrivals.ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, (self.arrivals, other.driver_arrivals)))
        self.assertCountEqual(results, [1, "conflict"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM driver_arrival_events").fetchone()[0], 1)

    def test_detail_and_boards_decorate_without_reading_credentials(self):
        self.arrivals.save(1, "2026-10-17T17:00", 0, NOW)
        self.store.upsert_segments([segment(2, origin="RIC01", destination="BWI.H",
                                           actualArrivalTime="2026-10-17 17:00:00")], NOW)
        with mock.patch.object(web.config, "secret", return_value=None):
            detail = web.segment_detail(self.store, 2, NOW)
            state = web.build_state(self.store, NOW)
        self.assertTrue(state["driver_arrival_enabled"])
        self.assertTrue(detail["driver_arrival_enabled"])
        self.assertIsNone(detail["driver_arrival"])
        for board in ("receiving", "departures"):
            rows = [r for r in state[board] if r["taskNo"] == "MT-DRIVER-TEST"]
            self.assertTrue(rows)
            if board == "departures":
                self.assertTrue(all(r["driver_arrival"]["version"] == 1 for r in rows))
            else:
                self.assertTrue(all(not r["driver_arrival"]["eligible"] for r in rows))

    def test_arrival_rejects_other_routes_chain_and_voided_tasks(self):
        invalid = [dict(segment(), destination="JFK.H"), dict(segment(), sortNum=2),
                   dict(segment(), taskName="EWR.H-BWI.H-ORF01"), dict(segment(), sortNum=None)]
        for value in invalid:
            self.store.upsert_segments([value], NOW)
            with self.subTest(value=value), self.assertRaises(driver_arrivals.ValidationError):
                self.arrivals.save(1, "2026-10-17T17:00", 0, NOW)
        self.store.upsert_segments([segment()], NOW)
        self.store.db.execute("UPDATE segments SET validity='VOIDED' WHERE id=1")
        with self.assertRaises(driver_arrivals.ValidationError):
            self.arrivals.save(1, "2026-10-17T17:00", 0, NOW)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM driver_arrivals").fetchone()[0], 0)


class ArrivalHttpTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.db.close)
        self.store.upsert_segments([segment()], NOW)

    def post(self, body=None, headers=None, readonly=False, token="", path="/api/driver-arrival/1", raw=None,
             server_address=("127.0.0.1", 8768), local_address=("127.0.0.1", 8768)):
        handler = object.__new__(web.Handler)
        handler.path, handler.store = path, self.store
        handler.server = SimpleNamespace(server_address=server_address)
        handler.connection = SimpleNamespace(getsockname=lambda: local_address)
        data = raw if raw is not None else json.dumps(body or {"actual_arrival_et": "2026-10-17T17:00", "version": 0}).encode()
        handler.rfile = io.BytesIO(data)
        handler.headers = Message()
        values = {"Host": "localhost:8768", "Origin": "http://localhost:8768", "Content-Type": "application/json",
                  "Content-Length": str(len(data))}
        values.update(headers or {})
        for key, value in values.items():
            if value is not None:
                handler.headers[key] = value
        handler._send = mock.Mock()
        with mock.patch.dict(os.environ, {"BWI_DASHBOARD_READONLY": "1" if readonly else "0"}), \
                mock.patch.object(config, "ADMIN_TOKEN", token), mock.patch.object(web, "utcnow", return_value=NOW), \
                mock.patch.object(config, "secret", side_effect=AssertionError("must not read credentials")):
            handler.do_POST()
        return handler._send.call_args.args

    def test_save_conflict_and_clear(self):
        code, first = self.post()
        self.assertEqual(code, 200)
        self.assertEqual(first["driver_arrival"]["version"], 1)
        code, conflict = self.post()
        self.assertEqual(code, 409)
        self.assertEqual(conflict["driver_arrival"]["version"], 1)
        code, cleared = self.post({"actual_arrival_et": None, "version": 1})
        self.assertEqual((code, cleared["driver_arrival"]["version"]), (200, 2))
        self.assertIsNone(cleared["driver_arrival"]["actual_arrival_et"])

    def test_readonly_admin_and_cross_origin_are_enforced(self):
        self.assertEqual(self.post(readonly=True)[0], 403)
        self.assertEqual(self.post(token="test-admin")[0], 403)
        for origin in (None, "null", "https://localhost:8768", "http://evil.example", "http://localhost:8769",
                       "http://localhost:8768/path", "http://user@localhost:8768"):
            with self.subTest(origin=origin):
                self.assertEqual(self.post(headers={"Origin": origin})[0], 403)
        self.assertEqual(self.post(token="test-admin", headers={"X-Admin-Token": "test-admin"})[0], 200)

    def test_json_body_and_version_validation(self):
        self.assertEqual(self.post(headers={"Content-Type": "text/plain"})[0], 415)
        for raw in (b"[]", b"null", b"{", b'{}', b'{"actual_arrival_et":null}', b'\xff'):
            with self.subTest(raw=raw):
                self.assertEqual(self.post(raw=raw)[0], 400)
        self.assertEqual(self.post({"actual_arrival_et": "2026-10-17T17:00", "version": True})[0], 400)
        self.assertEqual(self.post({"actual_arrival_et": "2026-10-17T18:01", "version": 0})[0], 400)
        self.assertEqual(self.post(headers={"Content-Length": "9999"})[0], 400)
        self.assertEqual(self.post(headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.post(path="/api/driver-arrival/nope")[0], 400)
        self.assertEqual(self.post(path="/api/driver-arrival/99")[0], 404)

    def test_matching_host_and_origin_cannot_authorize_a_rebound_domain(self):
        for authority in ("untrusted.example:8768", "192.0.2.20:8768", "localhost:8769", "127.0.0.1:80"):
            with self.subTest(authority=authority):
                self.assertEqual(self.post(headers={"Host": authority, "Origin": "http://" + authority})[0], 403)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM driver_arrivals").fetchone()[0], 0)

    def test_listener_loopback_aliases_require_the_real_port(self):
        for version, authority in enumerate(("localhost:8768", "127.0.0.1:8768", "[::1]:8768")):
            with self.subTest(authority=authority):
                self.assertEqual(self.post({"actual_arrival_et": "2026-10-17T17:00", "version": version},
                                           headers={"Host": authority, "Origin": "http://" + authority})[0], 200)

    def test_lan_and_wildcard_bindings_use_actual_server_or_connection_address(self):
        trusted = {"Host": "192.0.2.20:8768", "Origin": "http://192.0.2.20:8768"}
        self.assertEqual(self.post(headers=trusted, server_address=("192.0.2.20", 8768))[0], 200)
        self.assertEqual(self.post({"actual_arrival_et": "2026-10-17T17:00", "version": 1}, headers=trusted,
                                   server_address=("0.0.0.0", 8768), local_address=("192.0.2.20", 8768))[0], 200)
        for authority in ("untrusted.example:8768", "192.0.2.21:8768", "0.0.0.0:8768"):
            with self.subTest(authority=authority):
                headers = {"Host": authority, "Origin": "http://" + authority}
                self.assertEqual(self.post(headers=headers, server_address=("0.0.0.0", 8768),
                                           local_address=("192.0.2.20", 8768))[0], 403)


if __name__ == "__main__":
    unittest.main()
