"""Driver sync uses synthetic records and an injected transport; never live TMS."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from bwi_monitor import driver_scores, driver_sync, tms


NOW = datetime(2026, 10, 17, 20, tzinfo=timezone.utc)


def segment(number=1, day=None, **extra):
    day = day or NOW.date()
    result = {"id": number, "taskNo": f"MT{number}", "sortNum": 1,
              "placeOfOrigin": "BWI.H", "destination": "ORF01", "taskName": "BWI.H-ORF01",
              "plannedDepartureTime": str(day + timedelta(days=1)) + " 23:00:00"}
    result.update(extra)
    return result


def remote(task_no="MT1", **extra):
    result = {"id": int(task_no[2:]), "taskNo": task_no, "placeOfOrigin": "BWI.H", "destination": "ORF01", "taskStatus": 3,
              "mainDriver": "Example Driver", "mainDriverTelephone": "+1 (202) 555-0101",
              "supplierName": "Example Carrier", "licensePlateNo": "TESTPLATE",
              "createByName": "Operator Must Not Identify Driver", "unused": "DO NOT CACHE"}
    result.update(extra)
    return result


class FakeTransport:
    def __init__(self, response=None, gate=None, detail=None):
        self.response = response or (lambda task: {"records": [remote(task)]})
        self.gate = gate
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.closed = 0
        self.detail = detail
        self.detail_calls = []

    def __call__(self, auth):
        outer = self

        class Response:
            status_code = 200

            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                pass

            def json(self):
                return {"code": 200, "data": self.data}

        class Session:
            def get(self, url, params, headers, timeout):
                with outer.lock:
                    outer.detail_calls.append((url, params, headers, timeout))
                if outer.detail:
                    return Response(outer.detail(params["id"]))
                record = next(r for r in outer.response(f'MT{params["id"]}')["records"] if r["id"] == params["id"])
                return Response({**record, "lineVO": {"placeOfOrigin": record["placeOfOrigin"], "destination": record["destination"]}})

            def close(self):
                with outer.lock:
                    outer.closed += 1

        class Client:
            session = Session()

            def _page(self, filters, page_num, page_size, url):
                with outer.lock:
                    outer.calls.append((dict(filters), page_num, page_size, url, auth))
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                    outer.started.set()
                try:
                    if outer.gate is not None and not outer.gate.wait(5):
                        raise RuntimeError("test gate timeout")
                    return outer.response(filters["taskNo"])
                finally:
                    with outer.lock:
                        outer.active -= 1

        return Client()


class DriverIdentityTests(unittest.TestCase):
    def test_key_requires_full_phone_and_normalized_name(self):
        key = driver_sync.driver_key("  EXAMPLE   Driver ", "(202) 555-0101")
        self.assertEqual(key, driver_sync.driver_key("Example Driver", "+1 202-555-0101"))
        self.assertRegex(key, r"^tms:[a-f0-9]{64}$")
        self.assertNotEqual(key, driver_sync.driver_key("Another Driver", "2025550101"))
        for phone in (None, "", "5550101", "0000000000", "1111111111", "2025550101 ext 3", "1201555"):
            with self.subTest(phone=phone):
                self.assertIsNone(driver_sync.driver_key("Example Driver", phone))
        self.assertIsNone(driver_sync.driver_key("", "2025550101"))
        self.assertIsNone(driver_sync.driver_key("---", "2025550101"))
        self.assertEqual(driver_sync.normalized_phone("+44 20 7946 0018"), "+442079460018")


class DriverSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.auth = Path(self.temp.name) / "test-auth"
        self.auth.write_text("test-token-one", encoding="utf-8")
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE segments(id INTEGER PRIMARY KEY, task_no TEXT, data TEXT, validity TEXT)")
            db.executescript(driver_scores.SCHEMA)

    def add(self, seg=None, validity=None):
        seg = seg or segment()
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO segments(id,task_no,data,validity) VALUES(?,?,?,?)",
                       (seg["id"], seg["taskNo"], json.dumps(seg), validity))

    def cached(self, task_no="MT1", at=None, source=driver_sync.PROFILE_SOURCE, **extra):
        row = {"task_no": task_no, "driver_key": "tms:old", "driver_name": "Old Driver",
               "driver_phone": "2025550100", "supplier_name": "Old Carrier", "vehicle_plate": "OLDPLATE",
               "source": source, "updated_at": (at or NOW - timedelta(minutes=20)).isoformat()}
        row.update(extra)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO driver_task_profiles VALUES(?,?,?,?,?,?,?,?)", tuple(row.values()))

    def profiles(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return {r["task_no"]: dict(r) for r in db.execute("SELECT * FROM driver_task_profiles")}

    def sync(self, transport=None):
        sync = driver_sync.DriverSync(self.path, self.auth, transport or FakeTransport())
        self.addCleanup(sync.stop)
        return sync

    def run_batch(self, sync, now=NOW):
        with mock.patch.object(driver_sync, "utcnow", return_value=now):
            sync.start()
            self.assertTrue(sync._done.wait(10), "background batch did not finish")
        self.assertFalse(sync.status()["running"])
        return sync.status()

    def test_exact_read_only_task_lookup_and_minimal_profile(self):
        self.add()
        transport = FakeTransport(lambda task: {"records": [remote("MT999"), remote(task)]})
        sync = self.sync(transport)
        status = self.run_batch(sync)
        self.assertEqual(transport.calls[0][:4], ({"taskNo": "MT1"}, 1, 10, tms.TASK_URL))
        self.assertEqual(transport.detail_calls, [(driver_sync.TASK_DRIVER_DETAIL_URL, {"id": 1}, {"X-Need-Decrypt": "true"}, 30)])
        self.assertEqual((status["profiles_updated"], status["remaining"], status["error"]), (1, 0, None))
        profile = self.profiles()["MT1"]
        self.assertEqual(profile["driver_name"], "Example Driver")
        self.assertEqual(profile["driver_key"], driver_sync.driver_key(profile["driver_name"], profile["driver_phone"]))
        self.assertNotIn("Operator", json.dumps(profile))
        self.assertNotIn("DO NOT CACHE", json.dumps(profile))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM segments").fetchone()[0]), segment())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM driver_score_reviews").fetchone()[0], 0)

    def test_remote_empty_values_clear_all_old_profile_fields(self):
        self.add()
        self.cached()
        transport = FakeTransport(lambda task: {"records": [remote(task, mainDriver=None, mainDriverTelephone=None,
                                                                    supplierName=None, licensePlateNo=None)]})
        self.run_batch(self.sync(transport))
        profile = self.profiles()["MT1"]
        self.assertIsNone(profile["driver_key"])
        self.assertTrue(all(profile[k] == "" for k in ("driver_name", "driver_phone", "supplier_name", "vehicle_plate")))

    def test_registered_driver_comes_from_task_detail_not_empty_list_or_vehicle_defaults(self):
        self.add()
        detail = {"id": 1, "taskStatus": 3, "lineVO": {"placeOfOrigin": "BWI.H", "destination": "ORF01"},
                  "mainDriver": "Task Driver", "mainDriverTelephone": "2025550142",
                  "vehicleVO": {"mainDriver": "Wrong Vehicle Driver", "mainDriverTelephone": "2025550199"}}
        transport = FakeTransport(lambda task: {"records": [remote(task, mainDriver=None, mainDriverTelephone=None)]},
                                  detail=lambda task_id: detail)
        self.run_batch(self.sync(transport))
        self.assertEqual(self.profiles()["MT1"]["driver_name"], "Task Driver")
        self.assertEqual(self.profiles()["MT1"]["driver_phone"], "2025550142")
        detail.update(mainDriver=None, mainDriverTelephone=None)
        self.run_batch(self.sync(transport), now=NOW + timedelta(minutes=11))
        self.assertIsNone(self.profiles()["MT1"]["driver_key"])
        self.assertEqual(self.profiles()["MT1"]["driver_name"], "")

    def test_detail_task_identity_mismatch_or_failure_preserves_previous_profile(self):
        self.add()
        self.cached()
        before = self.profiles()
        for detail in (lambda task_id: {"id": 999}, lambda task_id: None):
            with self.subTest(detail=detail):
                status = self.run_batch(self.sync(FakeTransport(detail=detail)))
                self.assertIn("更新失败", status["error"])
                self.assertEqual(self.profiles(), before)

    def test_detail_scope_change_and_cancellation_remove_association(self):
        for number in (1, 2):
            self.add(segment(number))
            self.cached(f"MT{number}")
        def detail(task_id):
            return {"id": task_id, "taskStatus": 6 if task_id == 2 else 3,
                    "lineVO": {"placeOfOrigin": "EWR.H", "destination": "ORF01"},
                    "mainDriver": "Wrong Driver", "mainDriverTelephone": "2025550199"}
        self.run_batch(self.sync(FakeTransport(detail=detail)))
        self.assertEqual(self.profiles()["MT1"]["source"], driver_sync.MISMATCH_SOURCE)
        self.assertEqual(self.profiles()["MT2"]["source"], driver_sync.CANCELLED_SOURCE)
        self.assertTrue(all(r["driver_key"] is None for r in self.profiles().values()))

    def test_bwi_round_trip_uses_first_branch_not_final_return_station(self):
        self.add(segment(destination="RIC01", taskName="BWI.H-RIC01-ORF01-BWI.H(00:00)"))
        def detail(task_id):
            return {"id": task_id, "taskStatus": 3, "mainDriver": "Round Trip Driver", "mainDriverTelephone": "2025550142",
                    "lineVO": {"placeOfOrigin": "BWI.H", "destination": "BWI.H", "lineName": "BWI.H-RIC01-ORF01-BWI.H"},
                    "taskLinePointVOList": [{"sortNum": n, "meridianStopPoint": station} for n, station in
                                            enumerate(["BWI.H", "RIC01", "ORF01", "BWI.H"], 1)]}
        transport = FakeTransport(lambda task: {"records": [remote(task, destination="BWI.H", taskName="BWI.H-RIC01-ORF01-BWI.H(00:00)")]}, detail=detail)
        self.run_batch(self.sync(transport))
        self.assertEqual(self.profiles()["MT1"]["driver_name"], "Round Trip Driver")
        self.assertIsNotNone(self.profiles()["MT1"]["driver_key"])
        altered = detail(1)
        altered["taskLinePointVOList"][0]["meridianStopPoint"] = "EWR.H"
        transport.detail = lambda task_id: altered
        self.run_batch(self.sync(transport), now=NOW + timedelta(minutes=11))
        self.assertEqual(self.profiles()["MT1"]["source"], driver_sync.MISMATCH_SOURCE)

    def test_name_without_phone_is_displayed_but_never_merged(self):
        self.add()
        transport = FakeTransport(lambda task: {"records": [remote(task, mainDriverTelephone=None)]})
        self.run_batch(self.sync(transport))
        self.assertEqual(self.profiles()["MT1"]["driver_name"], "Example Driver")
        self.assertIsNone(self.profiles()["MT1"]["driver_key"])

    def test_local_scope_rejects_inbound_intermediate_voided_unverified_and_wrong_origin(self):
        self.add(segment(1))
        self.add(segment(2, placeOfOrigin="EWR.H", destination="BWI.H"))
        self.add(segment(3, sortNum=2))
        self.add(segment(4), "VOIDED")
        self.add(segment(5), "UNVERIFIED")
        self.add(segment(6, taskStatus="6"))
        self.add(segment(7, destination="JFK.H"))
        self.add(segment(8, taskName="EWR.H-BWI.H-ORF01"))
        self.add(segment(9, sortNum=None))
        self.add(segment(10))
        self.add(segment(11, taskNo="MT10", placeOfOrigin="EWR.H", destination="BWI.H", sortNum=1))
        transport = FakeTransport()
        self.run_batch(self.sync(transport))
        self.assertEqual([call[0] for call in transport.calls], [{"taskNo": "MT1"}])

    def test_exact_task_mismatch_duplicates_and_cancellation_clear_associations(self):
        for i in range(1, 6):
            self.add(segment(i))
            self.cached(f"MT{i}")
        def response(task):
            return {"records": {
                "MT1": [remote("MT999")],
                "MT2": [remote(task), remote(task)],
                "MT3": [remote(task, placeOfOrigin="EWR.H")],
                "MT4": [remote(task, destination="DCA01")],
                "MT5": [remote(task, taskStatus="6")],
            }[task]}
        self.run_batch(self.sync(FakeTransport(response)))
        profiles = self.profiles()
        self.assertTrue(all(r["driver_key"] is None and not r["driver_name"] for r in profiles.values()))
        self.assertEqual(profiles["MT3"]["source"], driver_sync.MISMATCH_SOURCE)
        self.assertEqual(profiles["MT4"]["source"], driver_sync.MISMATCH_SOURCE)
        self.assertEqual(profiles["MT5"]["source"], driver_sync.CANCELLED_SOURCE)

    def test_network_errors_preserve_existing_values_and_never_expose_exception_content(self):
        self.add()
        self.cached()
        before = self.profiles()
        def fail(task):
            raise tms.FeedError("sensitive-name sensitive-phone sensitive-token")
        transport = FakeTransport(fail)
        sync = self.sync(transport)
        status = self.run_batch(sync)
        self.assertEqual(self.profiles(), before)
        self.assertEqual((status["profiles_updated"], status["remaining"]), (0, 1))
        self.assertIn("更新失败", status["error"])
        self.assertNotIn("sensitive", json.dumps(status))
        # Repeated clicks during backoff neither conceal the error nor requery.
        repeated = self.run_batch(sync)
        self.assertEqual(repeated["error"], status["error"])
        self.assertEqual(len(transport.calls), 1)

    def test_cache_window_priority_dedup_and_old_profiles(self):
        self.add(segment(1, day=NOW.date()))
        self.add(segment(2, day=NOW.date() + timedelta(days=2)))
        self.add(segment(3, day=NOW.date() + timedelta(days=3)))
        self.add(segment(4, day=NOW.date() - timedelta(days=29)))
        self.add(segment(5, day=NOW.date() - timedelta(days=30)))
        self.add(segment(6, day=NOW.date() - timedelta(days=3)))
        self.cached("MT6", at=NOW - timedelta(hours=23))
        self.add(segment(7))
        self.cached("MT7", at=NOW - timedelta(seconds=599))
        self.add(segment(8, taskNo="MT1"))
        transport = FakeTransport()
        sync = self.sync(transport)
        self.run_batch(sync)
        self.assertEqual([call[0]["taskNo"] for call in transport.calls], ["MT2", "MT1", "MT4"])
        self.run_batch(sync)
        self.assertEqual(len(transport.calls), 3)

    def test_auth_file_is_reread_each_actual_batch(self):
        self.add()
        transport = FakeTransport()
        sync = self.sync(transport)
        self.run_batch(sync)
        self.auth.write_text("test-token-two", encoding="utf-8")
        self.run_batch(sync, now=NOW + timedelta(minutes=11))
        self.assertEqual([call[4] for call in transport.calls], ["test-token-one", "test-token-two"])

    def test_missing_auth_does_not_attempt_transport(self):
        self.add()
        self.auth.unlink()
        transport = FakeTransport()
        status = self.run_batch(self.sync(transport))
        self.assertIn("凭证", status["error"])
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.profiles(), {})

    def test_old_history_refreshes_daily_instead_of_freezing_changed_driver_forever(self):
        self.add(segment(1, day=NOW.date() - timedelta(days=4)))
        self.cached(at=NOW - timedelta(hours=25))
        transport = FakeTransport()
        self.run_batch(self.sync(transport))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.profiles()["MT1"]["driver_name"], "Example Driver")

    def test_maximum_batch_and_remaining_backlog(self):
        for i in range(1, 126):
            self.add(segment(i, day=NOW.date() - timedelta(days=3)))
        transport = FakeTransport()
        sync = self.sync(transport)
        first = self.run_batch(sync)
        self.assertEqual((first["profiles_updated"], first["remaining"]), (120, 5))
        second = self.run_batch(sync)
        self.assertEqual((second["profiles_updated"], second["remaining"]), (5, 0))
        self.assertEqual(len(transport.calls), 125)
        self.assertLessEqual(transport.max_active, 3)

    def test_start_is_nonblocking_mutex_and_stop_prevents_queued_requests_and_writes(self):
        for i in range(1, 8):
            self.add(segment(i))
        gate = threading.Event()
        self.addCleanup(gate.set)
        transport = FakeTransport(gate=gate)
        sync = self.sync(transport)
        with mock.patch.object(driver_sync, "utcnow", return_value=NOW):
            first = sync.start()
            self.assertTrue(first["running"])
            self.assertTrue(transport.started.wait(2))
            worker = sync._worker
            self.assertTrue(sync.start()["running"])
            self.assertIs(sync._worker, worker)
            sync.stop()
            gate.set()
            self.assertTrue(sync._done.wait(5))
        self.assertLessEqual(len(transport.calls), 3)
        self.assertEqual(self.profiles(), {})
        self.assertFalse(sync.start()["running"])

    def test_scope_change_while_request_in_flight_does_not_associate_driver(self):
        self.add()
        def response(task):
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE segments SET validity='VOIDED'")
            return {"records": [remote(task)]}
        self.run_batch(self.sync(FakeTransport(response)))
        self.assertEqual(self.profiles(), {})

    def test_stop_while_begin_waits_for_sqlite_lock_prevents_late_write(self):
        self.add()
        sync = self.sync()
        begin_attempted = threading.Event()
        original_connect = sync._connect

        def traced_connect():
            db = original_connect()
            db.set_trace_callback(lambda statement: begin_attempted.set()
                                  if statement == "BEGIN IMMEDIATE" else None)
            return db

        blocker = sqlite3.connect(self.path)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(driver_sync, "utcnow", return_value=NOW), \
                    mock.patch.object(sync, "_connect", side_effect=traced_connect):
                sync.start()
                self.assertTrue(begin_attempted.wait(2))
                sync.stop()
                blocker.rollback()
                self.assertTrue(sync._done.wait(5))
        finally:
            blocker.rollback()
        self.assertEqual(self.profiles(), {})
        self.assertEqual(sync.status()["profiles_updated"], 0)

    def test_stop_and_profile_commit_are_serialized(self):
        self.add()
        sync = self.sync()
        commit_entered, commit_release, stop_called, stop_returned = (
            threading.Event() for _ in range(4))
        original_connect = sync._connect

        def traced_connect():
            db = original_connect()
            def trace(statement):
                if statement == "COMMIT":
                    commit_entered.set()
                    if not commit_release.wait(3):
                        raise AssertionError("test commit gate timeout")
            db.set_trace_callback(trace)
            return db

        def stop():
            stop_called.set()
            sync.stop()
            stop_returned.set()

        with mock.patch.object(driver_sync, "utcnow", return_value=NOW), \
                mock.patch.object(sync, "_connect", side_effect=traced_connect):
            sync.start()
            self.assertTrue(commit_entered.wait(2))
            stopper = threading.Thread(target=stop, daemon=True)
            stopper.start()
            self.assertTrue(stop_called.wait(1))
            self.assertFalse(stop_returned.wait(0.05))
            commit_release.set()
            self.assertTrue(stop_returned.wait(2))
            self.assertTrue(sync._done.wait(2))
        self.assertEqual(len(self.profiles()), 1)  # committed before stop was allowed to return

    def test_newer_profile_is_not_overwritten_by_earlier_request(self):
        self.add()
        def response(task):
            self.cached(at=NOW + timedelta(seconds=1), driver_name="Newer Result")
            return {"records": [remote(task)]}
        self.run_batch(self.sync(FakeTransport(response)))
        self.assertEqual(self.profiles()["MT1"]["driver_name"], "Newer Result")

    def test_periodic_scheduler_finishes_backlog_without_waiting_normal_interval(self):
        self.add(segment(1))
        self.add(segment(2))
        second_request = threading.Event()
        def response(task):
            if task == "MT1":
                second_request.set()
            return {"records": [remote(task)]}
        transport = FakeTransport(response)
        sync = self.sync(transport)
        with mock.patch.object(driver_sync, "utcnow", return_value=NOW), \
                mock.patch.object(driver_sync, "MAX_BATCH", 1), \
                mock.patch.object(driver_sync, "CONTINUE_SECONDS", 0.01):
            sync.start_periodic(interval=600)
            self.assertTrue(second_request.wait(2))
            self.assertTrue(sync._done.wait(2))
            sync.stop()
            sync._periodic.join(1)
        self.assertFalse(sync._periodic.is_alive())
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sync.status()["remaining"], 0)

    def test_business_window_uses_the_same_five_am_boundary_as_scores(self):
        now = datetime(2026, 10, 17, 8, 59, tzinfo=timezone.utc)  # 04:59 Eastern: operating day October 16
        self.add(segment(1, day=NOW.date() - timedelta(days=30)))  # September 17 is the first included day
        self.add(segment(2, day=NOW.date() + timedelta(days=2)))  # October 19 is beyond day + 2
        transport = FakeTransport()
        self.run_batch(self.sync(transport), now=now)
        self.assertEqual([call[0] for call in transport.calls], [{"taskNo": "MT1"}])


if __name__ == "__main__":
    unittest.main()
