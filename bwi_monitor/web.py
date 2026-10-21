"""Internal dashboard (SRS §8): stdlib HTTP server, JSON API + one HTML page."""
import json
import os
import re
import sys
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from urllib.parse import parse_qs, unquote, urlsplit

from . import compliance, config, departures, driver_arrivals, driver_export, driver_print, driver_scores, rules, shift
from .engine import SYSTEM_SEGMENT, rank
from .rules import fmt_et
from .store import Store, from_iso, utcnow

FIELDS = ["taskNo", "taskName", "licensePlateNo", "trailerNo", "placeOfOrigin", "destination", "vehicleStatusStr",
          "plannedDepartureTime", "actualDepartureTime", "plannedArrivalTime", "actualArrivalTime",
          "fenceActualArrivalTimeStrFormat", "manualActualArrivalTimeStrFormat",
          "unloadCompleteTimeStrFormat", "loadBagCount", "unLoadBagCount", "loadWaybillCount", "unLoadWaybillCount",
          "handlingModeStr", "originLoadingModeStr", "destinationLoadingModeStr", "dispatchTypeStr", "sortNum",
          "supplierName", "sealNo", "updateTime", "updateByName"]


def _seg_brief(seg):
    if not seg:
        return {}
    return {k: seg.get(k) for k in ("taskNo", "licensePlateNo", "placeOfOrigin", "destination", "vehicleStatusStr")}


def _alert_view(a, seg, now):
    d = dict(a)
    deadline = from_iso(a["deadline"])
    d["deadline_et"] = fmt_et(deadline)
    d["minutes_left"] = int((deadline - now).total_seconds() // 60) if deadline else None
    d["rank"] = rank(a["stage"], a["warn_level"])
    d["evidence"] = json.loads(a["evidence"] or "{}")
    d["closable"] = a["status"] == "open" and config.closable(a["alert_type"], a["stage"])
    d.update(_seg_brief(seg))
    return d


def _feishu_status(store):
    last = store.last_delivery("feishu")
    configured = bool(config.secret("FEISHU_WEBHOOK_URL", config.FEISHU_WEBHOOK_FILE))
    # The monitoring service records its own Feishu settings at start-up; a separate dashboard process (e.g. a
    # read-only preview) shows those instead of its own environment.
    svc = json.loads(store.get_meta("service_feishu_config") or "null") or {}
    enabled = svc.get("enabled", config.FEISHU_ENABLED)
    test_mode = shift.feishu_test_mode(store, svc.get("test_mode", config.FEISHU_TEST_MODE))
    return {
        "enabled": enabled, "test_mode": test_mode, "configured": configured,
        "mode_switch": store.get_meta("feishu_mode"), "shift_authorization": config.FEISHU_SHIFT_AUTHORIZATION,
        "departure_enabled": svc.get("departure_enabled", config.FEISHU_DEPARTURE_ENABLED),
        "live_this_shift": enabled and not test_mode and shift.feishu_live(store),
        "authorization_pending": int(store.get_meta("feishu_shift_allowance") or 0) > 0,
        "duplicates_prevented": int(store.get_meta("duplicates_prevented") or 0),
        "admin_channel": bool(config.secret("FEISHU_ADMIN_WEBHOOK_URL", config.FEISHU_ADMIN_WEBHOOK_FILE)),
        "last": {"at_et": fmt_et(from_iso(last["at"])), "channel": last["channel"], "ok": bool(last["ok"]),
                 "error": last["error"]} if last else None,
    }


def predict_arrival(seg, now):
    """(time, source label, arrived?) for a segment into BWI. Arrived: earlier of manual check-in and GPS.
    Otherwise a prediction: TMS estimate > actual departure + planned running time > now + running time (not yet
    departed, planned departure passed) > planned arrival."""
    try:
        arrived, source, _, _ = rules.arrival(seg)
    except rules.BadTime:
        arrived, source = None, None
    if arrived:
        return arrived, {"GPS": "实际到达·GPS", "manual": "实际到达·人工收车"}.get(source, "实际到达"), True

    def t(key):
        try:
            return rules.seg_time(seg, key)
        except rules.BadTime:
            return None
    eta, planned_arr, planned_dep = t("estimationArrivalTime"), t("plannedArrivalTime"), t("plannedDepartureTime")
    departed = rules.departure_times(seg)[0] if seg.get("fenceActualDepartureTimeStrFormat") or \
        seg.get("manualActualDepartureTimeStrFormat") else None
    runtime = planned_arr - planned_dep if planned_arr and planned_dep else None
    if eta:
        return eta, "TMS预计", False
    if departed and runtime:
        return departed + runtime, "实际发车+计划行驶时长", False
    if planned_dep and planned_dep < now - timedelta(hours=24):
        return None, "计划发车已过 24 小时仍未发车（不纳入预测）", False  # stale task that never ran
    if planned_dep and runtime and planned_dep < now:
        return now + runtime, "尚未发车·按现在推算", False
    return planned_arr, "计划到达", False


def receiving_window(now):
    """Receiving day (2026-10-17): trucks reaching BWI from 19:00 the evening before until 19:00 are that day's work.
    After 19:00 the window moves to the next day. Returns (day, start, end) with start/end in UTC."""
    local = now.astimezone(rules.EASTERN)
    day = local.date() if local.strftime("%H:%M") < config.RECEIVING_CLOSE else local.date() + timedelta(days=1)

    def at(d):
        h, m = map(int, config.RECEIVING_CLOSE.split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=rules.EASTERN).astimezone(timezone.utc)
    return day, at(day - timedelta(days=1)), at(day)


def route_stops(seg):
    """Stops from the task name, e.g. "CNO.H-BWI.H-JFK.H(14:30)" -> ["CNO.H", "BWI.H", "JFK.H"]."""
    return [s.strip() for s in (seg.get("taskName") or "").split("(")[0].split("-") if s.strip()]


def cargo(seg, manual=None):
    """BWI cargo of one inbound truck: (bags, pieces, source, note); the board counts pieces (票 = waybills). source: manual (entered by hand) | unloaded
    (TMS unload complete: what BWI actually unloaded) | loaded (all cargo on the leg is for BWI) | None (TMS
    cannot tell: the truck goes on to another stop with cargo loaded before BWI, e.g. CNO-BWI-JFK)."""
    if manual:
        return manual["bags"], manual["pieces"], "manual", f"{manual['user']} 手填"
    if seg.get("unloadCompleteTimeStrFormat") and seg.get("unLoadWaybillCount"):
        return seg.get("unLoadBagCount"), seg["unLoadWaybillCount"], "unloaded", "TMS 实卸"
    bags, pieces = seg.get("loadBagCount"), seg.get("loadWaybillCount")
    if not pieces:
        return None, None, None, "TMS 无装车票数"
    if rules._multi_unload(seg):
        stops, k = route_stops(seg), seg.get("sortNum") or 0
        if not (0 < k < len(stops) and stops[k] == config.SITE_CODE and stops[k - 1] == seg.get("placeOfOrigin")):
            return None, None, None, f"装车 {pieces:,} 票，串点线路无法确认 BWI 票数"
        onward = [s for s in stops[k + 1:] if s != seg.get("placeOfOrigin") and s not in config.OWN_STOPS]
        if onward:
            return None, None, None, f"装车 {pieces:,} 票含 {'、'.join(dict.fromkeys(s.replace('.H', '') for s in onward))} 的货"
    return bags, pieces, "loaded", "TMS 装车"


def receiving_board(segs, by_task, unloading_rows, now, manual_cargo=None):
    """Trucks into BWI for the receiving board: arrivals inside the current receiving window, every truck that has
    left its origin and is still on the way (any day; departed within IN_TRANSIT_MAX_DAYS), and not-yet-departed
    trucks predicted to arrive inside the window. Each row says which receiving day it belongs to."""
    day, start, end = receiving_window(now)
    unl = {r["segment_id"]: r for r in unloading_rows}
    rows = []
    for seg in segs:
        if seg.get("destination") != config.SITE_CODE or seg.get("_validity") or seg.get("_hidden"):
            continue
        when, source, arrived = predict_arrival(seg, now)
        if when is None:
            continue
        try:
            dep = rules.departure_times(seg)[0]
            planned_arr = rules.seg_time(seg, "plannedArrivalTime")
        except rules.BadTime:
            dep, planned_arr = None, None
        in_window = start <= when < end
        try:
            complete = rules.seg_time(seg, "unloadCompleteTimeStrFormat") if arrived else None
        except rules.BadTime:
            complete = None
        unloading = bool(arrived and complete is None and not compliance._left_bwi(seg, {"by_task": by_task}))
        if arrived:
            # still unloading from the previous window (arrived within 24 h) stays on the board
            if not in_window and not (unloading and when < start and now - when < timedelta(hours=24)):
                continue
        elif dep is not None:
            if now - dep > timedelta(days=config.IN_TRANSIT_MAX_DAYS):
                continue  # left its origin long ago and never arrived: a stale TMS task
        elif not in_window and not (when < start and now - when < timedelta(days=1)):
            continue
        late = not arrived and when < now
        bucket = ("today" if in_window else "prev" if arrived else "next" if end <= when < end + timedelta(days=1)
                  else "later" if when >= end else "overdue")
        legs = by_task.get(seg.get("taskNo"), [])
        chain = len(legs) >= 2 or len((seg.get("taskName") or "").split("(")[0].split("-")) > 2
        u = unl.get(seg["id"], {})
        bags, pieces, qty_source, qty_note = cargo(seg, (manual_cargo or {}).get(seg["id"]))
        rows.append({"unloading": unloading, "unload_complete_et": fmt_et(complete) if complete else None,
                     "qty_bags": bags, "qty_pieces": pieces, "qty_source": qty_source, "qty_note": qty_note,"segment_id": seg["id"], "taskNo": seg.get("taskNo"), "licensePlateNo": seg.get("licensePlateNo"),
                     "origin": seg.get("placeOfOrigin"), "placeOfOrigin": seg.get("placeOfOrigin"),
                     "destination": seg.get("destination"), "taskName": seg.get("taskName"),
                     "kind": "串点" if chain else "单站", "arrived": arrived, "departed": dep is not None,
                     "time": when.isoformat(), "time_et": fmt_et(when), "source": source, "late": late, "bucket": bucket,
                     "origin_departed_et": fmt_et(dep) if dep else None,
                     "planned_arrival_et": fmt_et(planned_arr) if planned_arr else None,
                     "vehicle_status": seg.get("vehicleStatusStr"),
                     "unload_status": u.get("status"), "unload_key": u.get("status_key"),
                     "progress": u.get("progress"), "first_scan_deadline": u.get("deadline"),
                     "first_scan_deadline_et": u.get("deadline_et")})
    rows.sort(key=lambda r: r["time"])
    return rows


def departure_board(store, segs, now, muted_ids=None):
    """Branch-line outbound tasks around now, sorted by deadline (BWI Departure Monitor)."""
    rows, by_task = [], {}
    for seg in segs:
        by_task.setdefault(seg.get("taskNo"), []).append(seg)
    for seg in segs:
        m = departures.match(seg)
        if not m or "unmatched" in m or seg.get("_hidden"):
            continue
        deadline = m["deadline"]
        manual = rules.seg_time(seg, "manualActualDepartureTimeStrFormat")
        if not (now - timedelta(hours=18) <= deadline <= now + timedelta(hours=30)) and (manual or deadline < now - timedelta(hours=48)):
            continue
        margin = None
        if manual and not seg.get("_validity"):
            from .departures import margin_text
            margin = margin_text(int((deadline - manual).total_seconds()))
        has_inbound, at_bwi = rules.inbound_arrival(seg, {"by_task": by_task})
        not_here = has_inbound and at_bwi is None and not manual
        inbound_eta = None
        if has_inbound and at_bwi is None:
            legs = [o for o in by_task.get(seg.get("taskNo"), []) if o.get("destination") == config.SITE_CODE]
            if legs:
                when, src, _ = predict_arrival(max(legs, key=lambda o: o.get("sortNum") or 0), now)
                inbound_eta = f"{fmt_et(when)}（{src}）" if when else None
        if seg.get("_validity") == "VOIDED":
            status = "Voided (已作废)"
        elif seg.get("_validity"):
            status = "Validity unverified"
        elif not_here:
            status = "Truck not at BWI"
        elif manual:
            late = rules._minutes(manual - deadline) if manual > deadline else 0
            status = f"Late ({late} min)" if late else "On Time"
        else:
            status = "Overdue" if now > deadline else "Pending"
        alerts = {a["alert_type"]: a for a in store.alerts_for_segment(seg["id"])}
        b, done = alerts.get("BRANCH_DEPARTURE"), alerts.get("DEPARTURE_DONE")
        notes = []
        if b and b["stage"] in ("WARN", "OVERDUE"):
            notes.append(("Alert" if b["stage"] == "OVERDUE" else "Reminder") +
                         (" sent" if b["last_notified_at"] else " recorded (muted / not live)"))
        if done:
            notes.append("Completed" + ("" if done["last_notified_at"] else f" — {done['outcome']}"))
        rows.append({"segment_id": seg["id"], "route": m["route"], "taskNo": seg.get("taskNo"),
                     "licensePlateNo": seg.get("licensePlateNo"), "placeOfOrigin": seg.get("placeOfOrigin"),
                     "destination": seg.get("destination"), "deadline_et": rules.fmt_et(deadline),
                     "tms_planned_et": rules.fmt_et(m["tms_planned"]), "discrepancy_min": m["discrepancy_min"],
                     "manual_et": rules.fmt_et(manual) if manual else None, "margin": margin,
                     "op_date": str(m["op_date"]), "chain": has_inbound, "kind": m.get("kind", "branch"),
                     "bwi_arrival_et": fmt_et(at_bwi) if at_bwi else None, "inbound_eta": inbound_eta,
                     "minutes_left": None if manual or seg.get("_validity") or not_here else int((deadline - now).total_seconds() // 60),
                     "validity_detail": seg.get("_validity_detail"),
                     "status": status, "notification": "; ".join(notes) or "-", "deadline": deadline.isoformat()})
    rows.sort(key=lambda r: (r["manual_et"] is not None, r["deadline"]))
    return rows


UNLOAD_STATUS = {  # stage -> (key, 中文) for the unloading board; display only, no rule changes
    "WAITING": ("open", "待卸车"), "WARN": ("warn", "即将超时"), "OVERDUE": ("overdue", "已超时（TMS记录）"),
    "OK": ("done", "已完成"), "LATE": ("late", "超时完成（TMS记录）"), "UNCERTAIN": ("uncertain", "时间不确定"),
    "PENDING": ("pending", "等待开门"), "SKIP": ("unknown", "监控开始前已卸·未判定时效"),
}


def unloading_row(seg, f, now):
    ev = f.evidence or {}
    multi = ev.get("routeType") == "multi-stop"
    unloaded, loaded = seg.get("unLoadBagCount"), seg.get("loadBagCount")
    if multi:
        progress = f"首枪已扫描（{unloaded} 袋）" if unloaded else "尚无卸车扫描"
    else:
        progress = f"{unloaded or 0} / {loaded} 袋" if loaded else ("无装车数量" if unloaded is None else f"{unloaded} 袋")
    key, label = UNLOAD_STATUS.get(f.stage, ("unknown", f.stage))
    if f.stage == "WAITING" and unloaded:
        key, label = "working", "卸车中"
    if f.stage == "WARN" and f.priority == "high":
        label = "即将超时（5分钟）"
    if ev.get("quantityCheck") == "short" and f.stage == "UNCERTAIN":
        key, label = "qty", f"按时完成·数量不符（{unloaded}/{loaded}）"
    elif ev.get("quantityCheck") == "short" and f.stage == "LATE":
        label = f"完成超时·数量不符（{unloaded}/{loaded}）"
    arrival = ev.get("gpsArrival") if ev.get("arrivalSource") == "GPS" else ev.get("manualArrival")
    start = rules.parse_et(ev["countdownStart"] + ":00") if ev.get("countdownStart") else None
    return {"segment_id": seg["id"], "taskNo": seg.get("taskNo"), "licensePlateNo": seg.get("licensePlateNo"),
            "placeOfOrigin": seg.get("placeOfOrigin"), "destination": seg.get("destination"),
            "route": f"{seg.get('placeOfOrigin')} → {seg.get('destination')}",
            "arrival_et": (arrival or "")[:16] or None, "arrival_source": ev.get("arrivalSource"),
            "type": "串点（首枪）" if multi else "单站（整车卸完）", "progress": progress,
            "countdown_start_et": ev.get("countdownStart"), "deadline": f.deadline.isoformat() if f.deadline else None,
            "deadline_et": fmt_et(f.deadline) if f.deadline else None,
            "elapsed_min": int((now - start).total_seconds() // 60) if start and now > start else None,
            "tms_complete_et": ev.get("tmsUnloadComplete"), "quantity_check": ev.get("quantityCheck"),
            "status_key": key, "status": label, "reason": f.reason}


def _is_time(alert_type):
    return alert_type in ("DEPARTURE", "UNLOAD", "UNLOAD_60", "BRANCH_DEPARTURE")


def driver_arrival_settings():
    return {"readonly": os.environ.get("BWI_DASHBOARD_READONLY") == "1",
            "admin_token_required": bool(config.ADMIN_TOKEN), "timezone": "America/New_York"}


def driver_score_detail(store, seg, now):
    if not seg:
        return None
    snap = store.driver_scores.snapshot()
    profile = snap["profiles"].get(seg.get("taskNo"))
    siblings = [s for s in store.segments() if s.get("taskNo") == seg.get("taskNo")]
    if not driver_scores.in_task_scope(seg, siblings, profile):
        return None
    return driver_scores.task_view(seg, store.driver_arrivals.view(seg), profile,
                                   snap["reviews"].get(seg.get("taskNo")))


def driver_board(store, now, query=""):
    """Filters affect exported tasks; driver averages always retain the stated 30-day window."""
    args = parse_qs(query, keep_blank_values=True)
    if set(args) - {"start", "end", "route", "include_phone"} or any(len(v) != 1 for v in args.values()):
        raise driver_arrivals.ValidationError("筛选参数无效。")
    today = shift.reporting_date(now)
    values = {k: v[0] for k, v in args.items()}
    try:
        start = values.get("start") or str(today - timedelta(days=29))
        end = values.get("end") or str(today)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            raise ValueError()
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError()
    except ValueError:
        raise driver_arrivals.ValidationError("请填写有效日期范围，开始日期不能晚于结束日期。") from None
    route = values.get("route") or ""
    if route not in ("", "ORF01", "RIC01", "DCA01", "IAD01"):
        raise driver_arrivals.ValidationError("仅可筛选四条 BWI 始发支线。")
    if values.get("include_phone", "0") not in ("0", "1"):
        raise driver_arrivals.ValidationError("电话导出选项无效。")
    records, appointments = store.driver_arrivals.snapshot()
    data = store.driver_scores.board(store.segments(), records, appointments, now)
    data["tasks"] = [t for t in data["tasks"] if t.get("op_date") and start <= t["op_date"] <= end
                     and (not route or t.get("destination") == route)]
    keys = {(t.get("profile") or {}).get("driver_key") for t in data["tasks"]}
    data["drivers"] = [d for d in data["drivers"] if d["driver_key"] in keys]
    data["filter"] = {"start": start, "end": end, "route": route}
    data["settings"] = driver_arrival_settings()
    return data


def hidden_rows(store, segs):
    """Tasks hidden by hand, newest first, for the restore list on the receiving / departure tabs."""
    rows = []
    for seg_id, h in store.hidden().items():
        seg = segs.get(seg_id) or {}
        rows.append({"segment_id": seg_id, "board": "recv" if seg.get("destination") == config.SITE_CODE else "send",
                     "at_et": fmt_et(from_iso(h["at"])), "user": h["user"], "reason": h["reason"], **_seg_brief(seg)})
    return sorted(rows, key=lambda r: r["at_et"] or "", reverse=True)


def build_state(store, now):
    last = store.last_poll()
    last_ok = store.last_poll(ok_only=True)
    ok_at = from_iso(last_ok["finished_at"]) if last_ok else None
    stale = ok_at is None or now - ok_at > timedelta(minutes=config.STALE_AFTER_MINUTES)

    segs = {s["id"]: s for s in store.segments()}
    by_task = {}
    for s in segs.values():
        by_task.setdefault(s.get("taskNo"), []).append(s)
    ctx = dict(shift.context(store, list(segs.values())), by_task=by_task)
    recent = store.recent_alerts(now, hours=24)
    muted = store.muted_alert_ids([a["id"] for a in recent])
    open_alerts, closed = [], []
    for a in recent:
        view = _alert_view(a, segs.get(a["segment_id"]), now)
        view["muted"] = a["id"] in muted
        (open_alerts if a["status"] == "open" else closed).append(view)
    open_alerts.sort(key=lambda v: (-v["rank"], v["deadline"] or ""))

    upcoming, pending, compliance, unloading = [], [], [], []
    for seg in segs.values():
        ops = {}
        for f in rules.evaluate(seg, now, ctx):
            if f.alert_type == "UNLOAD":
                row = unloading_row(seg, f, now)
                arrived = rules.arrival(seg)[0]
                if row["status_key"] in ("open", "working", "warn", "overdue", "pending") or (
                        arrived and now - arrived <= timedelta(hours=24)):
                    unloading.append(row)
            if f.alert_type.startswith("OP:"):
                ops[f.alert_type[3:]] = {"status": f.evidence.get("opStatus"), "reason": f.reason}
            if f.alert_type == "UNLOAD" and f.stage == rules.PENDING:
                pending.append({"segment_id": seg["id"], "alert_type": f.alert_type, "stage": f.stage,
                                "reason": f.reason, "arrival_et": f.evidence.get("gpsArrival") or
                                f.evidence.get("manualArrival"), **_seg_brief(seg)})
            if f.alert_type in ("DEPARTURE", "UNLOAD") and f.stage in (rules.WAITING, rules.WARN, rules.OVERDUE):
                upcoming.append({"segment_id": seg["id"], "alert_type": f.alert_type, "stage": f.stage,
                                 "warn_level": f.warn_level, "priority": f.priority,
                                 "countdown_start_et": f.evidence.get("countdownStart"),
                                 "deadline_et": fmt_et(f.deadline),
                                 "minutes_left": int((f.deadline - now).total_seconds() // 60),
                                 **_seg_brief(seg)})
        if ops and any(o["status"] not in ("VERIFIED", "NOT_REQUIRED") for o in ops.values()):
            compliance.append({"segment_id": seg["id"], "ops": ops, **_seg_brief(seg)})
    upcoming.sort(key=lambda u: u["minutes_left"])
    order = {"MISSING": 0, "RISK": 1, "UNCERTAIN": 2, "PENDING": 3}
    compliance.sort(key=lambda c: min(order.get(o["status"], 9) for o in c["ops"].values()))

    ops_open = [a for a in open_alerts if not _is_time(a["alert_type"]) and a["segment_id"] != SYSTEM_SEGMENT]
    board = departure_board(store, list(segs.values()), now)
    receiving = receiving_board(list(segs.values()), by_task, unloading, now, store.manual_cargo())
    arrival_records, appointments = store.driver_arrivals.snapshot()
    scoring = store.driver_scores.snapshot()
    for row in board + receiving + unloading:
        seg = segs[row["segment_id"]]
        task_no = seg.get("taskNo")
        row["driver_arrival"] = driver_arrivals.view(seg, arrival_records.get(task_no), appointments.get(task_no))
        profile = scoring["profiles"].get(task_no)
        if driver_scores.in_task_scope(seg, by_task.get(task_no, []), profile):
            row["driver_score"] = driver_scores.task_view(seg, row["driver_arrival"], profile,
                                                         scoring["reviews"].get(task_no))
        else:
            row["driver_arrival"]["eligible"] = False
            row["driver_score"] = None
    urgency = {"overdue": 0, "warn": 1, "working": 2, "open": 3, "pending": 4, "qty": 5, "uncertain": 5, "late": 6,
               "done": 7, "unknown": 8}
    unloading.sort(key=lambda r: (urgency.get(r["status_key"], 9), r["deadline"] or "9"))
    valid = [r for r in board if not r["status"].startswith(("Voided", "Validity"))]
    cards = {
        "dep_waiting": sum(r["status"] in ("Pending", "Overdue") for r in valid),
        "dep_done": sum(r["manual_et"] is not None for r in valid),
        "unload_waiting": sum(r["status_key"] in ("open", "working", "warn", "overdue") for r in unloading),
        "near_deadline": sum(r["status"] == "Pending" and r["minutes_left"] is not None and 0 <= r["minutes_left"] <= 30
                             for r in valid) + sum(r["status_key"] == "warn" for r in unloading),
        "overdue": sum(r["status"] == "Overdue" for r in valid) + sum(r["status_key"] == "overdue" for r in unloading),
        "op_issues": sum(a["stage"] in ("MISSING", "RISK") for a in ops_open),
    }
    return {
        "now_iso": now.isoformat(),
        "cards": cards,
        "driver_arrival_enabled": True,
        "driver_scoring_enabled": True,
        "driver_arrival_settings": driver_arrival_settings(),
        "unloading": unloading[:400],
        "receiving": receiving,
        "receiving_window": dict(zip(("day", "start_et", "end_et"), (lambda d, s, e: (str(d), fmt_et(s), fmt_et(e)))(
            *receiving_window(now)))),
        "reporting_date": str(shift.reporting_date(now)),
        "now_et": fmt_et(now),
        "shift": shift.Shifts(store).view(now),
        "feishu": _feishu_status(store),
        "health": {
            "last_poll_et": fmt_et(from_iso(last["finished_at"])) if last else None,
            "last_poll_ok": bool(last["ok"]) if last else None,
            "last_error": last["error"] if last and not last["ok"] else None,
            "last_ok_et": fmt_et(ok_at),
            "records": last_ok["records"] if last_ok else 0,
            "stale": stale,
            "pid": os.getpid(), "version": _version(),
            # the TMS login is missing or rejected: the dashboard asks for a new token
            "auth_expired": bool(last and not last["ok"] and any(
                w in (last["error"] or "") for w in ("401", "Unauthorized", "GOFO_AUTH is not set", "token"))),
            "poll_minutes": config.POLL_SECONDS // 60,
        },
        "counts": {
            "overdue": sum(a["stage"] == "OVERDUE" for a in open_alerts),
            "warning": sum(a["stage"] == "WARN" for a in open_alerts),
            "op_missing": sum(a["stage"] == "MISSING" for a in ops_open),
            "op_risk": sum(a["stage"] == "RISK" for a in ops_open),
            "data": sum(a["alert_type"].startswith("DATA:") for a in open_alerts),
            "late_24h": sum(a["stage"] == "LATE" for a in closed),
            "uncertain_24h": sum(a["stage"] == "UNCERTAIN" for a in closed),
            "monitoring": len(upcoming),
            "active_trucks": sum(u["alert_type"] == "UNLOAD" for u in upcoming),
            "pending_alerts": sum(a["segment_id"] != SYSTEM_SEGMENT for a in open_alerts),
            "pending_start": len(pending),
        },
        "departures": board,
        "hidden": hidden_rows(store, segs),
        "time_alerts": [a for a in open_alerts if _is_time(a["alert_type"])],
        "compliance_alerts": ops_open,
        "compliance": compliance[:300],
        "pending": pending[:300],
        "system": [a for a in open_alerts if a["segment_id"] == SYSTEM_SEGMENT],
        "closed": closed[:300],
        "upcoming": upcoming[:300],
    }


def segment_detail(store, seg_id, now):
    seg = store.segment(seg_id)
    if seg is None:
        return None
    alerts = store.alerts_for_segment(seg_id)
    ids = [a["id"] for a in alerts]
    task_segments = [s for s in store.segments() if s.get("taskNo") == seg.get("taskNo")]
    task_segments.sort(key=lambda s: s.get("sortNum") or 0)
    score = driver_score_detail(store, seg, now)
    return {
        "segment": {k: seg.get(k) for k in FIELDS},
        "driver_arrival_enabled": True,
        "driver_arrival": store.driver_arrivals.view(seg) if score is not None else None,
        "driver_scoring_enabled": True,
        "driver_score": score,
        "driver_arrival_settings": driver_arrival_settings(),
        "observations": seg.get("_obs") or {},
        "findings": [{"alert_type": f.alert_type, "stage": f.stage, "deadline_et": fmt_et(f.deadline),
                      "minutes": f.minutes, "reason": f.reason, "evidence": f.evidence}
                     for f in rules.evaluate(seg, now, dict(shift.context(store),
                                                            by_task={seg.get("taskNo"): [s for s in store.segments()
                                                                     if s.get("taskNo") == seg.get("taskNo")]}))],
        "task_segments": [{"id": s["id"], **_seg_brief(s), "sortNum": s.get("sortNum")} for s in task_segments],
        "alerts": [_alert_view(a, seg, now) for a in alerts],
        "events": store.alert_events(ids),
        "notifications": store.notifications_for(ids),
        "history_versions": len(store.segment_history(seg_id)),
    }


def _check_token(auth):
    from . import tms
    tms.check_token(auth)


class Handler(BaseHTTPRequestHandler):
    store = None
    driver_sync = None
    lock = threading.Lock()
    check_token = staticmethod(_check_token)  # replaced in tests

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def _guard(self, handler):
        """A failing request is logged with its traceback and answered with the error, never a dropped connection
        (the page then says what went wrong instead of 'cannot connect')."""
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            import traceback
            print(f"dashboard request failed: {self.command} {self.path}", file=sys.stderr, flush=True)
            traceback.print_exc()
            try:
                self._send(500, {"error": f"服务器内部错误 {type(e).__name__}: {e}"})
            except Exception:
                pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _get(self):
        now = utcnow()
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            try:
                host = urlsplit("http://" + (self.headers.get("Host") or ""))
                trusted = self._trusted_driver_arrival_host(host)
            except ValueError:
                trusted = False
            if not trusted:
                return self._send(403, {"error": "请从本机或服务监听地址打开看板。"})
        with self.lock:
            if parsed.path in ("/", "/index.html", "/dashboard-v2"):
                page = "dashboard_v2.html" if parsed.path == "/dashboard-v2" else os.environ.get("BWI_DASHBOARD_FILE", "dashboard.html")
                with open(os.path.join(os.path.dirname(__file__), os.path.basename(page)), "rb") as fh:
                    return self._send(200, fh.read(), "text/html")
            if parsed.path.startswith("/api/driver-checkin/"):
                match = re.fullmatch(r"/api/driver-checkin/([0-9]+)\.html", parsed.path)
                if not match or parsed.query:
                    return self._send(400, {"error": "打印任务参数无效。"})
                if config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
                    return self._send(403, {"error": "打印需要管理员口令。"})
                seg = self.store.segment(int(match[1]))
                task = driver_score_detail(self.store, seg, now)
                if not task:
                    return self._send(404, {"error": "仅可打印有效的 BWI 始发四支线任务。"})
                return self._send(200, driver_print.render(task, seg), "text/html")
            if parsed.path in ("/api/drivers", "/api/driver-export.xlsx"):
                try:
                    if parsed.path.endswith(".xlsx") and config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
                        return self._send(403, {"error": "导出需要管理员口令。"})
                    data = driver_board(self.store, now, parsed.query)
                except (ValueError, driver_arrivals.ValidationError) as exc:
                    return self._send(400, {"error": str(exc)})
                data["sync"] = self.driver_sync.status() if self.driver_sync else {
                    "running": False, "error": "此入口未启动司机同步，请使用司机看板入口。"}
                if parsed.path == "/api/drivers":
                    return self._send(200, data)
                include_phone = parse_qs(parsed.query).get("include_phone", ["0"])[0] == "1"
                payload = driver_export.export_scores(data, include_phone=include_phone, exported_at=fmt_et(now))
                self.send_response(200)
                self.send_header("Content-Type", driver_export.MIME)
                self.send_header("Content-Disposition", f'attachment; filename="BWI_driver_scores_{now.astimezone(rules.EASTERN):%Y%m%d_%H%M}.xlsx"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(payload)
            if self.path == "/api/state":
                return self._send(200, build_state(self.store, now))
            if self.path.startswith("/api/segment/"):
                detail = segment_detail(self.store, int(self.path.rsplit("/", 1)[1]), now)
                return self._send(200 if detail else 404, detail or {"error": "not found"})
            if self.path == "/healthz":
                return self._send(200, build_state(self.store, now)["health"])
        self._send(404, {"error": "not found"})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _trusted_driver_arrival_host(self, requested):
        """Trust the actual listener/connection address, not two attacker-supplied matching headers."""
        try:
            bound_host, bound_port = self.server.server_address[:2]
            if (requested.port or 80) != bound_port:
                return False
            bound = ip_address(bound_host)
            if bound.is_unspecified:
                # A wildcard listener serves several interfaces; use the concrete interface receiving this request.
                bound = ip_address(self.connection.getsockname()[0])
            if bound.is_unspecified:
                return False
            hostname = requested.hostname.rstrip(".").lower()
            if bound.is_loopback and hostname == "localhost":
                return True
            # 2026-10-21: other warehouse computers open the dashboard by this computer's name (e.g. http://BWI-PC:8766);
            # accept our own host name and configured names, still never an arbitrary (rebinding) name.
            if not bound.is_loopback and hostname in _own_host_names():
                return True
            address = ip_address(hostname)
            return address.is_loopback if bound.is_loopback else address == bound
        except (ValueError, AttributeError, TypeError, IndexError):
            return False

    def _driver_arrival_body(self, required_keys=None):
        required_keys = {"actual_arrival_et", "version"} if required_keys is None else set(required_keys)
        # Requiring JSON and a same-origin browser POST prevents form-based CSRF even when no admin token is set.
        origin, host = self.headers.get("Origin"), self.headers.get("Host")
        try:
            if not origin or not host or any(c.isspace() for c in origin + host):
                raise ValueError()
            sent, here = urlsplit(origin), urlsplit("http://" + host)
            for part in (sent, here):
                if not part.hostname or part.username is not None or part.password is not None or \
                        part.path or part.query or part.fragment:
                    raise ValueError()
            if sent.scheme != "http" or (sent.hostname, sent.port or 80) != (here.hostname, here.port or 80):
                raise ValueError()
            if not self._trusted_driver_arrival_host(here):
                raise ValueError()
        except ValueError:
            self._send(403, {"error": "请在当前看板页面内保存到场记录。"})
            return None
        if config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
            self._send(403, {"error": "需要管理员令牌才能保存到场记录。"})
            return None
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._send(415, {"error": "到场记录只接受 JSON 请求。"})
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 8192 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(body, dict) or set(body) != required_keys:
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "请求格式无效，请重新打开表单后重试。"})
            return None
        return body

    def _save_driver_arrival(self):
        match = re.fullmatch(r"/api/driver-arrival/([0-9]{1,18})", self.path)
        if not match:
            return self._send(400, {"error": "任务航段编号无效。"})
        body = self._driver_arrival_body()
        if body is None:
            return
        segment_id = int(match.group(1))
        with self.lock:
            try:
                result = self.store.driver_arrivals.save(segment_id, body["actual_arrival_et"], body["version"], utcnow())
            except driver_arrivals.ConflictError as exc:
                return self._send(409, {"error": str(exc),
                                        "driver_arrival": self.store.driver_arrivals.view(self.store.segment(segment_id))})
            except driver_arrivals.NotFoundError as exc:
                return self._send(404, {"error": str(exc)})
            except driver_arrivals.ValidationError as exc:
                return self._send(400, {"error": str(exc)})
            except sqlite3.OperationalError:
                return self._send(503, {"error": "暂时无法保存到场记录，请稍后重新读取记录并重试。"})
            score = driver_score_detail(self.store, self.store.segment(segment_id), utcnow())
        return self._send(200, {"ok": True, "driver_arrival": result, "driver_score": score})

    def _save_driver_review(self):
        match = re.fullmatch(r"/api/driver-score/([0-9]{1,18})", self.path)
        if not match:
            return self._send(400, {"error": "任务航段编号无效。"})
        body = self._driver_arrival_body({"route_ok", "return_status", "manual_deduction", "manual_reason", "version"})
        if body is None:
            return
        segment_id, now = int(match.group(1)), utcnow()
        with self.lock:
            try:
                self.store.driver_scores.save_review(segment_id, body, now)
            except driver_arrivals.ConflictError as exc:
                return self._send(409, {"error": str(exc), "driver_score": driver_score_detail(self.store, self.store.segment(segment_id), now)})
            except driver_arrivals.NotFoundError as exc:
                return self._send(404, {"error": str(exc)})
            except driver_arrivals.ValidationError as exc:
                return self._send(400, {"error": str(exc)})
            except sqlite3.OperationalError:
                return self._send(503, {"error": "暂时无法保存，请重新读取记录后重试。"})
            return self._send(200, {"ok": True, "driver_score": driver_score_detail(self.store, self.store.segment(segment_id), now)})

    def _save_driver_override(self):
        match = re.fullmatch(r"/api/driver-total-score/(tms:[a-f0-9]{64})", unquote(self.path))
        if not match:
            return self._send(400, {"error": "司机身份标识无效。"})
        body = self._driver_arrival_body({"manual_score", "reason", "version"})
        if body is None:
            return
        key = match.group(1)
        with self.lock:
            try:
                result = self.store.driver_scores.override(key, body, utcnow())
            except driver_arrivals.ConflictError as exc:
                return self._send(409, {"error": str(exc), "override": self.store.driver_scores.get_override(key)})
            except driver_arrivals.NotFoundError as exc:
                return self._send(404, {"error": str(exc)})
            except driver_arrivals.ValidationError as exc:
                return self._send(400, {"error": str(exc)})
            except sqlite3.OperationalError:
                return self._send(503, {"error": "暂时无法保存，请重新读取记录后重试。"})
            return self._send(200, {"ok": True, "override": result})

    def _post(self):
        if os.environ.get("BWI_DASHBOARD_READONLY") == "1":  # preview servers never change shift or alert state
            return self._send(403, {"error": "read-only preview: use the live dashboard for shift actions"})
        if self.path.startswith("/api/driver-arrival/"):
            return self._save_driver_arrival()
        if self.path.startswith("/api/driver-score/"):
            return self._save_driver_review()
        if self.path.startswith("/api/driver-total-score/"):
            return self._save_driver_override()
        if self.path == "/api/driver-sync":
            body = self._driver_arrival_body(set())
            if body is None:
                return
            if not self.driver_sync:
                return self._send(503, {"error": "请使用司机看板入口同步 TMS 司机。"})
            return self._send(202, {"ok": True, "sync": self.driver_sync.start()})
        if self.path in ("/api/shift/start", "/api/shift/end", "/api/shift/confirm-open", "/api/shift/opening"):
            body = self._body()
            if config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
                return self._send(403, {"error": "admin token required"})
            user = str(body.get("user") or "").strip()[:80]
            if not user:
                return self._send(400, {"error": "user name required"})
            with self.lock:
                shifts = shift.Shifts(self.store)
                now = utcnow()
                action = self.path.rsplit("/", 1)[1]
                if action == "opening":
                    hhmm = str(body.get("time") or "").strip()
                    try:
                        h, m = map(int, hhmm.split(":"))
                        assert 0 <= h < 24 and 0 <= m < 60
                    except (ValueError, AssertionError):
                        return self._send(400, {"error": "time must be HH:MM"})
                    shifts.set_opening(now, user, f"{h:02d}:{m:02d}")
                else:
                    ended = shifts.end(now, user) if action == "end" else None
                    if action != "end":
                        {"start": shifts.start, "confirm-open": shifts.confirm_open}[action](now, user)
                    if ended is not None:  # End Shift: write the shift report right away
                        from .report import build
                        folder = os.path.join(os.path.dirname(config.DB_PATH) or ".", "reports")
                        os.makedirs(folder, exist_ok=True)
                        with open(os.path.join(folder, f"shift_report_session{ended}.md"), "w", encoding="utf-8") as fh:
                            fh.write(build(self.store, ended, now) + "\n")
                return self._send(200, shifts.view(now))
        if self.path == "/api/tms-token":  # new TMS login pasted on the dashboard: checked with TMS, then saved
            body = self._body()
            if config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
                return self._send(403, {"error": "admin token required"})
            from . import tms
            auth = tms.normalize_token(str(body.get("token") or ""))
            if len(auth) < 30:
                return self._send(400, {"error": "token 太短，请复制完整的 Authorization 值（Bearer 开头）"})
            try:
                self.check_token(auth)
            except tms.AuthError:
                return self._send(400, {"error": "TMS 不接受这个 token（无效或已过期），未保存。请重新登录 TMS 后再复制。"})
            except tms.FeedError as e:
                return self._send(502, {"error": f"暂时连不上 TMS，未保存，请稍后再试（{e}）"})
            tms.save_token(auth)
            with self.lock:
                self.store.set_meta("poll_now", "1")
                self.store.shift_event("-", utcnow(), "TMS login updated on the dashboard", str(body.get("user") or "")[:80])
            return self._send(200, {"ok": True})
        if self.path == "/api/feishu/mode":  # dashboard switch: live sending / test mode (record only)
            body = self._body()
            if config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
                return self._send(403, {"error": "admin token required"})
            user, mode = str(body.get("user") or "").strip()[:80], body.get("mode")
            if not user or mode not in ("live", "test"):
                return self._send(400, {"error": "user and mode (live|test) required"})
            with self.lock:
                self.store.set_meta("feishu_mode", mode)
                self.store.shift_event("-", utcnow(), f"feishu mode set to {mode} on the dashboard", user)
            return self._send(200, _feishu_status(self.store))
        qty = re.fullmatch(r"/api/segments/([0-9]{1,18})/cargo", self.path)
        if qty:  # BWI cargo by hand: {"user", "pieces"} (票数; "bags" optional); pieces null clears it
            body = self._body()
            if config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
                return self._send(403, {"error": "admin token required"})
            user = str(body.get("user") or "").strip()[:80]
            try:
                bags = None if body.get("bags") in (None, "") else int(body["bags"])
                pieces = None if body.get("pieces") in (None, "") else int(body["pieces"])
                assert user and (bags is None or 0 <= bags <= 10000) and (pieces is None or 0 <= pieces <= 1000000)
            except (ValueError, TypeError, AssertionError):
                return self._send(400, {"error": "name and a whole number of pieces (票数) required"})
            with self.lock:
                if self.store.segment(int(qty.group(1))) is None:
                    return self._send(404, {"error": "task not found"})
                self.store.set_manual_cargo(int(qty.group(1)), bags, pieces, user, utcnow())
            return self._send(200, {"ok": True})
        hide = re.fullmatch(r"/api/segments/([0-9]{1,18})/(hide|unhide)", self.path)
        if hide:
            body = self._body()
            if config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
                return self._send(403, {"error": "admin token required"})
            user, reason = str(body.get("user") or "").strip()[:80], str(body.get("reason") or "").strip()[:300]
            seg_id = int(hide.group(1))
            if not user or (hide.group(2) == "hide" and not reason):
                return self._send(400, {"error": "name and reason required"})
            with self.lock:
                if hide.group(2) == "unhide":
                    self.store.unhide_segment(seg_id, utcnow(), user)
                else:
                    if self.store.segment(seg_id) is None:
                        return self._send(404, {"error": "task not found"})
                    self.store.hide_segment(seg_id, utcnow(), user, reason)
            return self._send(200, {"ok": True})
        parts = self.path.strip("/").split("/")  # api/alerts/<id>/ack | api/alerts/<id>/close
        if len(parts) == 4 and parts[:2] == ["api", "alerts"] and parts[3] == "close":
            body = self._body()
            if config.ADMIN_TOKEN and self.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
                return self._send(403, {"error": "admin token required"})
            user, reason = str(body.get("user") or "").strip()[:80], str(body.get("reason") or "").strip()[:300]
            if not user or not reason:
                return self._send(400, {"error": "name and reason required"})
            with self.lock:
                row = self.store.db.execute("SELECT alert_type, stage, status FROM alerts WHERE id=?",
                                            (int(parts[2]),)).fetchone()
                if row is None or row["status"] != "open":
                    return self._send(404, {"error": "alert not open"})
                if not config.closable(row["alert_type"], row["stage"]):
                    return self._send(403, {"error": "this alert must be kept until TMS shows the operation"})
                self.store.manual_close(int(parts[2]), utcnow(), user, reason)
            return self._send(200, {"ok": True})
        if len(parts) == 4 and parts[:2] == ["api", "alerts"] and parts[3] == "ack":
            with self.lock:
                self.store.acknowledge(int(parts[2]), utcnow())
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def _own_host_names():
    import socket
    names = set(config.ALLOWED_HOSTS)
    for n in (socket.gethostname(), socket.getfqdn()):
        n = (n or "").rstrip(".").lower()
        if n:
            names |= {n, n.split(".")[0], n.split(".")[0] + ".local"}
    return names


class Server(ThreadingHTTPServer):
    """One dashboard per port. On Windows SO_REUSEADDR lets a second process bind the same port and the two then
    answer requests at random (2026-10-21: page broke on refresh after an upgrade left the old copy running), so
    Windows binds exclusively and a duplicate copy fails to start instead."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _version():
    for path in (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION.txt"),):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return "dev"


def serve(db_path, host=config.WEB_HOST, port=config.WEB_PORT):
    Handler.store = Store(db_path)  # own connection, separate from the poller
    server = Server((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
