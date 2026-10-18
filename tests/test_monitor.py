"""Acceptance tests mapped to SRS §13. Run: python3 -m unittest discover tests"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from unittest import mock

from bwi_monitor import compliance, config, departures, feishu, rules, shift, web
from bwi_monitor.engine import Engine
from bwi_monitor.rules import EASTERN
from bwi_monitor.notify import Notifier
from bwi_monitor.shift import Shifts
from bwi_monitor.store import Store


def et(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)


def dep_seg(**kw):
    """BWI departure segment. actualDepartureTime=... in tests means a manual (TMS) departure record."""
    seg = {"id": 1, "taskNo": "MT1", "placeOfOrigin": "BWI.H", "destination": "EWR.H", "licensePlateNo": "P1",
           "plannedDepartureTime": "2026-10-11 12:00:00", "actualDepartureTime": None, "actualArrivalTime": None,
           "manualActualDepartureTimeStrFormat": None, "fenceActualDepartureTimeStrFormat": None}
    if "actualDepartureTime" in kw and "manualActualDepartureTimeStrFormat" not in kw:
        kw["manualActualDepartureTimeStrFormat"] = kw["actualDepartureTime"]
    seg.update(kw)
    return seg


def unload_seg(**kw):
    seg = {"id": 2, "taskNo": "MT2", "placeOfOrigin": "EWR.H", "destination": "BWI.H",
           "plannedDepartureTime": "2026-10-11 08:00:00", "actualDepartureTime": "2026-10-11 08:00:00",
           "actualArrivalTime": "2026-10-11 12:00:00", "unloadCompleteTimeStrFormat": None,
           "handlingModeStr": "One loading and one unloading", "loadBagCount": 28, "unLoadBagCount": 28}
    seg.update(kw)
    return seg


_MISSING = object()


def stage(seg, now, kind, ctx=None, default=_MISSING):
    found = [f for f in rules.evaluate(seg, now, ctx) if f.alert_type == kind]
    if not found and default is not _MISSING:
        return default
    return found[0]


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.reminders = 0

    def send(self, alert_id, seg, f, kind, now):
        self.sent.append((seg["id"], f.alert_type, f.stage, kind))

    def send_shift_reminder(self, now):
        self.reminders += 1


class DepartureRules(unittest.TestCase):
    """Non-branch BWI routes (EWR, JFK, PNE, ...): deadline = TMS planned departure; manual departure completes."""
    now = et("2026-10-11 13:00:00")

    def test_early_equal_late(self):  # §13.1
        self.assertEqual(stage(dep_seg(actualDepartureTime="2026-10-10 09:00:00"), self.now, "BRANCH_DEPARTURE").stage, "OK")
        self.assertEqual(stage(dep_seg(actualDepartureTime="2026-10-11 12:00:00"), self.now, "BRANCH_DEPARTURE").stage, "OK")
        late = stage(dep_seg(actualDepartureTime="2026-10-11 12:00:01"), self.now, "BRANCH_DEPARTURE")
        self.assertEqual((late.stage, late.minutes), ("LATE", 0))
        self.assertEqual(late.evidence["deadlineKind"], "tms")

    def test_countdown_stages(self):  # §13.2 (clock-driven, no new data): 30-min reminder, deadline alert
        seg = dep_seg()
        self.assertEqual(stage(seg, et("2026-10-11 11:00:00"), "BRANCH_DEPARTURE").stage, "WAITING")
        f = stage(seg, et("2026-10-11 11:40:00"), "BRANCH_DEPARTURE")
        self.assertEqual((f.stage, f.warn_level), ("WARN", 30))
        self.assertEqual(stage(seg, et("2026-10-11 12:00:00"), "BRANCH_DEPARTURE").stage, "WARN")  # exactly at deadline
        self.assertEqual(stage(seg, et("2026-10-11 12:00:30"), "BRANCH_DEPARTURE").stage, "OVERDUE")

    def test_gps_alone_does_not_complete(self):
        seg = dep_seg(fenceActualDepartureTimeStrFormat="2026-10-11 11:30:00 (Eastern Time)")
        self.assertEqual(stage(seg, self.now, "BRANCH_DEPARTURE").stage, "OVERDUE")

    def test_missing_planned_is_data_check(self):
        f = rules.evaluate(dep_seg(plannedDepartureTime=None), self.now, compliance=False)
        self.assertEqual([x.alert_type for x in f], ["DATA:NO_PLANNED_DEPARTURE"])

    def test_arrived_not_departed(self):
        f = stage(dep_seg(actualArrivalTime="2026-10-11 12:30:00"), self.now, "OP:DEPARTURE")
        self.assertEqual((f.stage, f.evidence["opStatus"]), ("MISSING", "MISSING"))  # now a compliance finding


def obs(**kw):
    """Poll observations, e.g. done_seen="12:59" (times on 2026-10-11 unless a full date is given)."""
    out = {}
    for k, v in kw.items():
        out[k] = (et(v) if len(v) > 8 else et(f"2026-10-11 {v}:00")).astimezone(timezone.utc).isoformat()
    return out


MULTI = "Multiple loadings and multiple unloadings"


class UnloadRules(unittest.TestCase):
    """2026-10-17: first scan for every truck by MAX(arrival + 60, 14:00) (arrival before 19:00) or 14:00 next day
    (arrival at/after 19:00); plus a reminder 60 min after arrival when the truck is not unloaded."""
    now = et("2026-10-11 15:00:00")

    def test_first_scan_deadline(self):
        for arrive, expected in (("2026-10-11 10:00:00", "2026-10-11 14:00:00"),   # 10:00 -> 14:00
                                 ("2026-10-11 03:00:00", "2026-10-11 14:00:00"),   # overnight -> 14:00
                                 ("2026-10-11 12:30:00", "2026-10-11 14:00:00"),
                                 ("2026-10-11 13:30:00", "2026-10-11 14:30:00"),
                                 ("2026-10-11 18:30:00", "2026-10-11 19:30:00"),
                                 ("2026-10-11 18:59:00", "2026-10-11 19:59:00"),
                                 ("2026-10-11 19:00:00", "2026-10-12 14:00:00"),   # at 19:00 -> next day 14:00
                                 ("2026-10-11 20:00:00", "2026-10-12 14:00:00"),
                                 ("2026-10-11 23:30:00", "2026-10-12 14:00:00")):
            for mode in ("One loading and one unloading", MULTI):
                f = stage(unload_seg(actualArrivalTime=arrive, handlingModeStr=mode), et(arrive), "UNLOAD")
                self.assertEqual(f.deadline, et(expected), (arrive, mode))

    def test_first_scan_is_the_compliance_check_for_single_stop_too(self):
        seg = unload_seg(actualArrivalTime="2026-10-11 13:30:00", unLoadBagCount=10,
                         _obs=obs(first_poll="13:20", start_seen="14:10"))
        self.assertEqual(stage(seg, self.now, "UNLOAD").stage, "OK")  # first scan seen 14:10 <= 14:30
        late = unload_seg(actualArrivalTime="2026-10-11 13:30:00", unLoadBagCount=None, _obs=obs(first_poll="13:20"))
        f = stage(late, et("2026-10-11 14:31:00"), "UNLOAD")
        self.assertEqual(f.stage, "OVERDUE")

    def test_reminders_before_first_scan_deadline(self):
        seg = unload_seg(actualArrivalTime="2026-10-11 10:00:00", unLoadBagCount=None, _obs=obs(first_poll="09:50"))
        self.assertEqual(stage(seg, et("2026-10-11 13:29:00"), "UNLOAD").stage, "WAITING")
        f = stage(seg, et("2026-10-11 13:30:00"), "UNLOAD")
        self.assertEqual((f.stage, f.warn_level), ("WARN", 30))
        f = stage(seg, et("2026-10-11 13:55:00"), "UNLOAD")
        self.assertEqual((f.stage, f.warn_level, f.priority), ("WARN", 5, "high"))
        self.assertEqual(stage(seg, et("2026-10-11 14:01:00"), "UNLOAD").stage, "OVERDUE")

    def test_sixty_minute_reminder_single_stop(self):
        seg = unload_seg(actualArrivalTime="2026-10-11 10:00:00", unLoadBagCount=27, loadBagCount=28)
        self.assertEqual(stage(seg, et("2026-10-11 10:59:00"), "UNLOAD_60").stage, "WAITING")
        f = stage(seg, et("2026-10-11 11:00:00"), "UNLOAD_60")
        self.assertEqual((f.stage, f.evidence["progress60"]), ("WARN", "27/28 bags"))  # first scan done, not unloaded
        self.assertEqual(stage(seg, et("2026-10-11 11:00:00"), "UNLOAD").stage, "WAITING")  # first-scan deadline 14:00
        self.assertEqual(stage(dict(seg, unLoadBagCount=28), et("2026-10-11 11:05:00"), "UNLOAD_60").stage, "OK")

    def test_sixty_minute_reminder_multi_stop_and_any_hour(self):
        seg = unload_seg(actualArrivalTime="2026-10-11 02:00:00", handlingModeStr=MULTI, unLoadBagCount=None)
        f = stage(seg, et("2026-10-11 03:00:00"), "UNLOAD_60")  # at night too
        self.assertEqual((f.stage, f.evidence["progress60"]), ("WARN", "first scan not recorded"))
        self.assertEqual(stage(dict(seg, unLoadBagCount=3), et("2026-10-11 03:05:00"), "UNLOAD_60").stage, "OK")

    def test_single_stop_resolves_when_complete(self):
        store, n = Store(":memory:"), FakeNotifier()
        engine = Engine(store, n)
        seg = unload_seg(actualArrivalTime="2026-10-11 13:30:00", unLoadBagCount=None, _obs=obs(first_poll="13:20"))
        engine.run(et("2026-10-11 14:00:00"), [seg])
        engine.run(et("2026-10-11 14:05:00"), [dict(seg, unLoadBagCount=4, _obs=obs(first_poll="13:20", start_seen="14:04"))])
        self.assertEqual([(s, k) for _, t, s, k in n.sent if t == "UNLOAD"], [("WARN", "NEW"), ("OK", "RESOLVED")])

    def test_marked_complete_short_single_stop_only(self):
        seg = unload_seg(unLoadBagCount=20, unloadCompleteTimeStrFormat="2026-10-11 12:30:00 (Eastern Time)")
        self.assertIn("DATA:MARKED_COMPLETE_SHORT", {f.alert_type for f in rules.evaluate(seg, self.now)})
        multi = dict(seg, handlingModeStr=MULTI)
        self.assertNotIn("DATA:MARKED_COMPLETE_SHORT", {f.alert_type for f in rules.evaluate(multi, self.now)})

    def test_missing_counts_never_zero(self):
        self.assertEqual(rules.unload_states(unload_seg(unLoadBagCount=None)), {"start": None, "done": None})
        f = stage(unload_seg(loadBagCount=None, unLoadBagCount=5), et("2026-10-11 13:05:00"), "UNLOAD_60")
        self.assertEqual((f.stage, f.evidence["progress60"]), ("OK", "first scan recorded"))  # unknown load: first scan

    def test_one_unload_alert_per_arrival(self):
        types = [f.alert_type for f in rules.evaluate(unload_seg(handlingModeStr=MULTI), self.now, compliance=False)]
        self.assertEqual(types, ["ARRIVAL", "UNLOAD", "UNLOAD_60"])

    def test_observations_from_counts(self):
        o = Store.observe({}, unload_seg(unLoadBagCount=None), et("2026-10-11 12:10:00"))
        self.assertEqual(o, {"first_poll": obs(x="12:10")["x"]})  # missing count: no interval evidence
        o = Store.observe(o, unload_seg(unLoadBagCount=7), et("2026-10-11 12:30:00"))  # jump to 7 = scan seen
        self.assertEqual(o["start_seen"], obs(x="12:30")["x"])

    def test_no_arrival_no_countdown(self):
        ev = lambda seg: rules.evaluate(seg, self.now, compliance=False)
        self.assertEqual(ev(unload_seg(actualArrivalTime=None, unLoadBagCount=None)), [])
        f = ev(unload_seg(actualArrivalTime=None, unLoadBagCount=None, vehicleStatusStr="Arrived"))
        self.assertEqual(f[0].alert_type, "DATA:ARRIVED_STATUS_NO_TIME")
        f = ev(unload_seg(actualArrivalTime=None))
        self.assertEqual(f[0].alert_type, "DATA:UNLOAD_WITHOUT_ARRIVAL")

    def test_arrival_source_and_conflicts(self):
        gps, manual = "2026-10-11 12:00:00 (Eastern Time)", "2026-10-11 12:25:00 (Eastern Time)"
        t, source, ev, issues = rules.arrival(unload_seg(fenceActualArrivalTimeStrFormat=gps,
                                                         manualActualArrivalTimeStrFormat=manual))
        self.assertEqual((source, ev["manualMinusGpsMinutes"], t, issues), ("GPS", 25, et("2026-10-11 12:00:00"), []))
        t, source, _, _ = rules.arrival(unload_seg(manualActualArrivalTimeStrFormat=manual))
        self.assertEqual((source, t), ("manual", et("2026-10-11 12:25:00")))


class TimeSafety(unittest.TestCase):  # §13.7
    def test_departure_by_tms_plan(self):
        seg = dep_seg(plannedDepartureTime="2026-10-11 02:00:00")
        f = stage(seg, et("2026-10-11 03:00:00"), "BRANCH_DEPARTURE")
        self.assertEqual((f.stage, f.minutes, f.deadline), ("OVERDUE", 60, et("2026-10-11 02:00:00")))
        late = stage(dep_seg(plannedDepartureTime="2026-10-11 02:00:00", actualDepartureTime="2026-10-11 02:10:00"),
                     et("2026-10-11 03:00:00"), "BRANCH_DEPARTURE")
        self.assertEqual((late.stage, late.minutes), ("LATE", 10))

    def test_dst_fall_back(self):
        # 2026-11-01 02:00 EDT -> 01:00 EST. 13:30 arrival + 60 real minutes = 14:30.
        f = stage(unload_seg(actualArrivalTime="2026-11-01 13:30:00", unLoadBagCount=None), et("2026-11-01 14:00:00"), "UNLOAD")
        self.assertEqual((f.stage, f.deadline), ("WARN", et("2026-11-01 14:30:00")))

    def test_bad_timestamp_not_silently_ok(self):
        f = rules.evaluate(dep_seg(actualDepartureTime="garbage"), et("2026-10-11 13:00:00"))
        self.assertEqual(f[0].alert_type, "DATA:BAD_TIMESTAMP")

    def test_lookback_window(self):
        self.assertEqual(rules.evaluate(dep_seg(), et("2026-10-20 12:00:00")), [])


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.store = Store(self.path)
        self.n = FakeNotifier()
        self.engine = Engine(self.store, self.n)

    def kinds(self, types=("BRANCH_DEPARTURE", "UNLOAD", "SYSTEM:FEED")):
        return [(s, k) for _, t, s, k in self.n.sent if t in types]

    def test_dedupe_escalate_resolve(self):  # §13.8
        seg = dep_seg()
        for t in ("11:40", "11:42", "11:44"):
            self.engine.run(et(f"2026-10-11 {t}:00"), [seg])
        self.assertEqual(self.kinds(), [("WARN", "NEW")])
        self.engine.run(et("2026-10-11 11:50:00"), [seg])
        self.engine.run(et("2026-10-11 12:05:00"), [seg])
        self.engine.run(et("2026-10-11 12:15:00"), [seg])
        self.assertEqual(self.kinds()[1:], [("OVERDUE", "ESCALATED")])
        late = dep_seg(actualDepartureTime="2026-10-11 12:20:00")
        self.engine.run(et("2026-10-11 12:30:00"), [late])
        self.engine.run(et("2026-10-11 12:40:00"), [late])
        self.assertEqual(self.kinds()[-1], ("LATE", "LATE"))
        self.assertEqual(len(self.kinds()), 3)
        self.assertEqual([a for a in self.store.open_alerts() if a["alert_type"] == "BRANCH_DEPARTURE"], [])

    def test_on_time_after_warning_resolves(self):
        self.engine.run(et("2026-10-11 11:40:00"), [dep_seg()])
        self.engine.run(et("2026-10-11 11:55:00"), [dep_seg(actualDepartureTime="2026-10-11 11:52:00")])
        self.assertEqual(self.kinds(), [("WARN", "NEW"), ("OK", "RESOLVED")])

    def test_late_found_without_prior_alert_sent_once(self):
        seg = dep_seg(actualDepartureTime="2026-10-11 12:30:00")
        for t in ("12:40", "12:50"):
            self.engine.run(et(f"2026-10-11 {t}:00"), [seg])
        self.assertEqual(self.kinds(), [("LATE", "LATE")])

    def test_restart_does_not_resend(self):  # §13.9
        self.engine.run(et("2026-10-11 12:05:00"), [dep_seg()])
        restarted = Engine(Store(self.path), self.n)
        restarted.run(et("2026-10-11 12:15:00"), [dep_seg()])
        self.assertEqual(len(self.kinds()), 1)

    def test_deadline_change_notifies(self):
        self.engine.run(et("2026-10-11 12:05:00"), [dep_seg()])
        self.engine.run(et("2026-10-11 12:10:00"), [dep_seg(plannedDepartureTime="2026-10-11 12:25:00")])
        self.assertEqual(self.kinds()[-1], ("WARN", "UPDATED"))

    def test_overdue_confirmed_before_notice(self):
        departed = dep_seg(actualDepartureTime="2026-10-11 11:59:00")
        engine = Engine(self.store, self.n, confirm=lambda seg: departed)
        engine.run(et("2026-10-11 12:05:00"), [dep_seg()])
        self.assertEqual(self.kinds(), [])  # fresh data shows on-time departure: no false alarm

    def test_aged_out_overdue_kept_unresolved(self):
        self.engine.run(et("2026-10-11 12:05:00"), [dep_seg()])
        self.engine.run(et("2026-10-20 12:00:00"), [dep_seg()])
        a = self.store.latest_alert(1, "BRANCH_DEPARTURE")
        self.assertEqual((a["status"], a["stage"], a["outcome"]),
                         ("closed", "OVERDUE", "unresolved: aged out of monitoring window"))

    def test_uncertain_sent_once(self):
        # first scan deadline 14:00 (arrival 12:00); first scan observed between 13:55 and 14:05 -> uncertain
        seg = unload_seg(_obs=obs(first_poll="11:50", start_last_not="13:55", start_seen="14:05"))
        for t in ("14:10", "14:20"):
            self.engine.run(et(f"2026-10-11 {t}:00"), [seg])
        self.assertEqual([k for s, k in self.kinds() if s == "UNCERTAIN"], ["UNCERTAIN"])

    def test_feishu_off_by_default(self):
        from bwi_monitor import config
        self.assertFalse(config.FEISHU_ENABLED)
        self.assertTrue(config.FEISHU_TEST_MODE)

    def test_feed_alert(self):  # §13.10
        self.engine.feed_status(et("2026-10-11 12:00:00"), "401")
        self.engine.feed_status(et("2026-10-11 12:01:00"), "401")
        self.engine.feed_status(et("2026-10-11 12:10:00"), None)
        self.assertEqual(self.kinds(), [("CHECK", "NEW"), ("OK", "RESOLVED")])


class ShiftRules(unittest.TestCase):
    """Phase 3: shift start/end are employee clicks; earliest manual arrival is a reference only."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "s.db")
        self.store = Store(self.path)
        self.n = FakeNotifier()
        self.shifts = Shifts(self.store, self.n, now=et("2026-10-11 00:00:00"))
        self.engine = Engine(self.store, self.n, shifts=self.shifts)

    def arrival_seg(self, sid, gps=None, manual=None, **kw):
        seg = unload_seg(id=sid, taskNo=f"MT{sid}", actualArrivalTime=None,
                         actualDepartureTime="2026-10-11 00:30:00", unLoadBagCount=None,
                         fenceActualArrivalTimeStrFormat=gps and f"2026-10-11 {gps}:00 (Eastern Time)",
                         manualActualArrivalTimeStrFormat=manual and f"2026-10-11 {manual}:00 (Eastern Time)")
        seg.update(kw)
        return seg

    def ctx(self):
        return shift.context(self.store)

    def test_start_is_click_time_and_manual_arrival_is_reference(self):
        segs = [self.arrival_seg(1, manual="09:10"), self.arrival_seg(2, manual="08:35"),
                self.arrival_seg(3, manual="00:21")]  # 00:21 belongs to the previous reporting day
        self.shifts.update(et("2026-10-11 08:50:00"), segs)
        self.assertFalse(self.shifts.alerts_enabled())  # TMS records never start the shift
        view = self.shifts.view(et("2026-10-11 08:50:00"))
        self.assertEqual((view["state"], view["earliest_manual_ref"]["taskNo"], view["manual_arrival_without_shift"]),
                         ("CLOSED", "MT2", True))
        self.shifts.start(et("2026-10-11 08:52:30"), "Wei")
        view = self.shifts.view(et("2026-10-11 09:00:00"))
        self.assertEqual((view["state"], view["confirmed_start_et"], view["started_by"]),
                         ("ACTIVE", "2026-10-11 08:52", "Wei"))
        self.assertEqual(view["earliest_manual_ref"]["time_et"], "2026-10-11 08:35")  # kept separately
        self.assertEqual(self.store.open_session()["earliest_manual_ref"].count("MT2"), 1)
        self.assertTrue(self.shifts.alerts_enabled())

    def ctx_with(self, segs):
        return shift.context(self.store, segs)

    def test_overnight_example(self):
        # GPS 03:00 -> first-scan deadline 14:00 the same day; 60-min reminder at 04:00. No opening time involved.
        truck = self.arrival_seg(1, gps="03:00")
        f = stage(truck, et("2026-10-11 13:30:00"), "UNLOAD", ctx=self.ctx())
        self.assertEqual((f.stage, f.warn_level, f.deadline), ("WARN", 30, et("2026-10-11 14:00:00")))
        self.assertEqual(f.evidence["gpsArrival"], "2026-10-11 03:00:00 (Eastern Time)")  # original kept
        self.assertEqual(stage(truck, et("2026-10-11 04:00:00"), "UNLOAD_60", ctx=self.ctx()).stage, "WARN")
        self.assertEqual(stage(truck, et("2026-10-11 14:01:00"), "UNLOAD", ctx=self.ctx()).stage, "OVERDUE")

    def test_activation_time_never_moves_the_deadline(self):
        # Truck arrives 15:00, monitoring activated 17:28 -> first-scan deadline stays 16:00.
        truck = self.arrival_seg(1, gps="15:00")
        self.shifts.start(et("2026-10-11 17:28:00"), "wei")
        f = stage(truck, et("2026-10-11 17:30:00"), "UNLOAD", ctx=self.ctx())
        self.assertEqual((f.stage, f.deadline, f.evidence["countdownStart"]), ("OVERDUE", et("2026-10-11 16:00:00"),
                                                                              "2026-10-11 15:00"))

    def test_end_shift_mutes_but_keeps_records_and_countdowns(self):
        notifier = Notifier(self.store, log_dir=None, echo=False, enabled=self.shifts.alerts_enabled,
                            feishu_enabled=False)
        engine = Engine(self.store, notifier, shifts=self.shifts)
        self.shifts.start(et("2026-10-11 08:40:00"), "Wei")
        truck = self.arrival_seg(2, gps="17:30", manual="17:31")      # first-scan deadline 18:30
        engine.run(et("2026-10-11 17:35:00"), [truck])
        self.shifts.end(et("2026-10-11 17:50:00"), "Wei")
        engine.run(et("2026-10-11 18:05:00"), [truck])  # 30-min reminder while muted
        alert = self.store.latest_alert(2, "UNLOAD")
        self.assertEqual((alert["status"], alert["stage"], alert["deadline"]), ("open", "WARN", obs(x="18:30")["x"]))
        self.assertIsNone(alert["last_notified_at"])
        self.assertEqual([r["channel"] for r in self.store.db.execute(
            "SELECT channel FROM notifications WHERE alert_id=?", (alert["id"],))], ["muted"])
        s = self.store.sessions()[-1]
        self.assertEqual((s["ended_at"], s["ended_by"]), (obs(x="17:50")["x"], "Wei"))
        engine.run(et("2026-10-11 18:31:00"), [truck])  # still evaluated while closed: overdue recorded
        self.assertEqual(self.store.latest_alert(2, "UNLOAD")["stage"], "OVERDUE")

    def test_after_19_arrival_next_day_14(self):
        night = unload_seg(id=2, actualArrivalTime=None, actualDepartureTime="2026-10-11 15:00:00", unLoadBagCount=None,
                           fenceActualArrivalTimeStrFormat="2026-10-11 20:30:00 (Eastern Time)")
        f = stage(night, et("2026-10-12 09:00:00"), "UNLOAD", ctx=self.ctx())
        self.assertEqual((f.stage, f.deadline), ("WAITING", et("2026-10-12 14:00:00")))
        self.assertEqual(stage(night, et("2026-10-11 21:30:00"), "UNLOAD_60", ctx=self.ctx()).stage, "WARN")

    def test_boundary_does_not_close_and_warns_open_overnight(self):
        self.shifts.start(et("2026-10-11 08:40:00"), "Wei")
        self.shifts.update(et("2026-10-12 06:00:00"), [])
        self.assertTrue(self.shifts.alerts_enabled())  # 05:00 no longer closes the shift
        self.assertTrue(self.shifts.view(et("2026-10-12 06:00:00"))["open_overnight_warning"])
        self.assertEqual(self.store.sessions()[-1]["started_at"], obs(x="08:40")["x"])  # history unchanged
        self.shifts.confirm_open(et("2026-10-12 06:05:00"), "Wei")
        self.assertFalse(self.shifts.view(et("2026-10-12 06:10:00"))["open_overnight_warning"])
        self.assertTrue(self.shifts.view(et("2026-10-13 06:10:00"))["open_overnight_warning"])  # next day again

    def test_morning_reminder_once_while_closed(self):
        self.shifts.update(et("2026-10-11 08:00:00"), [])
        self.assertEqual(self.n.reminders, 0)
        for t in ("08:15", "08:25"):
            self.shifts.update(et(f"2026-10-11 {t}:00"), [])
        self.assertEqual(self.n.reminders, 1)
        self.shifts.update(et("2026-10-12 16:53:00"), [])
        self.assertEqual(self.n.reminders, 1)  # service started mid-afternoon: no "day approaching" reminder
        self.shifts.start(et("2026-10-13 08:00:00"), "Wei")
        self.shifts.update(et("2026-10-13 08:20:00"), [])
        self.assertEqual(self.n.reminders, 1)  # shift already open: no reminder

    def test_departure_ignores_shift(self):
        self.shifts.start(et("2026-10-11 08:40:00"), "Wei")
        f = stage(dep_seg(plannedDepartureTime="2026-10-11 02:00:00"), et("2026-10-11 03:00:00"), "BRANCH_DEPARTURE",
                  ctx=self.ctx())
        self.assertEqual((f.stage, f.deadline), ("OVERDUE", et("2026-10-11 02:00:00")))

    def test_restart_keeps_shift_and_does_not_resend(self):
        self.shifts.start(et("2026-10-11 08:40:00"), "Wei")
        segs = [self.arrival_seg(1, gps="09:00", manual="09:00", unLoadBagCount=None)]
        self.engine.run(et("2026-10-11 09:35:00"), segs)
        sent = len(self.n.sent)
        store2 = Store(self.path)
        restarted = Engine(store2, self.n, shifts=Shifts(store2, self.n))
        restarted.run(et("2026-10-11 09:36:00"), segs)
        self.assertEqual(len(self.n.sent), sent)
        self.assertTrue(Shifts(store2).alerts_enabled())


def op(seg, now, name, ctx=None):
    return next(f for f in compliance.evaluate(seg, now, ctx) if f.alert_type == f"OP:{name}")


class ComplianceRules(unittest.TestCase):
    now = et("2026-10-11 15:00:00")

    def out_seg(self, **kw):
        seg = dep_seg(originLoadingModeStr="Load Only", loadBagCount=None, loadedImg=None,
                      manualActualDepartureTimeStrFormat=None, fenceActualDepartureTimeStrFormat=None)
        seg.update(kw)
        return seg

    def test_loading(self):
        self.assertEqual(op(self.out_seg(), self.now, "LOADING").evidence["opStatus"], "PENDING")
        # 9. manual departure alone confirms loading (no count, no photo needed)
        dispatched = self.out_seg(manualActualDepartureTimeStrFormat="2026-10-11 12:05:00 (Eastern Time)")
        f = op(dispatched, self.now, "LOADING")
        self.assertEqual((f.stage, f.evidence["opStatus"]), ("OK", "VERIFIED"))
        # GPS departure without manual departure: loading not concluded either way
        gps_only = self.out_seg(fenceActualDepartureTimeStrFormat="2026-10-11 12:00:00 (Eastern Time)")
        self.assertEqual(op(gps_only, self.now, "LOADING").evidence["opStatus"], "UNCERTAIN")
        photo = self.out_seg(loadedImg="sys-mod-file/x.png")
        self.assertEqual(op(photo, self.now, "LOADING").stage, "OK")
        self.assertEqual(op(self.out_seg(originLoadingModeStr="Unload Only"), self.now, "LOADING").evidence["opStatus"],
                         "NOT_REQUIRED")
        self.assertEqual(op(self.out_seg(originLoadingModeStr=None), self.now, "LOADING").evidence["opStatus"], "UNCERTAIN")

    def test_departure_scenarios(self):
        planned_ok = dict(plannedDepartureTime="2026-10-11 10:30:00")
        # 4. GPS 10:00 + manual 10:05: operation complete, physical departure 10:00
        both = self.out_seg(fenceActualDepartureTimeStrFormat="2026-10-11 10:00:00 (Eastern Time)",
                            manualActualDepartureTimeStrFormat="2026-10-11 10:05:00 (Eastern Time)", **planned_ok)
        self.assertEqual(op(both, self.now, "DEPARTURE").stage, "OK")
        self.assertEqual(rules.departure_times(both)[:2], (et("2026-10-11 10:00:00"), "GPS"))
        self.assertEqual(stage(both, self.now, "BRANCH_DEPARTURE").stage, "OK")
        # 5. manual 10:05 only: operation complete, punctuality uses 10:05
        manual = self.out_seg(manualActualDepartureTimeStrFormat="2026-10-11 10:05:00 (Eastern Time)",
                              plannedDepartureTime="2026-10-11 10:02:00")
        self.assertEqual(op(manual, self.now, "DEPARTURE").stage, "OK")
        late = stage(manual, self.now, "BRANCH_DEPARTURE")
        self.assertEqual((late.stage, late.minutes, late.evidence["margin"]), ("LATE", 3, "late 0h 3m 0s"))
        # 6. GPS 10:00, no manual: physical departure yes, TMS operation missing after the grace
        gps = self.out_seg(fenceActualDepartureTimeStrFormat="2026-10-11 10:00:00 (Eastern Time)", **planned_ok)
        self.assertEqual(op(gps, et("2026-10-11 10:20:00"), "DEPARTURE").evidence["opStatus"], "PENDING")
        missing = op(gps, et("2026-10-11 10:31:00"), "DEPARTURE")
        self.assertEqual((missing.stage, missing.evidence["opStatus"]), ("MISSING", "MISSING"))
        self.assertIn("缺少人工发车记录", missing.evidence["statusZh"])
        self.assertEqual(stage(gps, self.now, "BRANCH_DEPARTURE").stage, "OVERDUE")  # GPS alone never completes
        # 7. neither: operation pending, punctuality countdown on the planned time
        neither = self.out_seg(**planned_ok)
        self.assertEqual(op(neither, self.now, "DEPARTURE").evidence["opStatus"], "PENDING")
        self.assertEqual(stage(neither, self.now, "BRANCH_DEPARTURE").stage, "OVERDUE")
        # 8. downstream arrival, no manual departure: missing BWI manual departure
        downstream = self.out_seg(actualArrivalTime="2026-10-11 14:00:00")
        self.assertEqual(op(downstream, self.now, "DEPARTURE").stage, "MISSING")

    def test_arrival(self):
        base = dict(actualArrivalTime=None, unLoadBagCount=None,
                    fenceActualArrivalTimeStrFormat="2026-10-11 14:00:00 (Eastern Time)")
        self.assertEqual(op(unload_seg(manualActualArrivalTimeStrFormat="2026-10-11 12:05:00", **{
            k: v for k, v in base.items() if k != "fenceActualArrivalTimeStrFormat"}), self.now, "ARRIVAL").stage, "OK")
        self.assertEqual(op(unload_seg(**base), self.now, "ARRIVAL").stage, "RISK")  # no shift tracking: 30 min grace
        self.assertEqual(op(unload_seg(**base), et("2026-10-11 14:20:00"), "ARRIVAL").evidence["opStatus"], "PENDING")
        # 2026-10-17: the 30-min check-in grace runs from the GPS arrival (opening time retired)
        self.assertEqual(op(unload_seg(**base), et("2026-10-11 14:29:00"), "ARRIVAL").stage, "WAITING")
        self.assertEqual(op(unload_seg(**base), et("2026-10-11 14:31:00"), "ARRIVAL").stage, "RISK")
        scanned = dict(base, unLoadBagCount=5)
        self.assertEqual(op(unload_seg(**scanned), self.now, "ARRIVAL").stage, "MISSING")

    def test_unloading(self):
        mode = dict(destinationLoadingModeStr="Unload Only")
        self.assertEqual(op(unload_seg(**mode), self.now, "UNLOADING").stage, "OK")
        none = dict(mode, unLoadBagCount=None)
        self.assertEqual(op(unload_seg(**none), self.now, "UNLOADING").evidence["opStatus"], "PENDING")
        marked = dict(none, unloadCompleteTimeStrFormat="2026-10-11 12:30:00 (Eastern Time)")
        self.assertEqual(op(unload_seg(**marked), self.now, "UNLOADING").stage, "MISSING")
        multi = dict(marked, handlingModeStr=MULTI)
        self.assertEqual(op(unload_seg(**multi), self.now, "UNLOADING").evidence["opStatus"], "UNCERTAIN")
        seg = unload_seg(**none)
        onward = dep_seg(id=9, taskNo=seg["taskNo"], sortNum=2, actualDepartureTime="2026-10-11 13:00:00")
        seg["sortNum"] = 1
        ctx = {"since": None, "shifts": None, "by_task": {seg["taskNo"]: [seg, onward]}}
        self.assertEqual(op(seg, self.now, "UNLOADING", ctx).stage, "MISSING")
        self.assertEqual(op(unload_seg(destinationLoadingModeStr=None), self.now, "UNLOADING").evidence["opStatus"],
                         "UNCERTAIN")

    def test_rechecks_tms_before_strong_reminder_and_compliance_warning(self):
        calls = []
        engine = Engine(Store(":memory:"), FakeNotifier(), confirm=lambda seg: calls.append(seg["id"]) or seg)
        seg = unload_seg(unLoadBagCount=None, destinationLoadingModeStr="Unload Only", _obs=obs(first_poll="11:50"),
                         manualActualArrivalTimeStrFormat="2026-10-11 12:00:00 (Eastern Time)")
        engine.run(et("2026-10-11 13:30:00"), [seg])  # first-scan deadline 14:00: 30-min reminder, no re-check
        self.assertEqual(calls, [])
        engine.run(et("2026-10-11 13:55:00"), [seg])  # 5 min left, high priority: re-check
        self.assertEqual(calls, [2])
        out = dep_seg(id=5, actualArrivalTime="2026-10-11 12:30:00", originLoadingModeStr="Unload Only")
        engine.run(et("2026-10-11 13:56:00"), [out])  # departure missing in TMS records: re-check
        self.assertEqual(calls, [2, 5])

    def test_time_and_compliance_are_independent(self):
        store, n = Store(":memory:"), FakeNotifier()
        engine = Engine(store, n)
        seg = unload_seg(unLoadBagCount=None, actualArrivalTime=None, destinationLoadingModeStr="Unload Only",
                         fenceActualArrivalTimeStrFormat="2026-10-11 12:00:00 (Eastern Time)",
                         _obs=obs(first_poll="11:50"))
        engine.run(et("2026-10-11 13:35:00"), [seg])  # first-scan deadline 14:00
        types = {(t, s) for _, t, s, _ in n.sent}
        self.assertIn(("UNLOAD", "WARN"), types)
        self.assertIn(("OP:ARRIVAL", "RISK"), types)
        checked_in = dict(seg, manualActualArrivalTimeStrFormat="2026-10-11 12:40:00 (Eastern Time)")
        engine.run(et("2026-10-11 13:41:00"), [checked_in])
        self.assertEqual(store.latest_alert(2, "OP:ARRIVAL")["outcome"], "resolved")
        unload_alert = store.latest_alert(2, "UNLOAD")
        self.assertEqual((unload_alert["status"], unload_alert["stage"]), ("open", "WARN"))  # time alert unaffected


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {"code": 0, "msg": "success"}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        r = self.responses.pop(0) if self.responses else FakeResp()
        if isinstance(r, Exception):
            raise r
        return r


HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token-123"


class FeishuDelivery(unittest.TestCase):
    def client(self, *responses, secret=None):
        self.session = FakeSession(*responses)
        return feishu.FeishuClient(HOOK, secret, session=self.session, retries=3, sleep=lambda s: None)

    def test_success_needs_http_200_and_code_0(self):
        self.assertEqual(self.client(FakeResp()).send("hi"), (True, None))
        ok, err = self.client(FakeResp(200, {"code": 19021, "msg": "sign match fail"})).send("hi")
        self.assertEqual((ok, len(self.session.calls)), (False, 1))  # permanent: not retried
        self.assertIn("19021", err)
        self.assertEqual(self.client(FakeResp(200, {"StatusCode": 0})).send("hi"), (True, None))  # older format

    def test_temporary_failures_retried_with_bound(self):
        ok, err = self.client(FakeResp(500), FakeResp(500), FakeResp(500), FakeResp()).send("hi")
        self.assertEqual((ok, len(self.session.calls)), (False, 3))
        ok, _ = self.client(FakeResp(429), FakeResp()).send("hi")
        self.assertTrue(ok)

    def test_errors_never_contain_webhook(self):
        boom = feishu.requests.ConnectionError(f"Max retries exceeded with url: {HOOK}")
        ok, err = self.client(boom, boom, boom).send("hi")
        self.assertFalse(ok)
        self.assertNotIn("secret-token-123", err)
        self.assertNotIn("open.feishu.cn/open-apis/bot/v2/hook/secret", err)

    def test_signature_added_when_secret_set(self):
        self.client(FakeResp(), secret="s3cr3t").send("hi")
        payload = self.session.calls[0][1]
        self.assertTrue(payload["timestamp"] and payload["sign"])

    def test_message_uses_real_fields_only(self):
        seg = unload_seg(id=7, taskNo="MT2026101400312", licensePlateNo=None)
        ev = {"routeType": "single-stop", "arrivalSource": "GPS", "gpsArrival": "2026-10-11 10:00:00 (Eastern Time)",
              "countdownStart": "2026-10-11 10:00", "unLoadBagCount": 10, "loadBagCount": 28}
        overdue = rules.Finding("UNLOAD", "OVERDUE", et("2026-10-11 11:00:00"), 1, "x", "", ev)
        self.assertIsNone(feishu.alert_message(seg, overdue, "ESCALATED", et("2026-10-11 11:01:00"), None))  # 60 min: no message
        f = rules.Finding("UNLOAD", "WARN", et("2026-10-11 11:00:00"), 5, "x", "", ev, warn_level=5, priority="high")
        severity, text = feishu.alert_message(seg, f, "ESCALATED", et("2026-10-11 10:55:00"), None)
        self.assertEqual(severity, "WARN5")
        self.assertTrue(text.startswith("🟠 BWI TMS — 卸车紧急提醒"))
        for line in ("任务编号：MT2026101400312", "到车时间：10-11 10:00（GPS）", "监控开始：10-11 10:00",
                     "卸车截止：10-11 11:00", "已卸 10 / 装车 28 袋"):
            self.assertIn(line, text)
        self.assertNotIn("车辆：", text)  # no plate in TMS -> no vehicle line
        self.assertNotIn("数据时间", text)
        self.assertIsNone(feishu.alert_message(seg, f, "RESOLVED", et("2026-10-11 11:01:00"), None))


class FeishuNotifier(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "n.db")
        self.store = Store(self.path)
        self.store.set_meta("feishu_shift_allowance", "1")  # authorization waiting (no shift open in these tests)
        self.session = FakeSession()
        self.on = True

    def notifier(self, test_mode=False, store=None):
        client = feishu.FeishuClient(HOOK, None, session=self.session, retries=1, sleep=lambda s: None)
        return Notifier(store or self.store, log_dir=None, echo=False, enabled=lambda: self.on, feishu_client=client,
                        feishu_enabled=True, test_mode=test_mode)

    def warn(self, sid, level=30, priority="normal"):
        return (unload_seg(id=sid, taskNo=f"MT{sid}"),
                rules.Finding("UNLOAD", "WARN", et("2026-10-11 13:00:00"), level, "r", "", {"routeType": "single-stop"},
                              warn_level=level, priority=priority))

    def test_dedupe_per_segment_type_severity_survives_restart(self):
        n = self.notifier()
        seg, f = self.warn(1)
        n.send(1, seg, f, "NEW", et("2026-10-11 12:30:00")); n.flush(et("2026-10-11 12:30:00"))
        n.send(1, seg, f, "NEW", et("2026-10-11 12:31:00")); n.flush(et("2026-10-11 12:31:00"))
        restarted = self.notifier(store=Store(self.path))
        restarted.send(1, seg, f, "NEW", et("2026-10-11 12:32:00")); restarted.flush(et("2026-10-11 12:32:00"))
        self.assertEqual(len(self.session.calls), 1)
        seg, strong = self.warn(1, 5, "high")
        n.send(1, seg, strong, "ESCALATED", et("2026-10-11 12:55:00")); n.flush(et("2026-10-11 12:55:00"))
        self.assertEqual(len(self.session.calls), 2)  # new severity
        self.assertIn("🟠", self.session.calls[-1][1]["content"]["text"])
        last = self.store.last_delivery("feishu")
        self.assertEqual((last["channel"], last["ok"]), ("feishu", 1))

    def test_test_mode_records_without_sending(self):
        n = self.notifier(test_mode=True)
        seg, f = self.warn(1)
        n.send(1, seg, f, "NEW", et("2026-10-11 12:30:00")); n.flush(et("2026-10-11 12:30:00"))
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.store.last_delivery("feishu")["channel"], "feishu-test-mode")

    def test_muted_not_sent_and_not_replayed_on_resume(self):
        n = self.notifier()
        self.on = False
        seg, f = self.warn(1)
        n.send(1, seg, f, "NEW", et("2026-10-11 12:30:00")); n.flush(et("2026-10-11 12:30:00"))
        self.on = True
        n.flush(et("2026-10-11 12:31:00"))
        self.assertEqual(self.session.calls, [])

    def test_burst_becomes_one_digest(self):
        n = self.notifier()
        for sid in range(1, 8):
            seg, f = self.warn(sid)
            n.send(sid, seg, f, "NEW", et("2026-10-11 12:30:00"))
        n.flush(et("2026-10-11 12:30:00"))
        self.assertEqual(len(self.session.calls), 1)
        self.assertIn("7 条新预警", self.session.calls[0][1]["content"]["text"])

    def test_morning_reminder_sent_while_closed_once_per_day(self):
        n = self.notifier()
        self.on = False
        shifts = Shifts(self.store, n, now=et("2026-10-11 00:00:00"))
        shifts.update(et("2026-10-11 08:15:00"), [])
        shifts.update(et("2026-10-11 08:30:00"), [])
        self.assertEqual(len(self.session.calls), 1)
        self.assertIn("🔔", self.session.calls[0][1]["content"]["text"])

    def test_disabled_sends_nothing(self):
        client = feishu.FeishuClient(HOOK, None, session=self.session)
        n = Notifier(self.store, log_dir=None, echo=False, feishu_client=client, feishu_enabled=False)
        seg, f = self.warn(1)
        n.send(1, seg, f, "NEW", et("2026-10-11 12:30:00")); n.flush(et("2026-10-11 12:30:00"))
        self.assertEqual(self.session.calls, [])


class NoticeRules(unittest.TestCase):
    """2026-10-15 notification set: departure reminders/alerts/completion, unloading 30/55, check-in issue."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "a.db")
        self.store = Store(self.path)
        self.session = FakeSession()
        self.store.set_meta("feishu_shift_allowance", "1")  # pilot: live Feishu authorized for one shift
        Shifts(self.store).start(et("2026-10-11 08:30:00"), "Wei")
        self.dep_on = mock.patch.object(config, "FEISHU_DEPARTURE_ENABLED", True)
        self.dep_on.start()
        self.addCleanup(self.dep_on.stop)

    def engine(self, store=None, confirm=None):
        store = store or self.store
        client = feishu.FeishuClient(HOOK, None, session=self.session, retries=1, sleep=lambda s: None)
        shifts = Shifts(store)
        n = Notifier(store, log_dir=None, echo=False, enabled=shifts.alerts_enabled, feishu_client=client,
                     feishu_enabled=True, test_mode=False)
        return Engine(store, n, shifts=shifts, confirm=confirm)

    def texts(self):
        return [c[1]["content"]["text"] for c in self.session.calls]

    def arrival(self, gps=None, manual=None, **kw):
        s = unload_seg(id=3, taskNo="MT2026101400312", licensePlateNo="1853019", actualArrivalTime=None,
                       unLoadBagCount=None, destinationLoadingModeStr="Unload Only",
                       fenceActualArrivalTimeStrFormat=gps and f"2026-10-11 {gps}:00 (Eastern Time)",
                       manualActualArrivalTimeStrFormat=manual and f"2026-10-11 {manual}:00 (Eastern Time)")
        s.update(kw)
        return s

    def branch(self, dest="ORF01", planned="2026-10-12 23:00:00", manual=None, **kw):  # TMS dates the 10-11 night run 10-12
        return dep_seg(id=20, taskNo="MT2026101100900", destination=dest, plannedDepartureTime=planned,
                       manualActualDepartureTimeStrFormat=manual and f"{manual} (Eastern Time)",
                       originLoadingModeStr="Load Only", **kw)

    # ---- route schedule (tests 1-4, 10) ----
    def test_route_deadlines(self):
        cases = [("ORF01", "2026-10-12 23:00:00", "2026-10-11 23:00:00", "2026-10-11", 1440),  # TMS D+1 23:00 -> night D
                 ("RIC01", "2026-10-12 00:00:00", "2026-10-12 00:00:00", "2026-10-11", 0),    # Day 1, crosses midnight
                 ("DCA01", "2026-10-12 03:00:00", "2026-10-12 02:00:00", "2026-10-11", 60),   # TMS 03:00, internal 02:00
                 ("IAD01", "2026-10-12 02:00:00", "2026-10-12 02:30:00", "2026-10-11", -30),
                 ("ORF01", "2026-10-12 02:00:00", "2026-10-11 23:00:00", "2026-10-11", 180),  # extra ORF run
                 ("RIC01", "2026-10-12 02:00:00", "2026-10-12 00:00:00", "2026-10-11", 120),  # extra RIC run
                 ("RIC01", "2026-10-12 08:54:00", "2026-10-12 00:00:00", "2026-10-11", 534)]  # daytime-dated run
        for dest, tms, deadline, op_date, diff in cases:
            m = departures.match(self.branch(dest, tms))
            self.assertEqual((m["deadline"], str(m["op_date"]), m["discrepancy_min"]), (et(deadline), op_date, diff),
                             (dest, tms))
            self.assertEqual(m["tms_planned"], et(tms))  # original TMS time kept separately
        self.assertIsNone(departures.match(self.branch("BWI01")))  # on-site operation
        pne = departures.match(self.branch("PNE01", "2026-10-12 04:00:00"))  # other routes: TMS planned exactly
        self.assertEqual((pne["route"], pne["deadline"], pne["kind"]), ("PNE", et("2026-10-12 04:00:00"), "tms"))
        extra = stage(self.branch("ORF01", "2026-10-12 02:00:00"), et("2026-10-11 23:05:00"), "BRANCH_DEPARTURE")
        self.assertEqual((extra.stage, extra.evidence["tmsPlannedDeparture"]), ("OVERDUE", "2026-10-12 02:00"))

    def test_regression_ORF_night_from_tms_next_day_date(self):
        """2026-10-17: ORF 23:00 runs are dated the next day in TMS (30/30 in 14 days). MT2026101700317: TMS planned
        2026-10-17 23:00, manual departure 2026-10-16 21:22:28 -> internal deadline 2026-10-16 23:00, 1 h 37 m 32 s early."""
        seg = dep_seg(id=185080, taskNo="MT2026101700317", destination="ORF01", plannedDepartureTime="2026-10-17 23:00:00",
                      manualActualDepartureTimeStrFormat="2026-10-16 21:22:28 (Eastern Time)", originLoadingModeStr="Load Only")
        m = departures.match(seg)
        self.assertEqual((m["deadline"], str(m["op_date"])), (et("2026-10-16 23:00:00"), "2026-10-16"))
        f = stage(seg, et("2026-10-17 09:00:00"), "BRANCH_DEPARTURE")
        self.assertEqual((f.stage, f.evidence["margin"], f.evidence["tmsPunctual"]), ("OK", "early 1h 37m 32s", True))
        _, text = feishu.departure_message(seg, stage(seg, et("2026-10-16 21:23:00"), "DEPARTURE_DONE"), "DEPARTURE_DONE")
        for line in ("内部截止时间：10-16 23:00", "实际人工发车：10-16 21:22:28", "发车状态：准时（提前 1小时37分32秒）",
                     "TMS计划发车：10-17 23:00（TMS准点，仅供参考）"):
            self.assertIn(line, text)
        # tonight's not-yet-departed ORF task (TMS planned 10-18 23:00) gets its reminder tonight, not tomorrow
        tonight = dep_seg(id=9, taskNo="MT9", destination="ORF01", plannedDepartureTime="2026-10-18 23:00:00",
                          originLoadingModeStr="Load Only")
        self.assertEqual(stage(tonight, et("2026-10-17 22:30:00"), "BRANCH_DEPARTURE").warn_level, 30)
        self.assertEqual(stage(tonight, et("2026-10-17 23:00:30"), "BRANCH_DEPARTURE").stage, "OVERDUE")

    def test_voided_task_excluded_and_alert_resolved(self):
        seg = self.branch("IAD01", "2026-10-12 02:30:00")
        self.engine().run(et("2026-10-12 02:35:00"), [seg])
        self.assertEqual(self.store.latest_alert(20, "BRANCH_DEPARTURE")["stage"], "OVERDUE")
        voided = dict(seg, _validity="VOIDED")
        self.assertEqual(rules.evaluate(voided, et("2026-10-12 03:00:00")), [])  # no deadline, warning or lateness
        sent = len(self.session.calls)
        self.engine().run(et("2026-10-12 03:00:00"), [voided])
        alert = self.store.latest_alert(20, "BRANCH_DEPARTURE")
        self.assertEqual((alert["status"], alert["outcome"]), ("closed", "resolved: task voided in TMS (已作废)"))
        self.assertTrue(self.store.alert_events([alert["id"]]))  # history kept
        self.assertEqual(len(self.session.calls), sent)  # no notification for the voided task

    def test_void_only_from_tms_status_missing_is_unverified(self):
        from bwi_monitor.tms import TmsClient
        store = Store(":memory:")
        a = self.branch("IAD01", "2026-10-12 02:30:00")                                   # cancelled in TMS
        b = dict(self.branch("RIC01", "2026-10-12 00:00:00"), id=21, taskNo="MT2")        # pagination miss
        c = dict(self.branch("DCA01", "2026-10-12 03:00:00"), id=22, taskNo="MT3")        # missing, status In Transit
        store.upsert_segments([a, b, c], et("2026-10-11 20:00:00"))
        client = TmsClient.__new__(TmsClient)
        client.fetch_task = lambda task_no: [b] if task_no == "MT2" else []
        client.task_status = lambda task_no: {"MT2026101100900": ("6", "Cancelled"), "MT3": ("4", "In Transit")}.get(task_no)
        voided, unverified, refreshed = client.reconcile_removed(store, et("2026-10-11 20:10:00"), et("2026-10-11 20:11:00"))
        self.assertEqual((voided, unverified, refreshed), ([20], [22], [21]))
        self.assertEqual(store.segment(20)["_validity"], "VOIDED")
        self.assertEqual(store.segment(22)["_validity"], "UNVERIFIED")  # disappearance alone never voids
        self.assertIn("In Transit", store.segment(22)["_validity_detail"])
        self.assertNotIn("_validity", store.segment(21))
        for sid in (20, 22):  # neither generates departure warnings
            self.assertEqual(rules.evaluate(store.segment(sid), et("2026-10-12 03:00:00")), [])
        store.upsert_segments([c], et("2026-10-11 20:20:00"))  # TMS returns it again: valid again
        self.assertNotIn("_validity", store.segment(22))

    def test_unverified_alert_closed_as_uncertain(self):
        seg = self.branch("DCA01", "2026-10-12 03:00:00")
        self.engine().run(et("2026-10-12 02:05:00"), [seg])
        self.engine().run(et("2026-10-12 02:10:00"), [dict(seg, _validity="UNVERIFIED")])
        alert = self.store.latest_alert(20, "BRANCH_DEPARTURE")
        self.assertEqual(alert["outcome"], "uncertain: task missing from TMS, validity unverified")

    def test_regression_MT2026101600301_early_not_late(self):
        """Exact TMS record (verified in the TMS interface 2026-10-16): manual departure 2026-10-15 23:43:47 ET,
        TMS planned 2026-10-16 03:00, internal DCA deadline 2026-10-16 02:00 -> on time, 2 h 16 min 13 s early."""
        raw = {"id": 184515, "taskNo": "MT2026101600301", "placeOfOrigin": "BWI.H", "destination": "DCA01",
               "taskName": "BWI.H-DCA01(03:00)", "originGroupTimeZone": "America/New_York",
               "destGroupTimeZone": "America/New_York", "actualDepartureTime": "2026-10-15 23:43:47",
               "actualDepartureTimeStrFormat": "2026-10-15 23:43:47 (Eastern Time)",
               "fenceActualDepartureTime": None, "fenceActualDepartureTimeStrFormat": None,
               "manualActualDepartureTime": 1792122227761,
               "manualActualDepartureTimeStrFormat": "2026-10-15 23:43:47 (Eastern Time)",
               "plannedDepartureTime": "2026-10-16 03:00:00", "actualArrivalTime": None}
        manual = rules.seg_time(raw, "manualActualDepartureTimeStrFormat")
        self.assertEqual(manual, datetime(2026, 10, 16, 3, 43, 47, tzinfo=timezone.utc))  # = 23:43:47 EDT
        m = departures.match(raw)
        self.assertEqual((m["deadline"], str(m["op_date"])), (et("2026-10-16 02:00:00"), "2026-10-15"))
        f = stage(raw, et("2026-10-16 09:00:00"), "BRANCH_DEPARTURE")
        self.assertEqual((f.stage, f.evidence["marginSeconds"], f.evidence["margin"]), ("OK", 8173, "early 2h 16m 13s"))
        done = stage(raw, et("2026-10-15 23:44:00"), "DEPARTURE_DONE")
        self.assertEqual(done.deadline, et("2026-10-16 02:00:00"))  # the record's deadline is the internal deadline
        _, text = feishu.departure_message(raw, done, "DEPARTURE_DONE")
        for line in ("内部截止时间：10-16 02:00", "实际人工发车：10-15 23:43:47", "发车状态：准时（提前 2小时16分13秒）"):
            self.assertIn(line, text)

    def test_timestamps_use_their_own_timezone(self):
        # CNO.H (Pacific) departure strings: label and epoch must win over the Eastern default.
        seg = {"originGroupTimeZone": "America/Los_Angeles", "destGroupTimeZone": "America/New_York",
               "manualActualDepartureTime": 1782147088901,
               "manualActualDepartureTimeStrFormat": "2026-06-22 09:51:28 (Pacific Time)",
               "plannedDepartureTime": "2026-06-22 10:00:00",
               "fenceActualDepartureTimeStrFormat": "2026-06-22 09:55:00 (Pacific Time)"}
        self.assertEqual(rules.seg_time(seg, "manualActualDepartureTimeStrFormat"),
                         datetime(2026, 6, 22, 16, 51, 28, tzinfo=timezone.utc))  # epoch
        self.assertEqual(rules.seg_time(seg, "fenceActualDepartureTimeStrFormat"),
                         datetime(2026, 6, 22, 16, 55, tzinfo=timezone.utc))      # "(Pacific Time)" label
        self.assertEqual(rules.seg_time(seg, "plannedDepartureTime"),
                         datetime(2026, 6, 22, 17, 0, tzinfo=timezone.utc))       # plain string: origin timezone
        self.assertEqual(rules.parse_et("2026-06-22 09:51:28 (Central Time)"),
                         datetime(2026, 6, 22, 14, 51, 28, tzinfo=timezone.utc))
        with self.assertRaises(rules.BadTime):
            rules.parse_et("2026-06-22 09:51:28 (Mars Time)")  # unknown label is not silently read as Eastern

    def test_regression_tasks_A_and_C_under_first_scan_rule(self):
        # Task C MT2026101600727: GPS 16:39:02, first scan observed 16:56, 27/28 bags -> first scan on time;
        # 60-min reminder because the truck is not fully unloaded.
        c = unload_seg(id=185002, taskNo="MT2026101600727", actualArrivalTime="2026-10-16 16:39:02",
                       fenceActualArrivalTimeStrFormat="2026-10-16 16:39:02 (Eastern Time)", loadBagCount=28,
                       unLoadBagCount=27, _obs={"first_poll": "2026-10-16T13:21:35+00:00",
                                                "start_seen": "2026-10-16T20:56:11+00:00"})
        now = et("2026-10-17 12:00:00")
        f = stage(c, now, "UNLOAD")
        self.assertEqual((f.stage, f.deadline), ("OK", datetime(2026, 10, 16, 21, 39, 2, tzinfo=timezone.utc)))
        g = stage(c, now, "UNLOAD_60")
        self.assertEqual((g.stage, g.evidence["progress60"]), ("WARN", "27/28 bags"))
        # Task A MT2026101700317 (ORF01 -> BWI, multi-stop): manual check-in 10-17 08:51:56, no scan
        # -> first-scan deadline 14:00 (not 09:51); 60-min reminder at 09:51.
        a = unload_seg(id=185081, taskNo="MT2026101700317", placeOfOrigin="ORF01", actualArrivalTime="2026-10-17 08:51:56",
                       manualActualArrivalTimeStrFormat="2026-10-17 08:51:56 (Eastern Time)", handlingModeStr=MULTI,
                       loadBagCount=None, unLoadBagCount=None, _obs={"first_poll": "2026-10-16T17:24:52+00:00"})
        self.assertEqual(stage(a, et("2026-10-17 10:00:00"), "UNLOAD").deadline, et("2026-10-17 14:00:00"))
        self.assertEqual(stage(a, et("2026-10-17 09:52:00"), "UNLOAD_60").stage, "WARN")
        _, text = feishu.alert_message(a, stage(a, et("2026-10-17 09:52:00"), "UNLOAD_60"), "NEW", et("2026-10-17 09:52:00"), None)
        for line in ("🟡 BWI 卸车提醒（到车已60分钟）", "任务编号：MT2026101700317", "卸车进度：尚无卸车扫描", "首枪截止：10-17 14:00"):
            self.assertIn(line, text)

    def test_no_gps_needed(self):  # 10
        seg = self.branch(manual="2026-10-11 22:45:00")
        self.assertIsNone(seg["fenceActualDepartureTimeStrFormat"])
        self.assertEqual(stage(seg, et("2026-10-11 23:30:00"), "BRANCH_DEPARTURE").stage, "OK")

    # ---- manual departure timing (tests 5-8) ----
    def test_manual_departure_vs_deadline(self):
        on_time = stage(self.branch(manual="2026-10-11 22:45:00"), et("2026-10-11 23:30:00"), "BRANCH_DEPARTURE")
        exact = stage(self.branch(manual="2026-10-11 23:00:00"), et("2026-10-11 23:30:00"), "BRANCH_DEPARTURE")
        late = stage(self.branch(manual="2026-10-11 23:12:00"), et("2026-10-11 23:30:00"), "BRANCH_DEPARTURE")
        self.assertEqual((on_time.stage, exact.stage, late.stage, late.minutes), ("OK", "OK", "LATE", 12))
        early = stage(self.branch(manual="2026-10-11 14:00:00"), et("2026-10-11 23:30:00"), "BRANCH_DEPARTURE")
        self.assertEqual(early.stage, "OK")  # early is always fine
        none = self.branch()
        self.assertEqual(stage(none, et("2026-10-11 22:29:00"), "BRANCH_DEPARTURE").stage, "WAITING")
        self.assertEqual(stage(none, et("2026-10-11 22:30:00"), "BRANCH_DEPARTURE").warn_level, 30)
        self.assertEqual(stage(none, et("2026-10-11 23:00:30"), "BRANCH_DEPARTURE").stage, "OVERDUE")  # 8

    def test_departure_reminder_alert_and_completion_messages(self):  # 9
        calls = []
        engine = self.engine(confirm=lambda seg: calls.append(seg["id"]) or seg)
        engine.run(et("2026-10-11 22:31:00"), [self.branch()])
        engine.run(et("2026-10-11 23:01:00"), [self.branch()])
        engine.run(et("2026-10-11 23:13:00"), [self.branch(manual="2026-10-11 23:12:00")])
        texts = self.texts()
        self.assertTrue(texts[0].startswith("🟡 BWI 发车提醒"))
        for line in ("线路：ORF", "计划发车截止：10-11 23:00", "当前状态：尚未人工发车", "剩余时间：30分钟"):
            self.assertIn(line, texts[0])
        self.assertTrue(texts[1].startswith("🔴 BWI 发车截止告警"))
        self.assertTrue(texts[2].startswith("✅ BWI 发车完成"))
        for line in ("任务编号：MT2026101100900", "实际人工发车：10-11 23:12:00", "发车状态：晚点（晚点 0小时12分0秒）"):
            self.assertIn(line, texts[2])
        self.assertEqual(len(texts), 3)
        self.assertEqual(calls, [20, 20])  # TMS re-checked before the reminder and before the deadline alert

    def test_completion_once_and_not_replayed_after_restart(self):  # 16
        seg = self.branch(manual="2026-10-11 22:45:00")
        self.engine().run(et("2026-10-11 22:46:00"), [seg])
        self.engine(Store(self.path)).run(et("2026-10-11 22:56:00"), [seg])
        self.assertEqual(len([t for t in self.texts() if t.startswith("✅")]), 1)
        old = dep_seg(id=21, taskNo="MT2026101000800", destination="ORF01", plannedDepartureTime="2026-10-10 23:00:00",
                      manualActualDepartureTimeStrFormat="2026-10-10 22:40:00 (Eastern Time)")
        self.engine().run(et("2026-10-11 09:00:00"), [old])  # old completion seen for the first time: not replayed
        self.assertEqual(len([t for t in self.texts() if t.startswith("✅")]), 1)
        self.assertIn("backfill", self.store.latest_alert(21, "DEPARTURE_DONE")["outcome"])

    def test_departure_messages_off_until_verified(self):
        self.dep_on.stop()
        self.engine().run(et("2026-10-11 22:31:00"), [self.branch()])
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.store.latest_alert(20, "BRANCH_DEPARTURE")["stage"], "WARN")  # still recorded
        self.dep_on.start()

    # ---- unloading (tests 11-14) ----
    def test_unloading_messages(self):
        # arrival 13:30 -> 60-min reminder 14:30 and first-scan deadline 14:30 (MAX(14:30, 14:00))
        truck = self.arrival(gps="13:30", manual="13:31", _obs=obs(first_poll="13:20"))
        for t in ("14:00", "14:25", "14:30", "14:31"):
            self.engine().run(et(f"2026-10-11 {t}:00"), [truck])
        unload = [t for t in self.texts() if "卸车" in t.split("\n")[0]]
        self.assertEqual([u.split("\n")[0] for u in unload],
                         ["🟡 BWI TMS — 卸车提醒", "🟠 BWI TMS — 卸车紧急提醒", "🟡 BWI 卸车提醒（到车已60分钟）"])
        self.assertIn("仅剩 5 分钟", unload[1])
        self.assertEqual(self.store.latest_alert(3, "UNLOAD")["stage"], "OVERDUE")  # recorded, no Feishu at deadline

    def test_no_immediate_arrival_notification(self):  # 14
        self.engine().run(et("2026-10-11 10:36:00"), [self.arrival(gps="10:35")])
        self.assertEqual([t for t in self.texts() if t.startswith("🚚")], [])
        self.assertEqual(self.store.latest_alert(3, "ARRIVAL")["outcome"], "arrived")  # arrival still recorded

    def test_checkin_issue_is_sent_other_compliance_stays_on_dashboard(self):
        opener = self.arrival(manual="08:00")
        opener["id"] = 9
        self.engine().run(et("2026-10-11 11:06:00"), [opener, self.arrival(gps="10:35")])
        self.assertTrue(any("TMS人工签到异常" in t for t in self.texts()))
        out = dep_seg(id=8, taskNo="MT77", plannedDepartureTime="2026-10-11 10:30:00",
                      fenceActualDepartureTimeStrFormat="2026-10-11 10:00:00 (Eastern Time)")
        self.engine().run(et("2026-10-11 10:31:00"), [out])
        self.assertFalse(any("TMS人工发车异常" in t for t in self.texts()))
        self.assertEqual(self.store.latest_alert(8, "OP:DEPARTURE")["stage"], "MISSING")  # on the dashboard

    # ---- shift (tests 15-16) ----
    def test_end_and_start_shift(self):
        Shifts(self.store).end(et("2026-10-11 22:00:00"), "Wei")
        self.engine().run(et("2026-10-11 22:31:00"), [self.branch()])
        self.assertEqual(self.texts(), [])  # muted after End Shift
        self.assertEqual(self.store.latest_alert(20, "BRANCH_DEPARTURE")["stage"], "WARN")  # but recorded
        Shifts(self.store).start(et("2026-10-11 22:40:00"), "Wei")
        self.store.set_meta("feishu_live_session", str(self.store.open_session()["id"]))  # re-authorized shift
        self.engine().run(et("2026-10-11 22:41:00"), [self.branch()])
        self.assertEqual(self.texts(), [])  # the muted reminder is not replayed
        self.engine().run(et("2026-10-11 23:01:00"), [self.branch()])
        self.assertEqual(len(self.texts()), 1)  # the next real transition is delivered
        self.assertTrue(self.texts()[0].startswith("🔴"))

    def test_live_feishu_only_for_the_authorized_shift(self):
        with mock.patch.object(config, "FEISHU_SHIFT_AUTHORIZATION", True):  # pilot rule, opt-in since 2026-10-18
            Shifts(self.store).end(et("2026-10-11 22:00:00"), "Wei")
            Shifts(self.store).start(et("2026-10-11 22:10:00"), "Wei")  # next shift, no new authorization
            self.engine().run(et("2026-10-11 22:31:00"), [self.branch()])
            self.assertEqual(self.texts(), [])
            self.assertEqual(self.store.last_delivery("feishu")["channel"], "feishu-test-mode")  # recorded, not sent

    def test_every_shift_is_live_without_a_cli_authorization(self):
        """2026-10-18: Start Shift alone turns live Feishu on; no --authorize-feishu-shift per shift."""
        Shifts(self.store).end(et("2026-10-11 22:00:00"), "Wei")
        self.engine().run(et("2026-10-11 22:31:00"), [self.branch()])
        self.assertEqual(self.texts(), [])                                   # no shift open: muted
        Shifts(self.store).start(et("2026-10-11 22:40:00"), "Wei")
        self.engine().run(et("2026-10-11 23:01:00"), [self.branch()])       # deadline passed: 🔴 goes out
        self.assertEqual(len(self.texts()), 1)

    def test_dashboard_feishu_switch_overrides_the_service_setting(self):
        self.store.set_meta("feishu_mode", "test")
        self.engine().run(et("2026-10-11 22:31:00"), [self.branch()])
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.store.last_delivery("feishu")["channel"], "feishu-test-mode")
        self.store.set_meta("feishu_mode", "live")
        self.engine().run(et("2026-10-11 23:01:00"), [self.branch()])       # deadline passed: 🔴 goes out
        self.assertEqual(len(self.texts()), 1)

    def test_feed_outage_and_recovery_reach_the_shift_group(self):
        e = self.engine()
        e.feed_status(et("2026-10-11 20:00:00"), "TMS login rejected (401 Unauthorized: token expired). Update GOFO_AUTH.")
        e.feed_status(et("2026-10-11 20:01:00"), "TMS login rejected (401 again). Update GOFO_AUTH.")  # no repeat
        e.feed_status(et("2026-10-11 20:10:00"), None)
        self.assertEqual([t.split("\n")[0] for t in self.texts()], ["⛔ BWI 监控数据中断", "✅ BWI 监控数据已恢复，提醒继续。"])
        self.assertIn("TMS 登录已失效", self.texts()[0])
        Shifts(self.store).end(et("2026-10-11 21:00:00"), "Wei")         # no shift: outage stays on the dashboard only
        e.feed_status(et("2026-10-11 22:00:00"), "No TMS data since 21:30 ET (no successful poll)")
        e.feed_status(et("2026-10-11 22:30:00"), None)
        self.assertEqual(len(self.texts()), 2)

    def test_hidden_task_leaves_the_board_and_stops_alerts(self):
        now = et("2026-10-11 22:31:00")
        self.store.upsert_segments([self.branch()], now)
        self.engine().run(now)
        self.assertEqual(len(self.texts()), 1)                              # 30-min reminder sent
        self.store.hide_segment(20, now, "Wei", "not a BWI task")
        seg = self.store.segments()[0]
        self.assertEqual(seg["_hidden"]["user"], "Wei")
        self.assertEqual(rules.evaluate(seg, now), [])
        later = et("2026-10-11 23:05:00")
        self.engine().run(later)                                             # past the deadline: still quiet
        self.assertEqual(len(self.texts()), 1)
        self.assertEqual(self.store.open_alerts(), [])
        self.assertTrue(self.store.latest_alert(20, "BRANCH_DEPARTURE")["outcome"].startswith("closed: task hidden"))
        self.assertEqual(web.departure_board(self.store, self.store.segments(), later), [])
        self.assertEqual([r["taskNo"] for r in web.hidden_rows(self.store, {20: seg})], ["MT2026101100900"])
        self.store.unhide_segment(20, later, "Wei")
        self.assertEqual(len(web.departure_board(self.store, self.store.segments(), later)), 1)

    def test_regression_MT2026101400730_no_departure_check_before_bwi_arrival(self):
        """CNO.H -> BWI.H -> JFK.H: the CNO leg has no manual check-in and no GPS arrival at BWI, so the BWI -> JFK
        departure (TMS planned 2026-10-17 06:06) is not judged yet; once the truck is checked in it is."""
        inbound = unload_seg(id=183697, taskNo="MT2026101400730", placeOfOrigin="CNO.H", sortNum=1, actualArrivalTime=None,
                             unLoadBagCount=None, vehicleStatusStr="In Transit")
        outbound = dep_seg(id=183698, taskNo="MT2026101400730", destination="JFK.H", sortNum=2,
                           plannedDepartureTime="2026-10-17 06:06:00", originLoadingModeStr="Load and Unload")
        ctx = {"by_task": {"MT2026101400730": [inbound, outbound]}}
        now = et("2026-10-17 14:00:00")
        f = stage(outbound, now, "BRANCH_DEPARTURE", ctx=ctx)
        self.assertEqual((f.stage, f.evidence["truckAtBwi"]), ("PENDING", False))
        self.assertEqual(op(outbound, now, "DEPARTURE", ctx).evidence["opStatus"], "PENDING")
        arrived = dict(inbound, manualActualArrivalTimeStrFormat="2026-10-17 13:00:00 (Eastern Time)")
        ctx2 = {"by_task": {"MT2026101400730": [arrived, outbound]}}
        self.assertEqual(stage(outbound, now, "BRANCH_DEPARTURE", ctx=ctx2).stage, "OVERDUE")  # judged once at BWI
        origin_task = dep_seg(id=7, taskNo="MT7", destination="JFK.H", plannedDepartureTime="2026-10-17 06:06:00")
        self.assertEqual(stage(origin_task, now, "BRANCH_DEPARTURE", ctx={"by_task": {"MT7": [origin_task]}}).stage,
                         "OVERDUE")  # a task starting at BWI has no leg to wait for

    def test_manual_close_sticks_until_worse_and_keep_open_policy(self):
        store, n = Store(":memory:"), FakeNotifier()
        engine = Engine(store, n)
        seg = unload_seg(actualArrivalTime="2026-10-11 13:30:00", unLoadBagCount=None, _obs=obs(first_poll="13:20"))
        engine.run(et("2026-10-11 14:00:00"), [seg])                      # 30-min reminder (deadline 14:30)
        alert = store.latest_alert(2, "UNLOAD")
        self.assertTrue(config.closable("UNLOAD", "WARN"))
        store.manual_close(alert["id"], et("2026-10-11 14:01:00"), "Wei", "司机已在卸车")
        engine.run(et("2026-10-11 14:02:00"), [seg])                      # same stage: stays closed, not re-sent
        self.assertEqual(store.latest_alert(2, "UNLOAD")["id"], alert["id"])
        self.assertTrue(store.latest_alert(2, "UNLOAD")["outcome"].startswith("manually closed by Wei: 司机已在卸车"))
        engine.run(et("2026-10-11 14:26:00"), [seg])                      # worse (5 min left): a new alert
        self.assertEqual(store.latest_alert(2, "UNLOAD")["warn_level"], 5)
        self.assertFalse(config.closable("BRANCH_DEPARTURE", "OVERDUE"))  # kept: clears only via TMS
        self.assertFalse(config.closable("UNLOAD", "OVERDUE"))
        self.assertTrue(config.closable("UNLOAD_60", "WARN"))

    def test_late_arrival_departure_alert_says_when_truck_reached_bwi(self):
        inbound = unload_seg(id=1, taskNo="MT8", placeOfOrigin="CNO.H", sortNum=1,
                             manualActualArrivalTimeStrFormat="2026-10-17 19:49:00 (Eastern Time)")
        out = dep_seg(id=2, taskNo="MT8", destination="JFK.H", sortNum=2, plannedDepartureTime="2026-10-17 06:06:00")
        f = stage(out, et("2026-10-17 19:55:00"), "BRANCH_DEPARTURE", ctx={"by_task": {"MT8": [inbound, out]}})
        self.assertEqual((f.stage, f.evidence["arrivedAfterDeadline"]), ("OVERDUE", True))
        _, text = feishu.departure_message(out, f, "NEW")
        self.assertTrue(text.startswith("🔴 BWI 发车截止告警"))
        self.assertIn("车辆到达BWI：10-17 19:49（晚于发车截止，车辆已晚点）", text)

    def test_receiving_window_and_board(self):
        from bwi_monitor import web
        day, start, end = web.receiving_window(et("2026-10-17 14:00:00"))
        self.assertEqual((str(day), start, end), ("2026-10-17", et("2026-10-16 19:00:00"), et("2026-10-17 19:00:00")))
        day, start, _ = web.receiving_window(et("2026-10-17 19:30:00"))   # after 19:00 the window moves on
        self.assertEqual((str(day), start), ("2026-10-18", et("2026-10-17 19:00:00")))
        now = et("2026-10-17 14:00:00")
        arrived_last_night = unload_seg(id=1, taskNo="A", fenceActualArrivalTimeStrFormat="2026-10-16 20:33:00 (Eastern Time)")
        arrived_before = unload_seg(id=2, taskNo="B", fenceActualArrivalTimeStrFormat="2026-10-16 18:50:00 (Eastern Time)")
        en_route_tomorrow = unload_seg(id=3, taskNo="C", placeOfOrigin="CNO.H", actualArrivalTime=None,
                                       manualActualDepartureTimeStrFormat="2026-10-16 01:07:00 (Eastern Time)",
                                       estimationArrivalTime="2026-10-17 19:49:00")
        stale = unload_seg(id=4, taskNo="D", actualArrivalTime=None,
                           manualActualDepartureTimeStrFormat="2026-06-13 01:00:00 (Eastern Time)")
        rows = {r["taskNo"]: r for r in web.receiving_board([arrived_last_night, arrived_before, en_route_tomorrow, stale],
                                                            {}, [], now)}
        self.assertEqual(sorted(rows), ["A", "C"])     # 18:50 belongs to yesterday; a June departure is stale
        self.assertEqual((rows["A"]["arrived"], rows["A"]["bucket"]), (True, "today"))
        self.assertEqual((rows["C"]["arrived"], rows["C"]["departed"], rows["C"]["bucket"]), (False, True, "next"))

    def test_sixty_minute_quantity_mismatch_message(self):
        seg = unload_seg(id=5, taskNo="MT2026101700743", actualArrivalTime="2026-10-17 14:53:59", unLoadBagCount=27,
                         loadBagCount=28, licensePlateNo="329")
        f = stage(seg, et("2026-10-17 15:54:00"), "UNLOAD_60")
        self.assertEqual(f.stage, "WARN")
        _, text = feishu.alert_message(seg, f, "NEW", et("2026-10-17 15:54:00"), None)
        self.assertIn("卸车进度：数量未对上：已卸 27 / 装车 28 袋", text)
        self.assertIn("手动确认", text)
        self.assertTrue(config.closable("UNLOAD_60", "WARN"))   # the team can confirm it by hand

    def test_arrival_uses_the_earlier_of_manual_and_gps(self):
        both_gps_first = unload_seg(fenceActualArrivalTimeStrFormat="2026-10-11 12:00:00 (Eastern Time)",
                                    manualActualArrivalTimeStrFormat="2026-10-11 12:25:00 (Eastern Time)")
        self.assertEqual(rules.arrival(both_gps_first)[:2], (et("2026-10-11 12:00:00"), "GPS"))
        both_manual_first = unload_seg(fenceActualArrivalTimeStrFormat="2026-10-11 12:20:00 (Eastern Time)",
                                       manualActualArrivalTimeStrFormat="2026-10-11 12:05:00 (Eastern Time)")
        self.assertEqual(rules.arrival(both_manual_first)[:2], (et("2026-10-11 12:05:00"), "manual"))
        only_gps = unload_seg(fenceActualArrivalTimeStrFormat="2026-10-11 12:20:00 (Eastern Time)")
        self.assertEqual(rules.arrival(only_gps)[:2], (et("2026-10-11 12:20:00"), "GPS"))

if __name__ == "__main__":
    unittest.main()
