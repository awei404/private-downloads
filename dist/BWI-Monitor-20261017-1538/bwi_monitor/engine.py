"""Alert lifecycle: turns rule findings into persisted alerts and notifications.

One alert object per (segment, alert type). Lifecycle (SRS §5.2):
    WAITING -> WARN(30) -> WARN(15) -> OVERDUE -> LATE / UNCERTAIN (closed, kept)
                                    \\-> OK (closed, resolved)
An OVERDUE/CHECK alert whose segment ages out of the lookback window is closed as
"unresolved", never as cleared.
Notifications fire only on transitions (new stage, escalation, deadline change,
resolution), so repeated polls never re-send the same stage; state is in SQLite,
so restarts do not re-send either.
"""
from datetime import datetime, timedelta

from . import config, rules, shift
from .rules import CHECK, LATE, OK, OVERDUE, PENDING, SKIP, UNCERTAIN, WAITING, WARN
from .store import from_iso

SYSTEM_SEGMENT = 0  # alerts about the monitor itself (feed down, token expired)


rank = rules.stage_rank


class Engine:
    def __init__(self, store, notifier, confirm=None, shifts=None):
        """confirm(seg) -> fresh seg dict or None: re-fetch one segment before a strong reminder, an overdue
        notice or a compliance warning. shifts: shift.Shifts; when given, unload countdowns follow confirmed
        shift starts."""
        self.store = store
        self.notifier = notifier
        self.confirm = confirm
        self.shifts = shifts
        self.ctx = None
        self._fresh = {}  # segment id -> re-fetched segment, reset every cycle

    def run(self, now, segments=None, notify=True):
        segments = self.store.segments() if segments is None else segments
        if self.shifts is not None:
            self.shifts.update(now, segments)
            self.ctx = shift.context(self.store, segments)
        self._fresh = {}
        by_task = {}
        for seg in segments:
            by_task.setdefault(seg.get("taskNo"), []).append(seg)
        self.ctx = dict(self.ctx or {}, by_task=by_task)
        produced, aged_out, removed = set(), set(), {}
        for seg in segments:
            if seg.get("_validity"):
                removed[seg["id"]] = seg["_validity"]
            if not rules.in_window(seg, now):
                aged_out.add(seg["id"])
            for f in rules.evaluate(seg, now, self.ctx):
                produced.add((seg["id"], f.alert_type))
                self.apply(seg, f, now, notify)
        for a in self.store.open_alerts():
            if a["segment_id"] != SYSTEM_SEGMENT and (a["segment_id"], a["alert_type"]) not in produced:
                aged = a["segment_id"] in aged_out and a["stage"] in (OVERDUE, CHECK)
                validity = removed.get(a["segment_id"])
                outcome = ("resolved: task voided in TMS (已作废)" if validity == "VOIDED"
                           else "uncertain: task missing from TMS, validity unverified" if validity
                           else "unresolved: aged out of monitoring window" if aged else "cleared")
                self.store.close_alert(a, now, outcome)
        if notify and hasattr(self.notifier, "flush"):
            self.notifier.flush(now)

    def apply(self, seg, f, now, notify=True, confirmed=False):
        latest = self.store.latest_alert(seg["id"], f.alert_type)
        open_ = latest if latest is not None and latest["status"] == "open" else None
        send = (lambda aid, kind: self.notifier.send(aid, seg, f, kind, now)) if notify else (lambda *a: None)

        # A by-hand close stands until the situation gets worse (higher stage) or TMS shows a final result.
        if (open_ is None and latest is not None and (latest["outcome"] or "").startswith("manually closed")
                and f.stage not in (OK, LATE, UNCERTAIN, rules.EVENT, SKIP, PENDING, WAITING)
                and rank(f.stage, f.warn_level) <= rank(latest["stage"], latest["warn_level"])):
            return

        if f.stage == rules.EVENT:  # vehicle arrival / departure completed: recorded once per segment, ever
            if latest is None:
                # f.deadline holds the event time. Events older than the cutoff when first seen (first run,
                # restart after a gap, data backfill) are recorded for review but not announced late.
                at = (f.evidence or {}).get("eventTime")
                event_time = datetime.fromisoformat(at) if at else f.deadline
                fresh = event_time is not None and now - event_time <= timedelta(minutes=config.ARRIVAL_NOTIFY_MAX_AGE_MINUTES)
                outcome = (f.evidence or {}).get("eventOutcome") or "arrived"
                aid = self.store.create_alert(seg["id"], f, now, status="closed",
                                              outcome=outcome if fresh else f"{outcome} (backfill, not notified)")
                if fresh:
                    send(aid, f.alert_type)
            return

        if f.stage in (SKIP, PENDING):  # not judged / countdown not started: no alert
            if open_:
                self.store.close_alert(open_, now, "not judged: " + f.reason, stage=SKIP)
            return

        if f.stage == WAITING:
            if open_:  # deadline moved later
                self.store.close_alert(open_, now, "cleared: deadline moved", stage=WAITING)
                send(open_["id"], "UPDATED")
            return

        if f.stage == OK:
            if open_:
                self.store.update_alert(open_, f, now, "resolved")
                self.store.close_alert(open_, now, "resolved", stage=OK)
                send(open_["id"], "RESOLVED")
            return

        if f.stage in (LATE, UNCERTAIN):
            outcome = "late" if f.stage == LATE else "uncertain"
            if open_:
                self.store.update_alert(open_, f, now, "completed " + outcome)
                self.store.close_alert(open_, now, outcome, stage=f.stage)
                send(open_["id"], f.stage)
            elif not (latest is not None and latest["stage"] == f.stage
                      and from_iso(latest["deadline"]) == f.deadline):
                aid = self.store.create_alert(seg["id"], f, now, status="closed", outcome=outcome)
                send(aid, f.stage)
            return

        # WARN / OVERDUE / CHECK: an open alert should exist.
        # Re-check the latest TMS data before an overdue notice or a strong (high-priority) reminder.
        needs_check = (f.stage in (OVERDUE, rules.MISSING, rules.RISK) or f.priority == "high"
                       or (f.alert_type == "BRANCH_DEPARTURE" and f.stage == WARN))
        if (needs_check and not confirmed and self.confirm
                and (open_ is None or rank(open_["stage"], open_["warn_level"]) < rank(f.stage, f.warn_level))):
            if seg["id"] not in self._fresh:
                self._fresh[seg["id"]] = self.confirm(seg)
            fresh = self._fresh[seg["id"]]
            if fresh is not None:
                for g in rules.evaluate(fresh, now, self.ctx):
                    if g.alert_type == f.alert_type:
                        return self.apply(fresh, g, now, notify, confirmed=True)
                return

        if open_ is None:
            aid = self.store.create_alert(seg["id"], f, now)
            send(aid, "NEW")
            return

        old_rank, new_rank = rank(open_["stage"], open_["warn_level"]), rank(f.stage, f.warn_level)
        old_deadline = from_iso(open_["deadline"])
        moved = (old_deadline is not None and f.deadline is not None
                 and abs(old_deadline - f.deadline) >= timedelta(minutes=1))
        if new_rank > old_rank:
            self.store.update_alert(open_, f, now, "escalated")
            send(open_["id"], "ESCALATED")
        elif moved:
            self.store.update_alert(open_, f, now, "deadline changed")
            send(open_["id"], "UPDATED")
        else:
            self.store.update_alert(open_, f, now, "refresh")
            last = from_iso(open_["last_notified_at"])
            if (f.stage == OVERDUE and config.REPEAT_OVERDUE_MINUTES > 0 and last is not None
                    and now - last >= timedelta(minutes=config.REPEAT_OVERDUE_MINUTES)):
                send(open_["id"], "REMINDER")

    # ---- monitor health ------------------------------------------------------
    def feed_status(self, now, problem, notify=True):
        """Open/close the SYSTEM:FEED alert. problem=None means the feed is healthy."""
        seg = {"id": SYSTEM_SEGMENT, "taskNo": "-", "placeOfOrigin": "monitor", "destination": "TMS"}
        latest = self.store.latest_alert(SYSTEM_SEGMENT, "SYSTEM:FEED")
        open_ = latest if latest is not None and latest["status"] == "open" else None
        if problem is None:
            if open_:
                self.store.close_alert(open_, now, "recovered", stage=OK)
                if notify:
                    self.notifier.send(open_["id"], seg, rules.Finding("SYSTEM:FEED", OK, reason="TMS feed recovered"),
                                       now=now, kind="RESOLVED")
            return
        f = rules.Finding("SYSTEM:FEED", CHECK, reason=problem,
                          action="Monitoring data may be stale. Refresh GOFO_AUTH / check the service.")
        if open_ is None:
            aid = self.store.create_alert(SYSTEM_SEGMENT, f, now)
            if notify:
                self.notifier.send(aid, seg, f, "NEW", now)
        elif open_["reason"] != problem:
            self.store.update_alert(open_, f, now, "problem changed")
