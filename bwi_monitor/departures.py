"""BWI branch-line departure monitor (2026-10-15).

Deadlines come from the user-supplied branch-line schedule (America/New_York), not from cargo cutoffs:
    ORF01 Day 0 23:00 · RIC01 Day 1 00:00 · DCA01 Day 1 02:00 · IAD01 Day 1 02:30
Every BWI outbound segment to a scheduled station uses that route's internal deadline, extra runs
included (confirmed 2026-10-15); the TMS planned departure is kept next to it and differences are
reported, never mixed in. Internal compliance is separate from official TMS punctuality. Segments that
TMS no longer returns (voided, 已作废) are excluded before this module is reached (rules.evaluate).

Completion needs a valid manual departure (GPS is not available for these lines and never replaces it):
manual <= deadline -> on time (early is always fine), later -> late. No manual departure -> reminder 30
min before the deadline, deadline alert at the deadline. A task only gets a deadline because TMS has a
segment for it; the schedule alone never creates an overdue task.
"""
from datetime import datetime, timedelta, timezone

from . import config
from .rules import (BadTime, EVENT, EASTERN, Finding, LATE, OK, PENDING, _countdown, _minutes, fmt_et, inbound_arrival,
                    seg_time)

EXCLUDED = {"BWI01"}  # on-site operation: no road-departure alerts


def schedule():
    """{destination: (label, 'HH:MM', day offset)} from BWI_BRANCH_SCHEDULE."""
    out = {}
    for item in config.BRANCH_SCHEDULE.split(","):
        if not item.strip():
            continue
        dest, rest = item.strip().split("=")
        hhmm, offset = rest.split("+")
        out[dest.strip()] = (dest.strip().rstrip("0123456789"), hhmm.strip(), int(offset))
    return out


def _at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=EASTERN).astimezone(timezone.utc)


def match(seg):
    """Internal deadline for a BWI outbound segment to a branch station, or None.
    Confirmed 2026-10-15: every run to the station (incl. extra runs such as BWI.H-ORF01(02:00)) uses the
    route's internal deadline. The operational day is the TMS planned departure's date, rolled back before
    DAY_BOUNDARY (05:00), so a 02:00 run belongs to the previous evening. The TMS planned time is kept
    separately (tms_planned, discrepancy_min) and is never used as the internal deadline."""
    if seg.get("placeOfOrigin") != config.SITE_CODE or seg.get("destination") in EXCLUDED:
        return None
    entry = schedule().get(seg.get("destination"))
    if entry is None:
        # 2026-10-17: every other BWI outbound route (JFK, EWR, PNE, ...) follows its TMS planned departure exactly.
        try:
            planned = seg_time(seg, "plannedDepartureTime")
        except BadTime:
            planned = None
        label = (seg.get("destination") or "").split(".")[0].rstrip("0123456789") or seg.get("destination")
        if planned is None:
            return {"route": label, "unmatched": "no TMS planned departure"}
        return {"route": label, "deadline": planned, "op_date": planned.astimezone(EASTERN).date(),
                "tms_planned": planned, "discrepancy_min": 0, "kind": "tms"}
    label, hhmm, offset = entry
    try:
        planned = seg_time(seg, "plannedDepartureTime")
    except BadTime:
        planned = None
    if planned is None:
        return {"route": label, "unmatched": "no TMS planned departure to tell the operational day"}
    local = planned.astimezone(EASTERN)
    # Confirmed 2026-10-17: TMS dates a night's branch runs on the next calendar day (ORF 23:00 runs are planned
    # D+1 23:00 although the truck leaves on D; RIC/DCA/IAD are planned D+1 00:00-03:00). The operating night is
    # therefore the TMS planned date minus one day: ORF 23:00 that night, RIC/DCA/IAD the following morning.
    op_date = local.date() - timedelta(days=1)
    deadline = _at(op_date + timedelta(days=offset), hhmm)
    return {"route": label, "deadline": deadline, "op_date": op_date, "tms_planned": planned,
            "discrepancy_min": _minutes(planned - deadline), "kind": "branch"}


def evaluate(seg, now, ctx=None):
    """Findings for a matched branch-line segment, or None when the segment is not a branch-line run."""
    m = match(seg)
    if m is None or "unmatched" in m:
        return None
    deadline = m["deadline"]
    manual = seg_time(seg, "manualActualDepartureTimeStrFormat")
    evidence = {"route": m["route"], "deadlineKind": m.get("kind", "branch"),
                "scheduledDeparture": fmt_et(deadline), "operationalDate": str(m["op_date"]),
                "tmsPlannedDeparture": fmt_et(m["tms_planned"]), "scheduleDiscrepancyMinutes": m["discrepancy_min"],
                "manualDeparture": seg.get("manualActualDepartureTimeStrFormat"),
                "gpsDeparture (support only)": seg.get("fenceActualDepartureTimeStrFormat")}
    has_inbound, arrived = inbound_arrival(seg, ctx)
    if has_inbound:
        evidence["bwiArrival"] = fmt_et(arrived) if arrived else None
        evidence["arrivedAfterDeadline"] = bool(arrived and arrived > deadline)
    if manual is None:
        if has_inbound and arrived is None:
            return [Finding("BRANCH_DEPARTURE", PENDING, deadline, 0,
                            "Truck has not arrived at BWI yet (no manual check-in or GPS arrival on the leg into BWI); "
                            "departure not judged", "", dict(evidence, truckAtBwi=False))]
        return [_countdown("BRANCH_DEPARTURE", deadline, now, "No manual departure in TMS",
                           "Check loading and complete the TMS departure", evidence,
                           levels=config.BRANCH_WARN_MINUTES)]
    # Full timezone-aware timestamps only: manual <= deadline is on time, however early.
    margin = int((deadline - manual).total_seconds())  # > 0 early, < 0 late
    late = _minutes(manual - deadline) if manual > deadline else 0
    # Reference only (2026-10-17): TMS punctuality = actual departure <= TMS planned departure. Internal compliance
    # above is decided by the internal deadline.
    evidence["tmsPunctual"] = manual <= m["tms_planned"]
    evidence.update(departureStatus="late" if margin < 0 else "on time", lateMinutes=late, marginSeconds=margin,
                    margin=margin_text(margin), manualDepartureEt=fmt_secs(manual), deadlineEt=fmt_secs(deadline))
    out = [Finding("BRANCH_DEPARTURE", LATE if margin < 0 else OK, deadline, late or 0,
                   f"Manual departure {fmt_secs(manual)} ET, {margin_text(margin)} vs the {m['route']} deadline "
                   f"{fmt_secs(deadline)} ET", "", evidence),
           # deadline = the internal deadline; the event time travels in evidence["eventTime"]
           Finding("DEPARTURE_DONE", EVENT, deadline, late, f"{m['route']} manual departure {fmt_secs(manual)} ET "
                   f"({margin_text(margin)})", "",
                   dict(evidence, eventTime=manual.isoformat(), eventOutcome="departed (late)" if margin < 0
                        else "departed (on time)"))]
    return out


def fmt_secs(dt):
    return dt.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def margin_text(seconds, lang="en"):
    """'early 2h 16m 13s' / 'late 0h 12m 0s' (zh: '提前 2小时16分13秒' / '晚点 0小时12分0秒')."""
    s = abs(seconds)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    if lang == "zh":
        return f"{'提前' if seconds >= 0 else '晚点'} {h}小时{m}分{sec}秒"
    return f"{'early' if seconds >= 0 else 'late'} {h}h {m}m {sec}s"
