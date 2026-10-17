"""Departure on-time rate (发车准点率) and anomaly detection, per 统计逻辑V2.

Each goArrive record is one route segment; segments of a multi-stop task share
taskNo and are ordered by sortNum. The statistic object is the segment's
departure point (placeOfOrigin).

V2 planned departure (串点线路 adjustment), when the upstream segment has arrived:
  1) upstream arrived inside a hub shift, or origin is a station:
       planned = MAX(upstream actual arrival + operation time, original planned)
  2) upstream arrived outside a hub shift (between shift end and next shift start):
       planned = MAX(next shift start + operation time, original planned)

Segment status:
  ON_TIME               actual departure <= planned
  LATE                  actual departure >  planned
  ARRIVED_NOT_DEPARTED  no actual departure, but next stop has an actual arrival (已到站未发车)
  OVERDUE               past planned, no departure and no arrival record yet (counted late)
  EXCLUDED              like OVERDUE but more than EXCLUDE_AFTER_HOURS old (剔除)
  PENDING               not yet due
"""
from collections import defaultdict
from datetime import datetime, timedelta

# ---- configuration: fill in from operations ---------------------------------
# Hub shifts as (start, end) "HH:MM"; an end earlier than start crosses midnight.
# A hub with no entry is treated as always in shift (rule 1).
HUB_SHIFTS = {
    # "EWR.H": [("06:00", "18:00"), ("20:00", "04:00")],
}
HUB_OP_MINUTES = defaultdict(lambda: 60)       # HUB班次操作时间, per hub
STATION_OP_MINUTES = defaultdict(lambda: 0)    # operation time at stations
# No departure and no next-stop arrival: still OVERDUE (counted late) within this
# window, then EXCLUDED (剔除) as a task that never ran.
EXCLUDE_AFTER_HOURS = 24

FMT = "%Y-%m-%d %H:%M:%S"
NOT_ON_TIME = {"LATE", "ARRIVED_NOT_DEPARTED", "OVERDUE"}


def is_hub(place):
    return bool(place) and place.endswith(".H")


def parse(ts):
    return datetime.strptime(ts, FMT) if ts else None


def _shift_windows(hub, around):
    """Concrete (start, end) datetimes for shifts starting the day before through the day after."""
    for day_offset in (-1, 0, 1):
        day = (around + timedelta(days=day_offset)).date()
        for s, e in HUB_SHIFTS.get(hub, []):
            start = datetime.combine(day, datetime.strptime(s, "%H:%M").time())
            end = datetime.combine(day, datetime.strptime(e, "%H:%M").time())
            if end <= start:
                end += timedelta(days=1)
            yield start, end


def _shift_base(hub, upstream_arrival):
    """Rule 1 vs 2: upstream arrival if inside a shift, else the next shift start."""
    if hub not in HUB_SHIFTS:
        return upstream_arrival
    windows = sorted(_shift_windows(hub, upstream_arrival))
    if any(start <= upstream_arrival <= end for start, end in windows):
        return upstream_arrival
    later = [start for start, _ in windows if start > upstream_arrival]
    return later[0] if later else upstream_arrival


def adjusted_planned_departure(seg, upstream):
    planned = parse(seg["plannedDepartureTime"])
    upstream_arrival = parse(upstream.get("actualArrivalTime")) if upstream else None
    if planned is None or upstream_arrival is None:
        return planned
    origin = seg.get("placeOfOrigin")
    if is_hub(origin):
        base = _shift_base(origin, upstream_arrival)
        op = HUB_OP_MINUTES[origin]
    else:
        base = upstream_arrival
        op = STATION_OP_MINUTES[origin]
    return max(base + timedelta(minutes=op), planned)


def classify(seg, planned, now):
    actual_dep = parse(seg.get("actualDepartureTime"))
    next_arrival = parse(seg.get("actualArrivalTime"))
    if actual_dep:
        return "ON_TIME" if actual_dep <= planned else "LATE"
    if next_arrival:
        return "ARRIVED_NOT_DEPARTED"
    if now <= planned:
        return "PENDING"
    return "OVERDUE" if now - planned <= timedelta(hours=EXCLUDE_AFTER_HOURS) else "EXCLUDED"


def evaluate(records, now):
    """Return one result dict per segment with V2 planned time and status."""
    by_task = defaultdict(list)
    for r in records:
        by_task[r["taskNo"]].append(r)

    results = []
    for segs in by_task.values():
        segs.sort(key=lambda r: (int(r.get("sortNum") or 0), r["plannedDepartureTime"] or ""))
        for i, seg in enumerate(segs):
            planned = adjusted_planned_departure(seg, segs[i - 1] if i else None)
            if planned is None:
                continue
            status = classify(seg, planned, now)
            actual = parse(seg.get("actualDepartureTime")) or now
            results.append({
                "key": seg.get("id") or f'{seg["taskNo"]}|{seg["placeOfOrigin"]}|{seg["plannedDepartureTime"]}',
                "taskNo": seg["taskNo"],
                "taskName": seg.get("taskName"),
                "licensePlateNo": seg.get("licensePlateNo"),
                "origin": seg.get("placeOfOrigin"),
                "destination": seg.get("destination"),
                "originalPlanned": seg["plannedDepartureTime"],
                "planned": planned.strftime(FMT),
                "actualDeparture": seg.get("actualDepartureTime") or "",
                "status": status,
                "delayMinutes": max(0, round((actual - planned).total_seconds() / 60))
                if status in ("LATE", "OVERDUE") else 0,
            })
    return results


def on_time_rate(results, day):
    """Per-origin on-time rate for segments whose (V2) planned departure falls on `day`."""
    stats = defaultdict(lambda: [0, 0])  # origin -> [on_time, total]
    for r in results:
        if r["planned"][:10] != day:
            continue
        if r["status"] not in NOT_ON_TIME | {"ON_TIME"}:
            continue
        stats[r["origin"]][1] += 1
        if r["status"] == "ON_TIME":
            stats[r["origin"]][0] += 1
    return dict(stats)
