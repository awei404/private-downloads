"""BWI internal alert rules (BWI_TMS_实时预警规则_V1.0). Pure functions, no I/O.

All times from the API are US Eastern wall-clock strings; they are converted to
UTC-aware datetimes so arithmetic stays correct across DST changes.

Departure (§3, confirmed): deadline = plannedDepartureTime; on time if
    actualDepartureTime <= deadline. Early is always fine. No 串点 adjustment.
Arrival (confirmed): a valid GPS/geofence arrival if present, else the manual
    arrival; the source, both timestamps and their difference are reported.
Unload (confirmed 2026-10-14): one UNLOAD alert per BWI arrival. Countdown start =
    MAX(arrival, start of the shift the arrival belongs to) (see shift.py); an arrival
    while no shift is active is PENDING until the next shift start is detected.
    Deadline = countdown start + 60 min; reminders at 30 min left and 5 min left
    (high priority, re-checked against TMS first); overdue after 60 min.
    Single-stop route: all cargo unloaded (unLoadBagCount >= loadBagCount).
    Multi-stop route:  first unloading scan recorded (unLoadBagCount > 0); BWI's
                       share is not checked and no short-quantity alert is raised.
    The API has counts, not scan times, so timing comes from poll observations kept
    by the store in seg["_obs"]: the last poll that saw the step incomplete and the
    first poll that saw it done. Results are "based on TMS records"; a window that
    crosses the deadline is UNCERTAIN; a missing count is unknown, never zero.
    unloadCompleteTime is kept as reference only.
Data checks (§4.4, SRS §3.4): flagged for verification, never counted as KPI.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

EASTERN = ZoneInfo("America/New_York")

# Alert stages, ordered by severity for escalation.
WAITING, WARN, OVERDUE, LATE, OK, CHECK = "WAITING", "WARN", "OVERDUE", "LATE", "OK", "CHECK"
UNCERTAIN = "UNCERTAIN"    # completion observed in a poll window that crosses the deadline
PENDING = "PENDING"        # countdown cannot start yet (no shift started); no alert
RISK, MISSING = "RISK", "MISSING"  # operation compliance: pending risk / missing in TMS records (compliance.py)
EVENT = "EVENT"            # one-time notice (vehicle arrival); recorded once per segment, never escalates
SKIP = "SKIP"              # cannot be judged yet (e.g. quantity due not verified); no alert
FINAL_STAGES = {LATE, OK, UNCERTAIN}

DATA_CHECKS = {
    "NO_PLANNED_DEPARTURE": "No planned departure time",
    "ARRIVAL_BEFORE_DEPARTURE": "Actual arrival is earlier than actual departure",
    "UNLOAD_WITHOUT_ARRIVAL": "Unloaded bags recorded but no arrival time",
    "GPS_ARRIVAL_INVALID": "GPS/geofence arrival is unparseable or earlier than departure; not used",
    "MANUAL_ARRIVAL_INVALID": "Manual arrival is unparseable or earlier than departure; not used",
    "ARRIVAL_SOURCES_DISAGREE": "GPS and manual arrival times differ by more than the configured limit",
    "ARRIVED_STATUS_NO_TIME": "Status is Arrived but actual arrival time is empty",
    "NO_LOAD_COUNT": "Loaded bag count is missing, so whole-truck unload cannot be judged",
    "MARKED_COMPLETE_SHORT": "TMS unload marked complete, but fewer bags unloaded than due at BWI",
    "BAD_TIMESTAMP": "Timestamp could not be parsed",
}


class BadTime(ValueError):
    pass


ZONE_LABELS = {"Eastern Time": "America/New_York", "Central Time": "America/Chicago",
               "Mountain Time": "America/Denver", "Pacific Time": "America/Los_Angeles"}


def parse_et(value, default_zone=EASTERN):
    """'2026-10-11 15:20:09' or '2026-10-11 15:20:09 (Pacific Time)' -> UTC datetime.
    A "(... Time)" label wins; otherwise default_zone (Eastern unless the caller knows better)."""
    if not value:
        return None
    text = str(value)
    try:
        local = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError as e:
        raise BadTime(value) from e
    zone = default_zone
    if "(" in text:
        label = text.rsplit("(", 1)[1].rstrip(") ")
        if label not in ZONE_LABELS:
            raise BadTime(value)
        zone = ZoneInfo(ZONE_LABELS[label])
    return local.replace(tzinfo=zone).astimezone(timezone.utc)


# Unambiguous epoch-millisecond twins of the formatted strings (verified 2026-10-16 against all records).
EPOCH_FIELDS = {"manualActualDepartureTimeStrFormat": "manualActualDepartureTime",
                "fenceActualDepartureTimeStrFormat": "fenceActualDepartureTime",
                "manualActualArrivalTimeStrFormat": "manualActualArrivalTime",
                "fenceActualArrivalTimeStrFormat": "fenceActualArrivalTime"}
ORIGIN_ZONE_FIELDS = {"plannedDepartureTime", "actualDepartureTime"}  # plain strings in the origin's timezone


def seg_time(seg, key):
    """A TMS timestamp from a segment as a timezone-aware UTC datetime.
    1. the epoch-millisecond twin if present; 2. the string's own "(... Time)" label; 3. the segment's origin
    (departure fields) or destination (arrival fields) group timezone. The strings are written in the
    origin's timezone for departures, so a CNO.H (Pacific) departure is never read as Eastern."""
    epoch = seg.get(EPOCH_FIELDS.get(key, ""))
    if isinstance(epoch, (int, float)) and epoch > 0 and seg.get(key):
        return datetime.fromtimestamp(epoch / 1000, timezone.utc).replace(microsecond=0)
    group = seg.get("originGroupTimeZone" if key in ORIGIN_ZONE_FIELDS or "Departure" in key else "destGroupTimeZone")
    try:
        zone = ZoneInfo(group) if group else EASTERN
    except Exception:
        zone = EASTERN
    return parse_et(seg.get(key), zone)


def fmt_et(dt):
    return dt.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M") if dt else "-"


@dataclass
class Finding:
    alert_type: str            # DEPARTURE | UNLOAD | DATA:<code>
    stage: str
    deadline: datetime = None  # UTC
    minutes: int = 0           # minutes left (WAITING/WARN) or late/overdue (OVERDUE/LATE)
    reason: str = ""
    action: str = ""
    evidence: dict = field(default_factory=dict)
    warn_level: int = None     # which reminder threshold (minutes left) was crossed
    priority: str = "normal"   # "high" for the strong unload reminder

    @property
    def rank(self):
        return stage_rank(self.stage, self.warn_level)


def stage_rank(stage, warn_level=None):
    """Escalation order. WARN ranks between 1 and 2: fewer minutes left = higher."""
    if stage == WARN:
        return 1 + (1440 - min(warn_level or 0, 1439)) / 1440
    return {WAITING: 0, SKIP: 0, PENDING: 0, EVENT: 0, RISK: 30, CHECK: 50, OVERDUE: 100, MISSING: 120, LATE: 200, OK: 200,
            UNCERTAIN: 200}.get(stage, 0)


def _minutes(delta):
    return int(delta.total_seconds() // 60)


def _countdown(alert_type, deadline, now, reason_pending, action, evidence, levels=None):
    if levels is None:
        levels = config.UNLOAD_WARN_MINUTES if alert_type == "UNLOAD" else config.WARN_MINUTES
    left = deadline - now
    if left < timedelta(0):
        return Finding(alert_type, OVERDUE, deadline, _minutes(-left) or 1,
                       f"{reason_pending}; deadline passed", action, evidence)
    level = next((m for m in reversed(levels) if left <= timedelta(minutes=m)), None)
    if level is not None:
        strong = alert_type == "UNLOAD" and len(levels) > 1 and level == levels[-1]
        label = f"strong reminder, {level} min left" if strong else f"reminder, {level} min left"
        return Finding(alert_type, WARN, deadline, _minutes(left), f"{reason_pending}; {label}",
                       action, evidence, warn_level=level, priority="high" if strong else "normal")
    return Finding(alert_type, WAITING, deadline, _minutes(left), reason_pending, action, evidence)


def _data(code, evidence, detail=""):
    return Finding(f"DATA:{code}", CHECK, reason=DATA_CHECKS[code] + (f" ({detail})" if detail else ""),
                   action="Verify the record in TMS", evidence=evidence)


def departure_times(seg):
    """(physical departure, source, manual departure). Confirmed 2026-10-14: GPS sets the physical departure
    time; a valid manual departure is the TMS departure operation (and confirms loading). GPS alone never
    completes the operation, and a downstream arrival never replaces it."""
    try:
        gps = seg_time(seg, "fenceActualDepartureTimeStrFormat")
    except BadTime:
        gps = None
    manual = seg_time(seg, "manualActualDepartureTimeStrFormat")
    if gps is not None:
        return gps, "GPS", manual
    if manual is not None:
        return manual, "manual", manual
    return None, None, None


def departure(seg, now):
    """Punctuality of the physical departure against the TMS planned departure (no HUB extension)."""
    physical, source, manual = departure_times(seg)
    evidence = {k: seg.get(k) for k in ("plannedDepartureTime", "fenceActualDepartureTimeStrFormat",
                                        "manualActualDepartureTimeStrFormat", "actualArrivalTime")}
    evidence.update(departureSource=source, tmsDepartureCompleted=manual is not None)
    planned = seg_time(seg, "plannedDepartureTime")
    out = []
    if planned is None:
        return [_data("NO_PLANNED_DEPARTURE", evidence)]
    if physical is not None:
        if physical <= planned:
            out.append(Finding("DEPARTURE", OK, planned, 0, f"Departed on time ({source})", "", evidence))
        else:
            out.append(Finding("DEPARTURE", LATE, planned, _minutes(physical - planned) or 1,
                               f"Departed {fmt_et(physical)} ({source}), after planned {fmt_et(planned)}", "",
                               evidence))
    else:
        out.append(_countdown("DEPARTURE", planned, now, "No GPS or manual departure recorded",
                              "Verify the truck has left and record the departure in TMS", evidence))
    arrival = seg_time(seg, "actualArrivalTime")
    if physical and arrival and arrival < physical:
        out.append(_data("ARRIVAL_BEFORE_DEPARTURE", evidence))
    return out


# ---- arrival and counts -------------------------------------------------------

def arrival(seg):
    """(arrival UTC, source, evidence, data-check codes). Valid GPS/geofence arrival, else manual,
    else TMS actualArrivalTime. GPS is invalid if unparseable or earlier than the departure."""
    gps_raw = seg.get("fenceActualArrivalTimeStrFormat")
    manual_raw = seg.get("manualActualArrivalTimeStrFormat")
    ev = {"gpsArrival": gps_raw, "manualArrival": manual_raw}
    issues = []
    departed = seg_time(seg, "actualDepartureTime")
    try:
        gps = seg_time(seg, "fenceActualArrivalTimeStrFormat")
    except BadTime:
        gps, issues = None, issues + ["GPS_ARRIVAL_INVALID"]
    if gps is not None and departed is not None and gps < departed:
        gps, issues = None, issues + ["GPS_ARRIVAL_INVALID"]
    try:
        manual = seg_time(seg, "manualActualArrivalTimeStrFormat")
    except BadTime:
        manual, issues = None, issues + ["MANUAL_ARRIVAL_INVALID"]
    if manual is not None and departed is not None and manual < departed:
        manual, issues = None, issues + ["MANUAL_ARRIVAL_INVALID"]
    if gps is not None and manual is not None:
        ev["manualMinusGpsMinutes"] = _minutes(manual - gps)  # >0: manual check-in later than GPS arrival
        limit = config.ARRIVAL_DIFF_FLAG_MINUTES
        if limit and abs(manual - gps) > timedelta(minutes=limit):
            issues.append("ARRIVAL_SOURCES_DISAGREE")
    # Confirmed 2026-10-17: use whichever of the manual check-in and the GPS geofence arrival exists; when both
    # exist, the earlier one.
    if gps is not None and (manual is None or gps <= manual):
        t, source = gps, "GPS"
    elif manual is not None:
        t, source = manual, "manual"
    else:
        t, source = seg_time(seg, "actualArrivalTime"), "TMS actualArrivalTime"
    source = source if t else None
    ev["arrivalSource"] = source
    return t, source, ev, issues


def inbound_arrival(seg, ctx):
    """For a segment leaving BWI: (has an earlier leg into BWI, BWI arrival time of that leg or None).
    Confirmed 2026-10-17: departure is only judged once the truck has arrived at BWI (manual check-in or GPS
    geofence arrival on the leg into BWI); a task that starts at BWI has no such leg."""
    order = seg.get("sortNum") or 0
    legs = [o for o in ((ctx or {}).get("by_task") or {}).get(seg.get("taskNo"), [])
            if o.get("destination") == config.SITE_CODE and (o.get("sortNum") or 0) < order]
    if not legs:
        return False, None
    leg = max(legs, key=lambda o: o.get("sortNum") or 0)
    try:
        return True, arrival(leg)[0]
    except BadTime:
        return True, None


def _multi_unload(seg):
    return "multiple unloadings" in (seg.get("handlingModeStr") or "").lower()


def quantity_due(seg):
    """(bags due at BWI or None, why None). loadBagCount on multi-stop routes includes cargo for other
    stops (confirmed 2026-10-12); those routes are judged on the first unloading scan instead."""
    if _multi_unload(seg):
        return None, "multi-stop route: loaded quantity includes cargo for other stops"
    loaded = seg.get("loadBagCount")
    if not loaded:  # None or 0: unknown, never treated as zero
        return None, "loaded bag count missing"
    return loaded, None


def unload_states(seg):
    """{"start": bool|None, "done": bool|None} as shown by TMS records. A missing count is unknown
    (None): it neither proves unloading has not started nor bounds a scan-time window."""
    unloaded = seg.get("unLoadBagCount")
    due, _ = quantity_due(seg)
    return {"start": None if unloaded is None else unloaded > 0,
            "done": None if unloaded is None or due is None else unloaded >= due}


def from_obs(value):
    return datetime.fromisoformat(value) if value else None


def _judge(alert_type, step, seg, deadline, now, action, evidence):
    """Timing of an observed step ('start' or 'done') against its deadline, from poll observations."""
    obs = seg.get("_obs") or {}
    seen, last_not = from_obs(obs.get(f"{step}_seen")), from_obs(obs.get(f"{step}_last_not"))
    first_poll = from_obs(obs.get("first_poll"))
    evidence = dict(evidence, firstObservedComplete=fmt_et(seen) if seen else None,
                    lastObservedIncomplete=fmt_et(last_not) if last_not else None)
    what = "First unloading scan" if step == "start" else "Complete unloading"
    if seen is None:
        if seg.get("unLoadBagCount") is None:
            pending = f"{what} not shown in TMS records (unloaded count missing; meaning unverified)"
        else:
            pending = f"{what} not shown in TMS records"
        return _countdown(alert_type, deadline, now, pending, action, evidence)
    if seen <= deadline:
        window = f"between {fmt_et(last_not)} and {fmt_et(seen)}" if last_not else f"by {fmt_et(seen)}"
        return Finding(alert_type, OK, deadline, 0, f"{what} observed {window}: on time based on available "
                       "records", "", evidence)
    if last_not is not None and last_not >= deadline:
        return Finding(alert_type, LATE, deadline, _minutes(last_not - deadline) or 1,
                       f"Overdue based on TMS records: still incomplete at {fmt_et(last_not)}, deadline "
                       f"{fmt_et(deadline)}; first observed complete {fmt_et(seen)}", "", evidence)
    if last_not is None and (first_poll is None or seen <= first_poll):
        return Finding(alert_type, SKIP, deadline, reason=f"{what} already shown when the monitor first saw this "
                       "task; no timing evidence, not judged", evidence=evidence)
    since = fmt_et(last_not) if last_not else "an unknown time (earlier counts missing)"
    return Finding(alert_type, UNCERTAIN, deadline, 0,
                   f"{what} observed between {since} and {fmt_et(seen)}, a window that crosses the deadline "
                   f"{fmt_et(deadline)}: timing uncertain", "", evidence)


def countdown_start(arrived, ctx):
    """(start, source) for the unload countdown, or (None, why) while pending.
    Start = MAX(actual arrival, warehouse opening on the arrival's local date). The opening is confirmed on
    the dashboard or, provisionally, the earliest valid morning manual check-in. Start Shift is not used."""
    if ctx is None or ctx.get("openings") is None:
        return arrived, "arrival"
    day = arrived.astimezone(EASTERN).date()
    opening = ctx["openings"].get(day)
    if opening is None:
        return None, f"warehouse opening for {day} not yet known (no confirmed time or morning manual check-in)"
    t, source = opening
    if arrived >= t:
        return arrived, "actual arrival (warehouse open)"
    return t, f"warehouse opening ({source}; truck arrived before opening)"


def unload(seg, now, ctx=None):
    """One UNLOAD finding per BWI arrival: deadline = countdown start + 60 min, requirement by route type."""
    arrived, source, evidence, issues = arrival(seg)
    evidence.update({k: seg.get(k) for k in ("loadBagCount", "unLoadBagCount", "handlingModeStr",
                                             "vehicleStatusStr")})
    evidence["unloadCompleteTime (reference only)"] = seg.get("unloadCompleteTimeStrFormat")
    unloaded = seg.get("unLoadBagCount")
    multi = _multi_unload(seg)
    due, why_no_due = quantity_due(seg)
    evidence["routeType"] = "multi-stop" if multi else "single-stop"
    evidence["requirement"] = ("first unloading scan within 60 min of arrival" if multi
                               else "all cargo unloaded within 60 min of arrival")
    if not multi:
        evidence["quantityDueAtBWI"] = due
    out = [_data(code, evidence) for code in dict.fromkeys(issues)]
    if arrived is not None:
        checked_in = bool(seg.get("manualActualArrivalTimeStrFormat")) and "MANUAL_ARRIVAL_INVALID" not in issues
        out.append(Finding("ARRIVAL", EVENT, arrived, 0,
                           f"Vehicle arrived {fmt_et(arrived)} ({source}); TMS check-in "
                           f"{'completed' if checked_in else 'not yet completed'}", "",
                           dict(evidence, checkInCompleted=checked_in)))
    if arrived is None:
        if unloaded:
            out.append(_data("UNLOAD_WITHOUT_ARRIVAL", evidence))
        elif seg.get("vehicleStatusStr") == "Arrived":
            out.append(_data("ARRIVED_STATUS_NO_TIME", evidence))
        return out  # not arrived yet: countdown has not started

    # Confirmed 2026-10-17: the compliance check is the FIRST unloading scan for every truck (KPI logic, only the
    # BWI-relevant part): arrival before 19:00 -> MAX(arrival + 60 min, 12:00 + 120 min that day); arrival at/after
    # 19:00 -> 14:00 the next day. Separately, a reminder goes out 60 min after arrival (any hour) if the truck is
    # not unloaded yet. Business hours (12:00-19:00) apply to receiving only; the opening-time rule is retired.
    deadline, basis = kpi_unload_deadline(arrived)
    evidence.update(countdownStart=fmt_et(arrived), deadlineBasis=basis,
                    requirement=f"first unloading scan by {fmt_et(deadline)}")
    out.append(_judge("UNLOAD", "start", seg, deadline, now, "Start unloading and scan the first bag in TMS", evidence))
    out.append(unload_60(seg, arrived, now, unloaded, due, multi, evidence))
    if not multi and due is not None and seg.get("unloadCompleteTimeStrFormat") and unloaded is not None and unloaded < due:
        out.append(_data("MARKED_COMPLETE_SHORT", evidence, f"{unloaded} of {due} bags"))
    return out


def kpi_unload_deadline(arrived):
    """(deadline, basis) for the first unloading scan. Times in America/New_York."""
    local = arrived.astimezone(EASTERN)
    day = local.date()

    def at(d, hhmm):
        h, m = map(int, hhmm.split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=EASTERN).astimezone(timezone.utc)

    if arrived >= at(day, config.RECEIVING_CLOSE):
        return at(day + timedelta(days=1), config.RECEIVING_BATCH_DEADLINE), "after 19:00: next day 14:00"
    batch = at(day, config.RECEIVING_BATCH_DEADLINE)
    plus60 = arrived + timedelta(minutes=config.UNLOAD_LIMIT_MINUTES)
    return (plus60, "arrival + 60 min") if plus60 >= batch else (batch, "12:00 + 120 min (14:00)")


def unload_60(seg, arrived, now, unloaded, due, multi, evidence):
    """Reminder 60 min after arrival when the truck is not unloaded: single-stop = unloaded bags < loaded bags
    (first scan when the loaded count is unknown), multi-stop = no first scan yet."""
    at60 = arrived + timedelta(minutes=config.UNLOAD_LIMIT_MINUTES)
    if not multi and due is not None:
        done = unloaded is not None and unloaded >= due
        progress = f"{unloaded if unloaded is not None else 0}/{due} bags"
    else:
        done = bool(unloaded)
        progress = f"first scan {'recorded' if unloaded else 'not recorded'}"
    ev = dict(evidence, at60=fmt_et(at60), progress60=progress)
    if done:
        return Finding("UNLOAD_60", OK, at60, 0, f"Unloaded ({progress})", "", ev)
    if now < at60:
        return Finding("UNLOAD_60", WAITING, at60, _minutes(at60 - now), f"60-min check at {fmt_et(at60)}", "", ev)
    return Finding("UNLOAD_60", WARN, at60, _minutes(now - at60), f"Arrived {fmt_et(arrived)}, 60 min passed and not "
                   f"unloaded yet ({progress})", "Finish unloading the truck and scan all bags in TMS", ev,
                   warn_level=60)


def anchor(seg):
    """Time used for the lookback window: arrival at BWI, else planned departure."""
    if seg.get("destination") == config.SITE_CODE:
        arrived = arrival(seg)[0]
        if arrived:
            return arrived
    return seg_time(seg, "plannedDepartureTime")


def in_window(seg, now):
    try:
        a = anchor(seg)
    except BadTime:
        return True
    return a is None or a >= now - timedelta(hours=config.LOOKBACK_HOURS)


def evaluate(seg, now, ctx=None, compliance=True):
    """All findings for one segment: time rules plus, independently, operation compliance
    (alert types OP:*). Segments outside the lookback window return []."""
    from . import compliance as ops
    if seg.get("_validity") or seg.get("_hidden"):
        # Hidden by hand on the dashboard: off the boards and no alerts until restored.
        # VOIDED: TMS taskStatus 6 (已作废). UNVERIFIED: missing from TMS, status not confirmed. Either way no
        # deadlines, warnings or notices; the dashboard shows the segment as voided / validity unverified.
        return []
    try:
        if not in_window(seg, now):
            return []
        from . import departures
        out = ops.evaluate(seg, now, ctx) if compliance else []
        if seg.get("placeOfOrigin") == config.SITE_CODE and seg.get("destination") not in departures.EXCLUDED:
            branch = departures.evaluate(seg, now, ctx)
            # Branch lines (ORF/RIC/DCA/IAD night runs) follow the branch schedule; other runs keep the TMS plan.
            out += branch if branch is not None else departure(seg, now)
        if seg.get("destination") == config.SITE_CODE:
            out += unload(seg, now, ctx)
        return out
    except BadTime as e:
        return [_data("BAD_TIMESTAMP", {"value": str(e)})]
