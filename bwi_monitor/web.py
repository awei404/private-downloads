"""Internal dashboard (SRS §8): stdlib HTTP server, JSON API + one HTML page."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, departures, rules, shift
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
    test_mode = svc.get("test_mode", config.FEISHU_TEST_MODE)
    return {
        "enabled": enabled, "test_mode": test_mode, "configured": configured,
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


def receiving_board(segs, by_task, unloading_rows, now):
    """Trucks into BWI for the receiving board: arrivals inside the current receiving window, every truck that has
    left its origin and is still on the way (any day; departed within IN_TRANSIT_MAX_DAYS), and not-yet-departed
    trucks predicted to arrive inside the window. Each row says which receiving day it belongs to."""
    day, start, end = receiving_window(now)
    unl = {r["segment_id"]: r for r in unloading_rows}
    rows = []
    for seg in segs:
        if seg.get("destination") != config.SITE_CODE or seg.get("_validity"):
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
        if arrived:
            if not in_window:
                continue
        elif dep is not None:
            if now - dep > timedelta(days=config.IN_TRANSIT_MAX_DAYS):
                continue  # left its origin long ago and never arrived: a stale TMS task
        elif not in_window and not (when < start and now - when < timedelta(days=1)):
            continue
        late = not arrived and when < now
        bucket = ("today" if in_window else "next" if end <= when < end + timedelta(days=1)
                  else "later" if when >= end else "overdue")
        legs = by_task.get(seg.get("taskNo"), [])
        chain = len(legs) >= 2 or len((seg.get("taskName") or "").split("(")[0].split("-")) > 2
        u = unl.get(seg["id"], {})
        rows.append({"segment_id": seg["id"], "taskNo": seg.get("taskNo"), "licensePlateNo": seg.get("licensePlateNo"),
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
        if not m or "unmatched" in m:
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
    "PENDING": ("pending", "等待开门"), "SKIP": ("unknown", "无法判定"),
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
        "unloading": unloading[:400],
        "receiving": receiving_board(list(segs.values()), by_task, unloading, now),
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
    return {
        "segment": {k: seg.get(k) for k in FIELDS},
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


class Handler(BaseHTTPRequestHandler):
    store = None
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        now = utcnow()
        with self.lock:
            if self.path in ("/", "/index.html"):
                page = os.environ.get("BWI_DASHBOARD_FILE", "dashboard.html")  # preview: dashboard_v2.html
                with open(os.path.join(os.path.dirname(__file__), os.path.basename(page)), "rb") as fh:
                    return self._send(200, fh.read(), "text/html")
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

    def do_POST(self):
        if os.environ.get("BWI_DASHBOARD_READONLY") == "1":  # preview servers never change shift or alert state
            return self._send(403, {"error": "read-only preview: use the live dashboard for shift actions"})
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


def serve(db_path, host=config.WEB_HOST, port=config.WEB_PORT):
    Handler.store = Store(db_path)  # own connection, separate from the poller
    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
