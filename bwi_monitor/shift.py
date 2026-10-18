"""Shifts and warehouse opening (corrected 2026-10-14).

Three separate times:
    actual arrival          GPS if valid, else manual arrival (rules.arrival)
    warehouse opening       per day: confirmed on the dashboard, else provisional = earliest valid
                            morning manual check-in (after DAY_BOUNDARY), pending confirmation
    monitoring activation   the Start Shift click: turns operational notifications on, nothing else
Unload countdown start = MAX(actual arrival, that day's warehouse opening). Start Shift and End Shift
never move a deadline. End Shift mutes operational notifications; collection and history continue.
DAY_BOUNDARY (05:00) is the reporting-day boundary; it never starts or closes a shift. A shift still
open across it shows a warning until someone confirms it or ends it.
"""
import json
from datetime import date, datetime, timedelta, timezone

from . import config, rules
from .rules import EASTERN
from .store import from_iso, iso

OPEN, CLOSED = "SHIFT ACTIVE", "SHIFT CLOSED — ALERTS MUTED"


def at_local(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=EASTERN).astimezone(timezone.utc)


def reporting_date(t):
    """Reporting day of instant t: the Eastern date, rolled back before DAY_BOUNDARY."""
    day = t.astimezone(EASTERN).date()
    return day if t >= at_local(day, config.DAY_BOUNDARY) else day - timedelta(days=1)


def openings(store, segments):
    """Warehouse opening per local date: {date: (time UTC, "confirmed" | "provisional")}.
    Confirmed = entered on the dashboard. Provisional = the earliest valid morning manual check-in that day
    (after DAY_BOUNDARY), pending confirmation. Never the Start Shift click."""
    out = {}
    for seg in segments:
        if seg.get("destination") != config.SITE_CODE:
            continue
        try:
            _, _, _, issues = rules.arrival(seg)
            manual = rules.seg_time(seg, "manualActualArrivalTimeStrFormat")
        except rules.BadTime:
            continue
        if manual is None or "MANUAL_ARRIVAL_INVALID" in issues:
            continue
        day = manual.astimezone(EASTERN).date()
        if manual < at_local(day, config.DAY_BOUNDARY):
            continue  # after-midnight check-ins belong to the previous night's operations
        if day not in out or manual < out[day][0]:
            out[day] = (manual, "provisional")
    for row in store.db.execute("SELECT key, value FROM meta WHERE key LIKE 'opening:%'"):
        rec = json.loads(row["value"])
        out[date.fromisoformat(row["key"].split(":", 1)[1])] = (from_iso(rec["time"]), "confirmed")
    return out


def context(store, segments=None):
    """What the rules need: warehouse openings (countdown start) and shift sessions (alert activation only)."""
    intervals = [(from_iso(s["started_at"]), from_iso(s["ended_at"])) for s in store.sessions()]
    segments = store.segments() if segments is None else segments
    return {"openings": openings(store, segments), "shifts": sorted(intervals)}


def earliest_manual_arrival(segments, day):
    """Earliest valid manual arrival at BWI on a reporting day: (time UTC, segment) or None. Reference only."""
    lower = at_local(day, config.DAY_BOUNDARY)
    upper = lower + timedelta(days=1)
    best = None
    for seg in segments:
        if seg.get("destination") != config.SITE_CODE:
            continue
        try:
            _, _, _, issues = rules.arrival(seg)
            manual = rules.seg_time(seg, "manualActualArrivalTimeStrFormat")
        except rules.BadTime:
            continue
        if manual is None or "MANUAL_ARRIVAL_INVALID" in issues or not (lower <= manual < upper):
            continue
        if best is None or manual < best[0]:
            best = (manual, seg)
    return best


def _ref(found):
    if not found:
        return None
    t, seg = found
    return json.dumps({"time": iso(t), "taskNo": seg.get("taskNo"), "segmentId": seg.get("id"),
                       "plate": seg.get("licensePlateNo"), "manualArrival": seg.get("manualActualArrivalTimeStrFormat")},
                      ensure_ascii=False)


def feishu_test_mode(store, default=None):
    """Test mode (record only, nothing sent): the dashboard switch (meta feishu_mode) wins over FEISHU_TEST_MODE."""
    mode = store.get_meta("feishu_mode")
    if mode in ("live", "test"):
        return mode == "test"
    return config.FEISHU_TEST_MODE if default is None else default


def feishu_live(store):
    """Live Feishu delivery is authorized one shift at a time (--authorize-feishu-shift): true only while the
    shift that used the authorization is open, or for the morning reminder while an authorization waits."""
    if not config.FEISHU_SHIFT_AUTHORIZATION:
        return True
    open_ = store.open_session()
    if open_ is not None:
        return store.get_meta("feishu_live_session") == str(open_["id"])
    return int(store.get_meta("feishu_shift_allowance") or 0) > 0


class Shifts:
    def __init__(self, store, notifier=None, now=None):
        self.store = store
        self.notifier = notifier

    def alerts_enabled(self, now=None):
        return self.store.open_session() is not None

    def update(self, now, segments):
        """Keep the earliest-manual-arrival reference and send the morning reminder. Never starts a shift."""
        day = reporting_date(now)
        found = earliest_manual_arrival(segments, day)
        if found:
            ref = _ref(found)
            if self.store.get_meta(f"earliest_manual:{day}") != ref:
                self.store.set_meta(f"earliest_manual:{day}", ref)
        remind_at = at_local(day, config.REMINDER_TIME)
        if (self.store.open_session() is None
                and remind_at <= now < remind_at + timedelta(minutes=config.REMINDER_WINDOW_MINUTES)
                and not self.store.get_meta(f"reminder:{day}")):
            self.store.set_meta(f"reminder:{day}", iso(now))
            self.store.shift_event(day, now, "morning reminder")
            if self.notifier:
                self.notifier.send_shift_reminder(now)

    # ---- user actions ----------------------------------------------------------
    def start(self, now, user):
        """Confirmed start = click time. A shift already open is ended at the same instant (restart)."""
        current = self.store.open_session()
        if current:
            self.store.end_session(current["id"], now, f"{user} (restarted shift)")
            self.store.shift_event(current["reporting_date"], now, "ended (restart)", user)
        day = reporting_date(now)
        sid = self.store.start_session(now, user, day, self.store.get_meta(f"earliest_manual:{day}"))
        self.store.shift_event(day, now, "started", user)
        if int(self.store.get_meta("feishu_shift_allowance") or 0) > 0:
            # One authorized live-Feishu shift: bind it to this session and use the authorization up.
            self.store.set_meta("feishu_live_session", str(sid))
            self.store.set_meta("feishu_shift_allowance", "0")
            self.store.shift_event(day, now, "feishu live delivery authorized for this shift", user)
        return sid

    def end(self, now, user):
        current = self.store.open_session()
        if current is None:
            return None
        self.store.end_session(current["id"], now, user)
        self.store.shift_event(current["reporting_date"], now, "ended", user)
        return current["id"]

    def set_opening(self, now, user, local_hhmm, day=None):
        """Confirm the warehouse opening time for a day (default: today's reporting day)."""
        day = day or reporting_date(now)
        t = at_local(day, local_hhmm)
        self.store.set_meta(f"opening:{day}", json.dumps({"time": iso(t), "by": user, "at": iso(now)}))
        self.store.shift_event(day, now, f"warehouse opening confirmed {local_hhmm}", user)
        return t

    def confirm_open(self, now, user):
        """Acknowledge that a shift left open across the reporting-day boundary is really still active."""
        current = self.store.open_session()
        if current:
            self.store.confirm_open_session(current["id"], now, user)
            self.store.shift_event(reporting_date(now), now, "confirmed still open", user)
        return current

    def open_overnight(self, now, session=None):
        """True when the open shift started on an earlier reporting day and nobody confirmed it since."""
        s = session or self.store.open_session()
        if not s:
            return False
        boundary = at_local(reporting_date(now), config.DAY_BOUNDARY)
        started = from_iso(s["started_at"])
        confirmed = from_iso(s["open_confirmed_at"])
        return started < boundary and (confirmed is None or confirmed < boundary)

    def _opening_view(self, day):
        confirmed = self.store.get_meta(f"opening:{day}")
        if confirmed:
            rec = json.loads(confirmed)
            return {"time_et": rules.fmt_et(from_iso(rec["time"])), "source": "confirmed", "by": rec.get("by")}
        ref = json.loads(self.store.get_meta(f"earliest_manual:{day}") or "null")
        if ref:
            return {"time_et": rules.fmt_et(from_iso(ref["time"])), "source": "provisional (earliest manual check-in)",
                    "by": None}
        return None

    def view(self, now):
        s = self.store.open_session()
        day = reporting_date(now)
        ref = json.loads(self.store.get_meta(f"earliest_manual:{day}") or "null")
        last = self.store.sessions()[-1] if self.store.sessions() else None
        fmt = lambda v: rules.fmt_et(from_iso(v)) if v else None
        return {
            "reporting_date": str(day),
            "state": "ACTIVE" if s else "CLOSED",
            "label": OPEN if s else CLOSED,
            "alerts_enabled": s is not None,
            "confirmed_start_et": fmt(s["started_at"]) if s else None,
            "started_by": s["started_by"] if s else None,
            "last_end_et": fmt(last["ended_at"]) if last and last["ended_at"] else None,
            "last_ended_by": last["ended_by"] if last and last["ended_at"] else None,
            "earliest_manual_ref": ref and dict(ref, time_et=fmt(ref["time"])),
            "opening": self._opening_view(day),
            "manual_arrival_without_shift": bool(ref) and s is None,
            "open_overnight_warning": self.open_overnight(now, s),
            "reminder_sent_et": fmt(self.store.get_meta(f"reminder:{day}")),
            "admin_token_required": bool(config.ADMIN_TOKEN),
            "feishu_live_this_shift": feishu_live(self.store),
            "feishu_authorization_pending": int(self.store.get_meta("feishu_shift_allowance") or 0) > 0,
            "events": self.store.shift_events(20),
        }
