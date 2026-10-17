"""Feishu custom-bot delivery (phase 3, 2026-10-14).

Messages are built only from TMS fields and alert evidence; a value that is not available is left
out, never filled in. Delivery checks both the HTTP status and Feishu's own response code, retries
temporary failures with bounded backoff, and never puts the webhook URL or secret in a log or error.
"""
import base64
import hashlib
import hmac
import time

import requests

from . import config
from .rules import EASTERN, fmt_et

TEMPORARY_CODES = {9499, 11232, 11233}  # Feishu rate-limit / busy codes


def _hm(dt):
    return dt.astimezone(EASTERN).strftime("%m-%d %H:%M") if dt else None


def _hm_str(value):
    """'2026-10-14 08:51:10 (Eastern Time)' -> '10-14 08:51'."""
    return f"{value[5:16]}" if value else None


def _lines(*pairs):
    return "\n".join(f"{k}：{v}" for k, v in pairs if v not in (None, ""))


def _vehicle(seg):
    parts = [f"车牌 {seg['licensePlateNo']}" if seg.get("licensePlateNo") else None,
             f"挂车 {seg['trailerNo']}" if seg.get("trailerNo") else None]
    return " / ".join(p for p in parts if p) or None


def _route(seg, ev):
    route = f"{seg.get('placeOfOrigin')} → {seg.get('destination')}"
    if ev.get("routeType"):
        route += "（串点）" if ev["routeType"] == "multi-stop" else "（单站）"
    return route


def _arrival(ev):
    src = {"GPS": "GPS", "manual": "人工", "TMS actualArrivalTime": "TMS"}.get(ev.get("arrivalSource"))
    t = _hm_str(ev.get("gpsArrival")) if ev.get("arrivalSource") == "GPS" else _hm_str(ev.get("manualArrival"))
    return f"{t}（{src}）" if t and src else None


def _unload_status(ev):
    unloaded, loaded = ev.get("unLoadBagCount"), ev.get("loadBagCount")
    if ev.get("routeType") == "multi-stop":
        return "TMS 尚未记录卸车扫描" if not unloaded else f"TMS 已卸 {unloaded} 袋"
    if unloaded is None:
        return "TMS 尚未确认完成卸车（无卸车数量）"
    return f"TMS 尚未确认完成卸车（已卸 {unloaded} / 装车 {loaded if loaded is not None else '未知'} 袋）"


NA = "未提供"


def arrival_message(seg, f, now):
    ev = f.evidence or {}
    source = {"GPS": "GPS", "manual": "TMS人工签到", "TMS actualArrivalTime": "TMS到车时间"}.get(ev.get("arrivalSource"), NA)
    checkin = _hm_str(ev.get("manualArrival")) if ev.get("checkInCompleted") else None
    body = _lines(("任务编号", seg.get("taskNo") or NA), ("车辆编号", _vehicle(seg) or NA),
                  ("线路", _route(seg, {})), ("实际到车时间", _hm(f.deadline) or NA), ("时间来源", source),
                  ("GPS到车", _hm_str(ev.get("gpsArrival")) or NA),
                  ("TMS人工签到", f"已完成（{checkin}）" if checkin else "尚未完成"),
                  ("提醒时间", f"{_hm(now)} ET"))
    return "ARRIVED", f"🚚 BWI — 车辆到达提醒\n{body}\n请及时安排车辆签到和卸车。"


def _hhmm(dt):
    return dt.astimezone(EASTERN).strftime("%H:%M") if dt else NA


def departure_message(seg, f, kind):
    """🟡 reminder / 🔴 deadline alert / ✅ completed for a branch-line departure (2026-10-15 templates)."""
    # Always full dates (MM-DD HH:MM) so a 23:43 departure is never read against a next-day 02:00 deadline.
    from .departures import margin_text
    ev = f.evidence or {}
    route, deadline = ev.get("route") or NA, ev.get("scheduledDeparture")
    deadline_txt = deadline[5:16] if deadline else NA
    if ev.get("deadlineKind") == "tms" and deadline:
        deadline_txt += "（按 TMS 计划发车时间）"
    tms = ev.get("tmsPlannedDeparture")
    tms_line = (f"{tms[5:16]}（与内部截止相差 {ev.get('scheduleDiscrepancyMinutes'):+d} 分钟）"
                if tms and ev.get("scheduleDiscrepancyMinutes") else (tms[5:16] if tms else None))
    if kind == "DEPARTURE_DONE":
        manual = ev.get("manualDepartureEt")
        status = ("准时" if ev.get("marginSeconds", 0) >= 0 else "晚点") + f"（{margin_text(ev.get('marginSeconds', 0), 'zh')}）"
        tms_ref = None
        if ev.get("tmsPunctual") is not None and tms:
            tms_ref = f"{tms[5:16]}（{'TMS准点' if ev['tmsPunctual'] else 'TMS晚点'}，仅供参考）"
        body = _lines(("线路", route), ("任务编号", seg.get("taskNo") or NA), ("内部截止时间", deadline_txt),
                      ("实际人工发车", manual[5:] if manual else NA), ("TMS计划发车", tms_ref or tms_line),
                      ("发车状态", status))
        return "DONE", f"✅ BWI 发车完成\n{body}"
    if f.stage == "WARN":
        body = _lines(("线路", route), ("任务编号", seg.get("taskNo") or NA), ("计划发车截止", deadline_txt),
                      ("TMS计划发车", tms_line), ("当前状态", "尚未人工发车"), ("剩余时间", f"{f.warn_level}分钟"))
        return f"WARN{f.warn_level}", f"🟡 BWI 发车提醒\n{body}\n请检查车辆装车及TMS发车操作。"
    if f.stage == "OVERDUE":
        late_arrival = None
        if ev.get("bwiArrival"):
            late_arrival = ev["bwiArrival"][5:16] + ("（晚于发车截止，车辆已晚点）" if ev.get("arrivedAfterDeadline") else "")
        body = _lines(("线路", route), ("任务编号", seg.get("taskNo") or NA), ("截止时间", deadline_txt),
                      ("TMS计划发车", tms_line), ("车辆到达BWI", late_arrival), ("人工发车", "尚未记录"))
        return "OVERDUE", f"🔴 BWI 发车截止告警\n{body}\n已到达规定发车时间，请立即检查。"
    return None


def alert_message(seg, f, kind, now, data_as_of):
    """(severity, text) for an alert Feishu should carry, else None. Severity is part of the dedupe key.
    Active types (2026-10-15): branch departure reminder / deadline / completed (once departure matching is
    verified, FEISHU_DEPARTURE_ENABLED), unloading 30-min and 55-min reminders, missing manual check-in.
    Arrival notices and the 60-min unloading message are off; their records stay on the dashboard."""
    ev = f.evidence or {}
    if kind == "ARRIVAL" and f.alert_type == "ARRIVAL":
        return arrival_message(seg, f, now) if config.FEISHU_ARRIVAL_NOTICE else None
    if f.alert_type in ("BRANCH_DEPARTURE", "DEPARTURE_DONE"):
        if not config.FEISHU_DEPARTURE_ENABLED:
            return None
        if kind == "DEPARTURE_DONE" or (kind in ("NEW", "ESCALATED") and f.stage in ("WARN", "OVERDUE")):
            return departure_message(seg, f, kind)
        return None
    if kind not in ("NEW", "ESCALATED"):
        return None  # resolutions, late/uncertain results and updates stay on the dashboard
    common_tail = [("预警时间", f"{_hm(now)} ET"), ("数据时间", f"{_hm(data_as_of)} ET" if data_as_of else None)]
    if f.alert_type == "UNLOAD" and f.stage == "OVERDUE" and not config.FEISHU_UNLOAD_OVERDUE:
        return None
    if f.alert_type == "UNLOAD_60" and f.stage == "WARN":  # 2026-10-17: reminder 60 min after arrival, any hour
        progress = ev.get("progress60") or ""
        progress = progress.replace(" bags", " 袋").replace("first scan recorded", "已首枪扫描").replace(
            "first scan not recorded", "尚无卸车扫描")
        unloaded, loaded = ev.get("unLoadBagCount"), ev.get("loadBagCount")
        if ev.get("routeType") != "multi-stop" and loaded and unloaded is not None and unloaded < loaded:
            progress = f"数量未对上：已卸 {unloaded} / 装车 {loaded} 袋"
        body = _lines(("任务编号", seg.get("taskNo") or NA), ("车辆", _vehicle(seg) or NA), ("线路", _route(seg, ev)),
                      ("到车时间", _arrival(ev)), ("卸车进度", progress or NA),
                      ("首枪截止", _hm_str(ev.get("requirement", "").replace("first unloading scan by ", "")) or NA),
                      *common_tail)
        return "AT60", (f"🟡 BWI 卸车提醒（到车已60分钟）\n{body}\n车辆到达已满 60 分钟仍未卸完或数量未对上，请核实；"
                        "如已核实无误，可在监控台“手动确认”。")
    if f.alert_type == "UNLOAD" and f.stage in ("WARN", "OVERDUE"):
        if f.stage == "OVERDUE":
            severity, head, timing = "OVERDUE", "🔴 BWI TMS — 卸车超时", f"已达到 {config.UNLOAD_LIMIT_MINUTES} 分钟（超时 {f.minutes} 分钟）"
        elif f.priority == "high":
            severity, head, timing = f"WARN{f.warn_level}", "🟠 BWI TMS — 卸车紧急提醒", f"仅剩 {f.warn_level} 分钟"
        else:
            severity, head, timing = f"WARN{f.warn_level}", "🟡 BWI TMS — 卸车提醒", f"剩余 {f.warn_level} 分钟"
        need = "首次卸车扫描" if ev.get("routeType") == "multi-stop" else "整车卸完"
        body = _lines(("任务编号", seg.get("taskNo")), ("车辆", _vehicle(seg)), ("线路", _route(seg, ev)),
                      ("预警类别", f"卸车时效（{need}，{config.UNLOAD_LIMIT_MINUTES} 分钟）"), ("到车时间", _arrival(ev)),
                      ("监控开始", _hm_str(ev.get("countdownStart"))),
                      ("卸车截止", f"{_hm(f.deadline)}" if f.deadline else None), ("当前状态", _unload_status(ev)),
                      ("时效状态", timing), *common_tail)
        return severity, f"{head}\n{body}\n请检查车辆卸车情况。"
    if f.alert_type.startswith("OP:") and f.stage in ("RISK", "MISSING") and (
            f.alert_type in config.FEISHU_OP_TYPES):
        operation = ev.get("operation")
        title = {"ARRIVAL": "TMS人工签到异常", "DEPARTURE": "TMS人工发车异常"}.get(operation, "TMS操作异常")
        op = {"LOADING": "装车", "DEPARTURE": "发车", "ARRIVAL": "到车签到", "UNLOADING": "卸车"}.get(operation, "")
        level = "缺失（TMS 记录）" if f.stage == "MISSING" else "尚未完成（待确认）"
        extra = []
        if operation == "ARRIVAL":
            extra = [("GPS到车", _hm_str(ev.get("gpsArrival")) or NA), ("TMS人工签到", "未记录")]
        elif operation == "DEPARTURE":
            extra = [("GPS离场", _hm_str(ev.get("gpsDeparture")) or NA), ("人工发车", "未记录")]
        body = _lines(("任务编号", seg.get("taskNo") or NA), ("车辆", _vehicle(seg) or NA), ("线路", _route(seg, {})),
                      ("异常类型", f"{op}操作{level}"), *extra, ("当前状态", ev.get("statusZh")), *common_tail)
        return f.stage, f"⚠️ BWI TMS — {title}\n{body}\n请核实并在 TMS 中补齐操作记录。"
    if f.alert_type == "DEPARTURE" and config.FEISHU_INCLUDE_DEPARTURE and f.stage == "OVERDUE":
        body = _lines(("任务编号", seg.get("taskNo") or NA), ("车辆", _vehicle(seg) or NA), ("线路", _route(seg, {})),
                      ("计划发车", _hm(f.deadline)), ("当前状态", "TMS 尚无 GPS 离场或人工发车记录"),
                      ("超时状态", f"已超过计划发车 {f.minutes} 分钟"), *common_tail)
        return "OVERDUE", f"⚠️ BWI TMS — 发车超时\n{body}\n请确认车辆是否已发车并在 TMS 中完成人工发车。"
    return None


def reminder_message(now):
    return ("🔔 BWI Shift Reminder\nA new working day is approaching. Please confirm that warehouse operations "
            f"have started.\n新的工作日即将开始，请确认仓库是否已开始作业，并在监控台点击 [START SHIFT / RESUME ALERTS]。\n"
            f"提醒时间：{_hm(now)} ET")


def digest_message(items, now):
    lines = [f"📋 BWI TMS — {len(items)} 条新预警（合并发送，详情见监控台）"]
    for _, text in items:
        lines.append("• " + text.split("\n")[0].replace("BWI TMS — ", "") + " "
                     + next((l.split("：", 1)[1] for l in text.split("\n") if l.startswith("任务编号")), ""))
    lines.append(f"预警时间：{_hm(now)} ET")
    return "\n".join(lines)


class FeishuClient:
    def __init__(self, webhook=None, secret=None, session=None, retries=None, sleep=time.sleep):
        """webhook/secret: callables returning the current value (read at send time) or plain strings."""
        self._webhook = webhook
        self._secret = secret
        self.session = session or requests.Session()
        self.retries = config.FEISHU_RETRIES if retries is None else retries
        self.sleep = sleep
        self.last_result = None  # "HTTP <status>, code <code>, msg <msg>" of the last attempt (no URL)

    @staticmethod
    def _value(v):
        return v() if callable(v) else v

    def configured(self):
        return bool(self._value(self._webhook))

    def _sanitize(self, text, webhook, secret):
        text = str(text)
        for s in (webhook, secret, webhook and webhook.rstrip("/").rsplit("/", 1)[-1]):
            if s:
                text = text.replace(s, "<redacted>")
        return text[:300]

    def send(self, text):
        """(ok, error). error never contains the webhook or secret."""
        webhook, secret = self._value(self._webhook), self._value(self._secret)
        if not webhook:
            return False, "Feishu webhook not configured"
        payload = {"msg_type": "text", "content": {"text": text}}
        error = None
        for attempt in range(max(1, self.retries)):
            if secret:
                ts = str(int(time.time()))
                sign = hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
                payload.update(timestamp=ts, sign=base64.b64encode(sign).decode())
            temporary = False
            try:
                r = self.session.post(webhook, json=payload, timeout=10)
                self.last_result = f"HTTP {r.status_code}"
                if r.status_code == 429 or r.status_code >= 500:
                    error, temporary = f"HTTP {r.status_code}", True
                elif r.status_code != 200:
                    return False, f"HTTP {r.status_code}"
                else:
                    body = r.json()
                    code = body.get("code", body.get("StatusCode"))
                    self.last_result = f"HTTP 200, code {code}, msg {body.get('msg') or body.get('StatusMessage')}"
                    if code == 0:
                        return True, None
                    error = f"Feishu code {code}: {body.get('msg') or body.get('StatusMessage')}"
                    temporary = code in TEMPORARY_CODES
            except (requests.RequestException, ValueError) as e:
                error, temporary = f"{type(e).__name__}: {e}", True
            if not temporary:
                break
            if attempt + 1 < self.retries:
                self.sleep(2 ** attempt)
        return False, self._sanitize(error, webhook, secret)
