"""TMS operation compliance at BWI (phase 3, 2026-10-14). Independent of the time rules.

Confirmed evidence (2026-10-14): check-in = valid manual arrival; departure = valid manual departure
(GPS departure or a downstream arrival never replaces it); a valid manual departure also confirms
loading. Reference: GOFO TMS V2 operation rate. The origin is responsible for loading and departure, the
destination for arrival and unloading. For each segment BWI is the origin, the destination or both.

Each check yields one finding with alert type OP:<OPERATION> and evidence["opStatus"]:
    VERIFIED      the operation is recorded                         -> stage OK (resolves any alert)
    PENDING       not due yet, or still inside the recording grace  -> stage WAITING (no alert)
    RISK          due and still not recorded (pending risk)         -> stage RISK (alert)
    MISSING       a later step is recorded but this one is not      -> stage MISSING (alert)
    UNCERTAIN     TMS data cannot tell                              -> stage SKIP (dashboard only)
    NOT_REQUIRED  the loading mode does not require it              -> stage SKIP
MISSING means "missing in TMS records"; it is never a KPI penalty. An empty field on its own is
UNCERTAIN, not MISSING. Field meanings below are taken from the TMS list API and still need business
verification (see README).
"""
from datetime import timedelta

from . import config
from .rules import BadTime, Finding, OK, SKIP, WAITING, arrival, countdown_start, fmt_et, inbound_arrival, seg_time

RISK, MISSING = "RISK", "MISSING"
STAGE = {"VERIFIED": OK, "PENDING": WAITING, "RISK": RISK, "MISSING": MISSING, "UNCERTAIN": SKIP,
         "NOT_REQUIRED": SKIP}
LOADING_MODES = {"Load Only", "Load and Unload"}     # origin mode that requires loading
UNLOADING_MODES = {"Unload Only", "Load and Unload"}  # destination mode that requires unloading
OPS_ZH = {"LOADING": "装车", "DEPARTURE": "发车", "ARRIVAL": "到车", "UNLOADING": "卸车"}


def _f(op, status, reason, status_zh, evidence, action="", deadline=None):
    ev = dict(evidence, opStatus=status, statusZh=status_zh, operation=op)
    return Finding(f"OP:{op}", STAGE[status], deadline, 0, reason, action, ev)


def _time(seg, key):
    try:
        return seg_time(seg, key)
    except BadTime:
        return None


def _left_bwi(seg, ctx):
    """True when a later segment of the same task departs from BWI (the truck moved on)."""
    others = ((ctx or {}).get("by_task") or {}).get(seg.get("taskNo"), [])
    order = seg.get("sortNum") or 0
    return any(o.get("placeOfOrigin") == config.SITE_CODE and (o.get("sortNum") or 0) > order
               and (o.get("actualDepartureTime") or o.get("manualActualDepartureTimeStrFormat")) for o in others)


def _manual_departure(seg):
    return _time(seg, "manualActualDepartureTimeStrFormat")


def loading(seg, now, ev):
    """Confirmed 2026-10-14: a valid manual departure means loading is complete and the truck dispatched."""
    mode = seg.get("originLoadingModeStr")
    if _manual_departure(seg):
        return _f("LOADING", "VERIFIED", "Manual departure recorded: loading complete", "已人工发车，装车视为完成", ev)
    if mode and mode not in LOADING_MODES:
        return _f("LOADING", "NOT_REQUIRED", f"Origin mode '{mode}': no loading at BWI", "无需装车", ev)
    if seg.get("loadBagCount") is not None or seg.get("loadedImg"):
        return _f("LOADING", "VERIFIED", f"Loading recorded ({seg.get('loadBagCount')} bags)",
                  f"TMS 已记录装车（{seg.get('loadBagCount')} 袋）", ev)
    if not mode:
        return _f("LOADING", "UNCERTAIN", "Origin loading mode missing; cannot tell if loading is required",
                  "TMS 未提供始发装卸模式，无法判断是否需要装车", ev)
    if _time(seg, "fenceActualDepartureTimeStrFormat") or seg.get("actualArrivalTime"):
        # Without a manual departure the loading evidence is incomplete, but that alone does not prove
        # loading was skipped; the missing manual departure is flagged by the departure check.
        return _f("LOADING", "UNCERTAIN", "Truck left without a manual departure; no loading count or photo",
                  "车辆已离场但无人工发车，无装车数量或照片，无法确认装车", ev)
    return _f("LOADING", "PENDING", "Not dispatched yet", "尚未人工发车，装车未确认", ev)


def departure(seg, now, ev, ctx=None):
    """Confirmed 2026-10-14: only a valid manual departure completes the TMS departure operation.
    GPS departure and downstream arrival show the truck left, never that the operation was done."""
    manual, gps = _manual_departure(seg), _time(seg, "fenceActualDepartureTimeStrFormat")
    ev = dict(ev, gpsDeparture=seg.get("fenceActualDepartureTimeStrFormat"),
              manualDeparture=seg.get("manualActualDepartureTimeStrFormat"))
    if manual:
        return _f("DEPARTURE", "VERIFIED", "Manual departure recorded", "TMS 已记录人工发车", ev)
    downstream = _time(seg, "actualArrivalTime")
    has_inbound, at_bwi = inbound_arrival(seg, ctx)
    if has_inbound and at_bwi is None:  # 2026-10-17: the truck has not reached BWI yet; nothing for BWI to do
        return _f("DEPARTURE", "PENDING", "Truck has not arrived at BWI yet; departure not judged", "车辆尚未到达 BWI", ev)
    if downstream:
        return _f("DEPARTURE", "MISSING", f"Next stop {seg.get('destination')} recorded arrival "
                  f"{fmt_et(downstream)}, but BWI has no manual departure",
                  f"下一站 {seg.get('destination')} 已于 {fmt_et(downstream)[5:]} 到车，但 BWI 缺少人工发车记录", ev,
                  "Record the manual departure in TMS")
    if gps:
        due = gps + timedelta(minutes=config.OP_GRACE_MINUTES)
        if now >= due:
            return _f("DEPARTURE", "MISSING", f"GPS departure {fmt_et(gps)}, no manual departure after "
                      f"{config.OP_GRACE_MINUTES} min", f"GPS 已于 {fmt_et(gps)[5:]} 离场，但缺少人工发车记录", ev,
                      "Record the manual departure in TMS", due)
        return _f("DEPARTURE", "PENDING", "GPS departure; manual departure not yet recorded",
                  "GPS 已离场，等待人工发车记录", ev, deadline=due)
    return _f("DEPARTURE", "PENDING", "No GPS or manual departure yet (planned time monitored)", "尚未发车", ev)


def arrival_op(seg, now, ctx, ev):
    _, _, aev, issues = arrival(seg)
    ev = dict(ev, gpsArrival=aev.get("gpsArrival"), manualArrival=aev.get("manualArrival"))
    if seg.get("manualActualArrivalTimeStrFormat") and "MANUAL_ARRIVAL_INVALID" not in issues:
        # Confirmed 2026-10-14: a valid manual arrival is the completed TMS check-in.
        return _f("ARRIVAL", "VERIFIED", "Manual check-in recorded", "TMS 已完成人工签到", ev)
    if seg.get("unLoadBagCount"):
        return _f("ARRIVAL", "MISSING", "Unloading recorded but no manual arrival check-in",
                  "TMS 已有卸车记录，但未记录人工到车签到", ev, "Record the arrival check-in in TMS")
    gps = None if "GPS_ARRIVAL_INVALID" in issues else _time(seg, "fenceActualArrivalTimeStrFormat")
    if gps:
        start = gps  # 2026-10-17: the opening-time rule is retired; the grace runs from the GPS arrival
        due = start + timedelta(minutes=config.OP_GRACE_MINUTES)
        if now >= due:
            return _f("ARRIVAL", "RISK", f"GPS arrival {fmt_et(gps)}, no manual check-in {config.OP_GRACE_MINUTES} min "
                      f"after {fmt_et(start)}", f"GPS 显示已于 {fmt_et(gps)[5:]} 到达，TMS 人工签到尚未完成", ev,
                      "Record the arrival check-in in TMS", due)
        return _f("ARRIVAL", "PENDING", "GPS arrival; recording grace running", "GPS 已到达，等待人工签到", ev, deadline=due)
    if seg.get("vehicleStatusStr") == "Arrived":
        return _f("ARRIVAL", "UNCERTAIN", "Status Arrived but no arrival time", "状态为已到达，但无到车时间", ev)
    return None  # not arrived


def unloading(seg, now, ctx, ev):
    arrived = seg.get("actualArrivalTime") or seg.get("manualActualArrivalTimeStrFormat")
    if not arrived:
        return None
    mode = seg.get("destinationLoadingModeStr")
    if not mode:
        return _f("UNLOADING", "UNCERTAIN", "Destination loading mode missing", "TMS 未提供目的地装卸模式", ev)
    if mode not in UNLOADING_MODES:
        return _f("UNLOADING", "NOT_REQUIRED", f"Destination mode '{mode}': no unloading at BWI", "无需卸车", ev)
    unloaded = seg.get("unLoadBagCount")
    if unloaded:
        return _f("UNLOADING", "VERIFIED", f"Unloading recorded ({unloaded} bags)", f"TMS 已记录卸车（{unloaded} 袋）", ev)
    moved_on = _left_bwi(seg, ctx)
    marked = bool(seg.get("unloadCompleteTimeStrFormat"))
    if moved_on or marked:
        later = "the truck departed BWI again" if moved_on else "unload was marked complete"
        later_zh = "车辆已从 BWI 再次发车" if moved_on else "TMS 已标记卸车完成"
        single = "multiple unloadings" not in (seg.get("handlingModeStr") or "").lower()
        if single and seg.get("loadBagCount"):
            return _f("UNLOADING", "MISSING", f"{later}, but no unloading scans are recorded",
                      f"{later_zh}，但没有卸车扫描记录", ev, "Check the unloading operation in TMS")
        return _f("UNLOADING", "UNCERTAIN", f"{later}, no unloading scans; BWI cargo quantity unknown",
                  f"{later_zh}，无卸车扫描；无法确认 BWI 是否有应卸货物", ev)
    return _f("UNLOADING", "PENDING", "No unloading recorded yet (see time alert)", "TMS 尚无卸车记录", ev)


def evaluate(seg, now, ctx=None):
    """Compliance findings for one segment (BWI origin and/or destination)."""
    ev = {k: seg.get(k) for k in ("loadBagCount", "unLoadBagCount", "originLoadingModeStr",
                                  "destinationLoadingModeStr", "handlingModeStr", "vehicleStatusStr")}
    out = []
    if seg.get("placeOfOrigin") == config.SITE_CODE:
        out += [loading(seg, now, ev), departure(seg, now, ev, ctx)]
    if seg.get("destination") == config.SITE_CODE:
        out += [arrival_op(seg, now, ctx, ev), unloading(seg, now, ctx, ev)]
    return [f for f in out if f is not None]
