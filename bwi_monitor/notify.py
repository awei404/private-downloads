"""Notification delivery: console, logs/notifications.jsonl, the notifications table and Feishu.

Operational alerts (time, compliance, data checks) follow the shift switch: while no shift is open
they are recorded with channel "muted" and not shown or delivered, and they are never replayed
later, so resuming a shift does not flood the group. Feishu gets only the alert types it should
carry (feishu.alert_message), once per segment + alert type + severity (notification_keys), and a
burst of more than FEISHU_MAX_PER_TICK messages in one cycle goes out as a single digest.
Monitor-health alerts go to the optional admin channel; the morning Start Shift reminder is sent
even while the shift is closed. FEISHU_ENABLED=false: nothing goes to Feishu. FEISHU_TEST_MODE=true:
messages are recorded as "feishu-test-mode" but not delivered."""
import json
import os

from . import config, feishu
from .rules import EASTERN, fmt_et

TITLES = {
    "DEPARTURE": "Departure", "UNLOAD": "Unload 60 min", "SYSTEM:FEED": "Monitor feed",
    "OP:LOADING": "Operation: loading", "OP:DEPARTURE": "Operation: departure", "OP:ARRIVAL": "Operation: arrival",
    "OP:UNLOADING": "Operation: unloading", "ARRIVAL": "Vehicle arrived",
    "BRANCH_DEPARTURE": "Departure", "DEPARTURE_DONE": "Departure completed", "UNLOAD_60": "Unload 60-min check",
}
KIND_LABEL = {
    "NEW": "", "ESCALATED": "", "UPDATED": "updated: ", "REMINDER": "reminder: ",
    "RESOLVED": "resolved: ", "LATE": "", "UNCERTAIN": "", "ARRIVAL": "", "DEPARTURE_DONE": "",
}


def describe(f):
    if f.stage == "WARN":
        left = f.warn_level if f.warn_level is not None else f.minutes
        if f.priority == "high":
            return f"HIGH PRIORITY — only {left} min left, not done"
        return f"{left} min left, not done"
    observed = f.alert_type == "UNLOAD"  # judged from 10-minute poll observations
    if f.stage == "OVERDUE":
        return f"OVERDUE by {f.minutes} min" + (" based on TMS records" if observed else "")
    if f.stage == "LATE":
        if observed:
            return f"overdue based on TMS records (still incomplete {f.minutes}+ min after deadline)"
        return f"completed LATE by {f.minutes} min"
    if f.stage == "UNCERTAIN":
        return "timing uncertain (completed between two polls around the deadline)"
    if f.stage == "OK":
        return "completed on time" if f.alert_type in ("DEPARTURE", "UNLOAD") else "cleared"
    if f.stage == "WAITING":
        return "deadline moved, no longer urgent"
    if f.stage == "MISSING":
        return "missing in TMS records"
    if f.stage == "RISK":
        return "not recorded yet (pending risk)"
    if f.stage == "EVENT":
        return f.reason
    return "needs verification"


def format_message(seg, f, kind, now, data_as_of):
    title = TITLES.get(f.alert_type) or ("Data check: " + f.alert_type.split(":", 1)[-1])
    if f.alert_type == "UNLOAD":
        multi = (f.evidence or {}).get("routeType") == "multi-stop"
        title += " (multi-stop: first scan)" if multi else " (single-stop: complete unloading)"
    lines = [f"[BWI] {KIND_LABEL.get(kind, '')}{title} — {describe(f)}"]
    if seg.get("id"):
        lines.append(f"Task {seg.get('taskNo')} | {seg.get('placeOfOrigin')} → {seg.get('destination')}"
                     f" | plate {seg.get('licensePlateNo') or '-'} | trailer {seg.get('trailerNo') or '-'}")
    if f.deadline:
        lines.append(f"Deadline {fmt_et(f.deadline)} ET | now {now.astimezone(EASTERN):%H:%M} ET")
    if f.reason:
        lines.append(f"Reason: {f.reason}")
    if f.action and kind not in ("RESOLVED", "LATE", "UNCERTAIN"):
        lines.append(f"Action: {f.action}")
    lines.append(f"TMS data as of {fmt_et(data_as_of)} ET")
    return "\n".join(lines)


class Notifier:
    def __init__(self, store, log_dir=config.LOG_DIR, echo=True, enabled=None, feishu_client=None,
                 admin_client=None, feishu_enabled=None, test_mode=None):
        """enabled() -> bool: whether a shift is open (operational alerts on). None = always on."""
        self.store = store
        self.log_dir = log_dir
        self.echo = echo
        self.enabled = enabled
        self.feishu = feishu_client
        self.admin = admin_client
        self.feishu_enabled = config.FEISHU_ENABLED if feishu_enabled is None else feishu_enabled
        self.test_mode = config.FEISHU_TEST_MODE if test_mode is None else test_mode
        self.queue = []  # (alert_id, text) waiting for flush()
        self.data_as_of = None  # set by the monitor after each successful poll

    @classmethod
    def from_config(cls, store, **kw):
        client = feishu.FeishuClient(lambda: config.secret("FEISHU_WEBHOOK_URL", config.FEISHU_WEBHOOK_FILE),
                                     lambda: config.secret("FEISHU_SECRET", config.FEISHU_SECRET_FILE))
        admin_url = lambda: config.secret("FEISHU_ADMIN_WEBHOOK_URL", config.FEISHU_ADMIN_WEBHOOK_FILE)
        admin = feishu.FeishuClient(admin_url, None) if admin_url() else None
        return cls(store, feishu_client=client, admin_client=admin, **kw)

    def send(self, alert_id, seg, f, kind, now):
        text = format_message(seg, f, kind, now, self.data_as_of)
        system = f.alert_type.startswith("SYSTEM")
        if not system and self.enabled is not None and not self.enabled():
            self._log(alert_id, now, kind, text, muted=True)
            return
        self._deliver(alert_id, now, kind, text)
        self.store.mark_notified(alert_id, now)
        if system:
            if kind == "NEW" and self.admin is not None and self.feishu_enabled:
                self._feishu_now(self.admin, alert_id, now, "feishu-admin", f"⛔ BWI TMS 监控系统异常\n{f.reason}\n"
                                 f"时间：{now.astimezone(EASTERN):%m-%d %H:%M} ET")
            elif self.admin is None and self.feishu and self.feishu_enabled and (self.enabled is None or self.enabled()):
                # 2026-10-18: without an admin channel the shift group hears that monitoring stopped (TMS login
                # expired, no data) and, only if it heard that, that it recovered.
                at = f"时间：{now.astimezone(EASTERN):%m-%d %H:%M} ET"
                if kind == "NEW":
                    why = ("TMS 登录已失效，请更新监控电脑上的 TMS 登录凭证（secrets/gofo_auth）" if "login" in f.reason
                           else f"超过 {config.STALE_AFTER_MINUTES} 分钟没有拿到 TMS 数据，请检查监控电脑是否开机、联网")
                    self._feishu_now(self.feishu, alert_id, now, "feishu",
                                     f"⛔ BWI 监控数据中断\n{why}。\n在恢复之前，监控台和飞书提醒都不是最新的。\n{at}")
                elif kind == "RESOLVED" and any(n["channel"] == "feishu" and n["ok"]
                                                for n in self.store.notifications_for([alert_id])):
                    self._feishu_now(self.feishu, alert_id, now, "feishu", f"✅ BWI 监控数据已恢复，提醒继续。\n{at}")
            return
        msg = feishu.alert_message(seg, f, kind, now, self.data_as_of) if self.feishu else None
        if msg and self.feishu_enabled:
            severity, ftext = msg
            key = f"{seg.get('taskNo')}|{seg.get('id')}|{f.alert_type}|{severity}"  # task, segment, category, severity
            if self.store.claim_notification_key(key, now):
                self.queue.append((alert_id, ftext))
            else:
                self.store.set_meta("duplicates_prevented", str(int(self.store.get_meta("duplicates_prevented") or 0) + 1))

    def send_shift_reminder(self, now):
        text = feishu.reminder_message(now)
        self._deliver(None, now, "SHIFT", text)
        day = now.astimezone(EASTERN).date()
        if self.feishu and self.feishu_enabled and self.store.claim_notification_key(f"reminder|{day}", now):
            self._feishu_now(self.feishu, None, now, "feishu", text)

    def flush(self, now):
        """Deliver this cycle's Feishu messages; a burst becomes one digest."""
        items, self.queue = self.queue, []
        if not items:
            return
        if len(items) > config.FEISHU_MAX_PER_TICK:
            text = feishu.digest_message(items, now)
            ok, err = self._feishu_send(self.feishu, text)
            for alert_id, item_text in items:
                self.store.log_notification(alert_id, now, self._channel("feishu-digest"), ok, err, item_text)
            return
        for alert_id, text in items:
            self._feishu_now(self.feishu, alert_id, now, "feishu", text)

    def _live(self):
        from . import shift
        return not shift.feishu_test_mode(self.store, self.test_mode) and shift.feishu_live(self.store)

    def _channel(self, name):
        return name if self._live() else name + "-test-mode"

    def _feishu_send(self, client, text):
        if not self._live():
            return True, "not sent (test mode or no live authorization for this shift)"
        return client.send(text)

    def _feishu_now(self, client, alert_id, now, channel, text):
        ok, err = self._feishu_send(client, text)
        self.store.log_notification(alert_id, now, self._channel(channel), ok, err, text)
        return ok

    def _deliver(self, alert_id, now, kind, text):
        if self.echo:
            print(text + "\n", flush=True)
        self._log(alert_id, now, kind, text)

    def _log(self, alert_id, now, kind, text, muted=False):
        channel = "muted" if muted else "log"
        if not self.log_dir:
            if muted:
                self.store.log_notification(alert_id, now, channel, True, None, text)
            return
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(os.path.join(self.log_dir, "notifications.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": now.isoformat(), "alert_id": alert_id, "kind": kind, "muted": muted,
                                     "text": text}, ensure_ascii=False) + "\n")
            self.store.log_notification(alert_id, now, channel, True, None, text)
        except OSError as e:
            self.store.log_notification(alert_id, now, channel, False, str(e), text)
