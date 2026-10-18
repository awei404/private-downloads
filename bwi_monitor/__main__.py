"""BWI TMS real-time alert service.

    python3 -m bwi_monitor                 # run: poll every 10 min, tick every 60 s, dashboard on :8765
    python3 -m bwi_monitor --once          # one poll + evaluation, then exit
    python3 -m bwi_monitor --replay FILE --at "2026-10-11 12:00"
                                           # offline replay of saved API records (in-memory DB)

Credentials: GOFO_AUTH / GOFO_TAG environment variables. Set GOFO_AUTH_FILE to a
file holding the Authorization value to refresh the token without a restart.
"""
import argparse
import json
import os
import sys
import time

from . import config
from .engine import Engine
from .notify import Notifier
from .shift import Shifts
from .rules import EASTERN, fmt_et
from .store import Store, from_iso, utcnow
from .tms import CANCELLED, AuthError, FeedError, TmsClient


def read_auth():
    path = os.environ.get("GOFO_AUTH_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    return os.environ.get("GOFO_AUTH")


class Monitor:
    def __init__(self, store):
        self.store = store
        self.notifier = Notifier.from_config(store)
        self.shifts = Shifts(store, self.notifier)
        self.notifier.enabled = self.shifts.alerts_enabled
        self.engine = Engine(store, self.notifier, confirm=self.confirm, shifts=self.shifts)
        self.client = None
        last_ok = store.last_poll(ok_only=True)
        self.notifier.data_as_of = from_iso(last_ok["finished_at"]) if last_ok else None

    def _client(self):
        auth = read_auth()
        if self.client is None or self.client.session.headers.get("Authorization") != auth:
            self.client = TmsClient(auth=auth)
        return self.client

    def poll(self):
        """Fetch BWI segments. Returns True on success."""
        now = utcnow()
        first_ever = self.store.last_poll(ok_only=True) is None
        run = self.store.start_poll(now)
        try:
            records = self._client().fetch_bwi()
        except AuthError as e:
            self.store.finish_poll(run, False, error=str(e))
            self.engine.feed_status(now, f"TMS login rejected ({e}). Update GOFO_AUTH.")
            print(f"[{now.astimezone(EASTERN):%H:%M}] poll failed: {e}", file=sys.stderr, flush=True)
            return False
        except FeedError as e:
            self.store.finish_poll(run, False, error=str(e))
            print(f"[{now.astimezone(EASTERN):%H:%M}] poll failed: {e}", file=sys.stderr, flush=True)
            return False
        changed = self.store.upsert_segments(records, now)
        try:
            voided, unverified, _ = self._client().reconcile_removed(self.store, now, utcnow())
            if voided or unverified:
                print(f"[{now.astimezone(EASTERN):%H:%M}] not in TMS arrival list — voided (taskStatus 6): {voided}; "
                      f"validity unverified: {unverified}", flush=True)
        except (AuthError, FeedError) as e:
            print(f"void check skipped: {e}", file=sys.stderr, flush=True)
        self.store.finish_poll(run, True, len(records), changed)
        self.notifier.data_as_of = now
        self.engine.feed_status(now, None)
        if first_ever:
            # Baseline: record current alert state without flooding notifications.
            self.engine.run(now, notify=False)
            print(f"Baseline stored: {len(records)} segments; alerts recorded silently.", flush=True)
        print(f"[{now.astimezone(EASTERN):%H:%M}] poll ok: {len(records)} BWI segments, {changed} changed",
              flush=True)
        return True

    def confirm(self, seg):
        """Re-fetch one task right before an important notice. A segment TMS no longer returns, or whose task is
        Cancelled (taskStatus 6), comes back with _validity set, so no alert is sent for it."""
        from . import departures
        try:
            client = self._client()
            fresh = [r for r in client.fetch_task(seg["taskNo"]) if r["id"] == seg["id"]]
            now = utcnow()
            if not fresh:
                client.verify(self.store, seg["id"], seg["taskNo"], now)
                return self.store.segment(seg["id"])
            self.store.upsert_segments(fresh, now)
            if departures.match(fresh[0]):  # departure alerts: also confirm the task is not cancelled
                status = client.task_status(seg["taskNo"])
                if status and status[0] == CANCELLED:
                    self.store.mark_validity(seg["id"], now, "VOIDED", f"TMS taskStatus {status[0]} {status[1]}")
        except (AuthError, FeedError):
            return None
        return self.store.segment(seg["id"])  # with updated poll observations and validity

    def check_staleness(self, now):
        last_ok = self.store.last_poll(ok_only=True)
        ok_at = from_iso(last_ok["finished_at"]) if last_ok else None
        if ok_at is None or (now - ok_at).total_seconds() > config.STALE_AFTER_MINUTES * 60:
            last = self.store.last_poll()
            why = (last["error"] if last and not last["ok"] else None) or "no successful poll"
            self.engine.feed_status(now, f"No TMS data since {fmt_et(ok_at)} ET ({why})")

    def run_forever(self):
        keep_awake()
        next_poll = 0.0
        while True:
            # Wall clock, not monotonic: on macOS the monotonic clock stops while the computer sleeps, so after a wake
            # the next poll would wait out the rest of the interval (2026-10-17: 25 min with stale data).
            if time.time() >= next_poll:
                self.poll()
                next_poll = time.time() + config.POLL_SECONDS
            now = utcnow()
            self.check_staleness(now)
            self.engine.run(now)
            time.sleep(config.TICK_SECONDS)


FEISHU_TEST_TEXT = """✅ BWI TMS 实时预警系统
飞书机器人连接测试成功！
当前系统处于测试阶段。
已支持：
🚚 车辆到达提醒
🟡 30分钟卸车提醒
🟠 55分钟紧急提醒
🔴 60分钟超时告警
⚠️ TMS人工签到异常
⚠️ TMS人工发车异常
正式运营通知尚未开启。"""


def _feishu_client():
    from .feishu import FeishuClient
    return FeishuClient(lambda: config.secret("FEISHU_WEBHOOK_URL", config.FEISHU_WEBHOOK_FILE),
                        lambda: config.secret("FEISHU_SECRET", config.FEISHU_SECRET_FILE))


def feishu_test():
    """One controlled connection-test message (sent even in test mode). Never prints the webhook."""
    client = _feishu_client()
    if not client.configured():
        print(f"Feishu webhook not configured (set FEISHU_WEBHOOK_URL or write {config.FEISHU_WEBHOOK_FILE}).")
        return 1
    store, now = Store(config.DB_PATH), utcnow()
    if not store.claim_notification_key("feishu-connection-test", now):
        print("Connection test already sent from this database; not sending again.")
        return 1
    ok, err = client.send(FEISHU_TEST_TEXT)
    store.log_notification(None, now, "feishu-test", ok, err, FEISHU_TEST_TEXT)
    print(f"Feishu: {client.last_result}")
    print("Feishu accepted the test message (HTTP 200, code 0)." if ok else f"Feishu test failed: {err}")
    return 0 if ok else 1


# Candidate conditions for the one-time end-to-end alert test, in order of preference.
E2E_CONDITIONS = [
    ("OP:DEPARTURE", "MISSING", lambda f: bool(f.evidence.get("gpsDeparture")), "GPS已离场，但缺少人工发车记录"),
    ("OP:DEPARTURE", "MISSING", lambda f: True, "下一站已到车，但缺少BWI人工发车记录"),
    ("OP:ARRIVAL", "RISK", lambda f: True, "GPS已到达，但TMS人工签到尚未完成"),
    ("UNLOAD", "OVERDUE", lambda f: True, "卸车已超过60分钟"),
    ("UNLOAD", "WARN", lambda f: True, "卸车临近截止时间"),
    ("ARRIVAL", "EVENT", lambda f: True, "新到达车辆"),
]


def feishu_alert_test():
    """Authorized once: fetch BWI tasks from TMS, find a genuine alert condition, re-check it, send one
    clearly labelled test alert, record it. Falls back to one labelled simulated alert if none is found."""
    from . import shift
    from .feishu import _hm, _hm_str
    from .rules import evaluate
    client, store, now = _feishu_client(), Store(config.DB_PATH), utcnow()
    if not client.configured():
        print("Feishu webhook not configured.")
        return 1
    tms = TmsClient(auth=read_auth())
    records = tms.fetch_bwi()
    store.upsert_segments(records, now)
    segs = store.segments()
    by_task = {}
    for s in segs:
        by_task.setdefault(s.get("taskNo"), []).append(s)
    ctx = dict(shift.context(store, segs), by_task=by_task)
    found = {i: [] for i in range(len(E2E_CONDITIONS))}
    examined = 0
    for s in segs:
        findings = evaluate(s, now, ctx)
        examined += bool(findings)
        for f in findings:
            for i, (atype, stage, test, _) in enumerate(E2E_CONDITIONS):
                if f.alert_type == atype and f.stage == stage and test(f):
                    found[i].append((s, f))
                    break
    anomalies = sum(len(v) for k, v in found.items() if E2E_CONDITIONS[k][0] != "ARRIVAL")
    print(f"TMS returned {len(records)} BWI segments; {examined} evaluated in the monitoring window; "
          f"{anomalies} potential anomalies: " + ", ".join(f"{E2E_CONDITIONS[i][3]}={len(v)}" for i, v in found.items() if v))
    chosen = None
    for i, items in found.items():
        for seg, f in sorted(items, key=lambda x: str(x[0].get("taskNo")), reverse=True)[:5]:
            fresh = [r for r in tms.fetch_task(seg["taskNo"]) if r["id"] == seg["id"]]
            if not fresh:
                continue
            store.upsert_segments(fresh, utcnow())
            fresh_seg = store.segment(seg["id"])
            g = next((g for g in evaluate(fresh_seg, utcnow(), ctx) if g.alert_type == f.alert_type
                      and g.stage == f.stage and E2E_CONDITIONS[i][2](g)), None)
            if g:
                chosen = (i, fresh_seg, g)
                break
            print(f"  {seg['taskNo']}: condition no longer present after TMS re-check; skipped")
        if chosen:
            break
    detected = _hm(utcnow())
    if chosen:
        i, seg, f = chosen
        ev = f.evidence
        lines = ["🧪 BWI TMS — 实时预警测试", "⚠️ 检测到TMS操作异常" if f.alert_type.startswith("OP:") else "⏱️ 检测到时效预警",
                 f"任务编号：{seg.get('taskNo')}", f"车辆编号：{seg.get('licensePlateNo') or '未提供'}",
                 f"线路：{seg.get('placeOfOrigin')} → {seg.get('destination')}", f"异常类型：{E2E_CONDITIONS[i][3]}"]
        if f.alert_type == "OP:DEPARTURE":
            lines += [f"计划发车：{(seg.get('plannedDepartureTime') or '未提供')[5:16]}",
                      f"GPS离场时间：{_hm_str(ev.get('gpsDeparture')) or '未提供'}", "人工发车：未记录"]
            if seg.get("actualArrivalTime"):
                lines.append(f"下一站到车：{seg['actualArrivalTime'][5:16]}")
        elif f.alert_type == "OP:ARRIVAL":
            lines += [f"GPS到车时间：{_hm_str(ev.get('gpsArrival')) or '未提供'}", "人工签到：未记录"]
        else:
            lines += [f"到车时间：{_hm_str(ev.get('gpsArrival') or ev.get('manualArrival')) or '未提供'}",
                      f"截止时间：{_hm(f.deadline) or '未提供'}", f"当前状态：{f.reason}"]
        lines += [f"检测时间：{detected} ET（已重新抓取TMS确认）", "当前为测试通知，请勿作为正式运营指令。"]
        key, label = f"e2e-test|{seg.get('taskNo')}|{seg['id']}|{f.alert_type}|{f.stage}", "real TMS condition"
    else:
        seg, f = {"id": 0, "taskNo": "TEST-SIMULATED"}, None
        lines = ["🧪 BWI TMS — 模拟预警测试（虚构数据）", "未发现真实异常，以下为模拟数据，并非真实运营事件。",
                 "任务编号：TEST-0000（虚构）", "异常类型：GPS已离场，但缺少人工发车记录（模拟）",
                 f"检测时间：{detected} ET", "当前为测试通知，请勿作为正式运营指令。"]
        key, label = "e2e-test|simulated", "simulated (no genuine condition found)"
    if not store.claim_notification_key("e2e-alert-test", now):
        print("End-to-end alert test already sent from this database; not sending again.")
        return 1
    store.claim_notification_key(key, now)
    alert = store.latest_alert(seg["id"], f.alert_type) if f else None
    alert_id = alert["id"] if alert else (store.create_alert(seg["id"], f, now, status="closed", outcome="e2e test")
                                          if f else None)
    text = "\n".join(lines)
    ok, err = client.send(text)
    store.log_notification(alert_id, utcnow(), "feishu-e2e-test", ok, err, text)
    print(f"Selected: {label}" + (f" — {seg.get('taskNo')} {f.alert_type} {f.stage}; evidence: {f.reason}" if f else ""))
    print(f"Feishu: {client.last_result}")
    print("Feishu accepted the alert (HTTP 200, code 0)." if ok else f"Feishu send failed: {err}")
    print(f"Recorded as notification on alert {alert_id} in {config.DB_PATH}.")
    return 0 if ok else 1


def replay(path, at):
    from datetime import datetime
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    records = data if isinstance(data, list) else [r for v in data.values() for r in v]
    now = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN) if at else utcnow()
    store = Store(":memory:")
    store.upsert_segments({r["id"]: r for r in records}.values(), now)
    Engine(store, Notifier(store, log_dir=None, feishu_enabled=False)).run(now)
    print(f"Replay at {fmt_et(now)} ET: {len(store.open_alerts())} open alerts")


def keep_awake():
    """Windows: ask the OS not to sleep while the monitor runs (macOS uses `caffeinate` in the start script)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        except Exception as e:  # never stop monitoring because of this
            print(f"keep-awake not available: {e}", file=sys.stderr, flush=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows consoles/log files: always UTF-8 (Chinese text, emoji)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="bwi_monitor", description=__doc__.split("\n")[0])
    p.add_argument("--once", action="store_true", help="one poll and evaluation, then exit")
    p.add_argument("--no-web", action="store_true", help="do not start the dashboard")
    p.add_argument("--replay", metavar="FILE", help="evaluate saved API records offline")
    p.add_argument("--at", metavar="'YYYY-MM-DD HH:MM'", help="replay clock (Eastern)")
    p.add_argument("--feishu-test", action="store_true",
                   help="send one controlled test message to the configured Feishu webhook, then exit")
    p.add_argument("--authorize-feishu-shift", nargs="?", const="current", choices=["current", "next"],
                   help="authorize live Feishu for one shift: the open shift (default) or the next Start Shift")
    p.add_argument("--shift-report", nargs="?", const=0, type=int, metavar="SESSION",
                   help="print and save the report for a shift session (default: the latest)")
    p.add_argument("--feishu-alert-test", action="store_true",
                   help="one-time end-to-end test: detect a real TMS condition, re-check, send one labelled alert")
    args = p.parse_args(argv)

    if args.feishu_test:
        return feishu_test()
    if args.feishu_alert_test:
        return feishu_alert_test()
    if args.authorize_feishu_shift:
        store = Store(config.DB_PATH)
        current = store.open_session() if args.authorize_feishu_shift == "current" else None
        if current is not None:  # the pilot shift was already started: authorize exactly that shift
            store.set_meta("feishu_live_session", str(current["id"]))
            store.shift_event(current["reporting_date"], utcnow(), "feishu live delivery authorized for this open shift",
                              os.environ.get("USER"))
            print(f"Live Feishu delivery authorized for the open shift (session {current['id']}, started "
                  f"{fmt_et(from_iso(current['started_at']))} ET by {current['started_by']}) only.")
            return 0
        store.set_meta("feishu_shift_allowance", "1")
        store.shift_event("-", utcnow(), "feishu live delivery authorized for next shift", os.environ.get("USER"))
        live = config.FEISHU_ENABLED and not config.FEISHU_TEST_MODE
        print("Live Feishu delivery authorized for the next shift only."
              + ("" if live else " Note: this process sees FEISHU_ENABLED/FEISHU_TEST_MODE that keep delivery off."))
        return 0
    if args.shift_report is not None:
        from .report import build
        text = build(Store(config.DB_PATH), args.shift_report or None)
        os.makedirs(os.path.join(os.path.dirname(config.DB_PATH) or ".", "reports"), exist_ok=True)
        path = os.path.join(os.path.dirname(config.DB_PATH) or ".", "reports", f"shift_report_{utcnow():%Y%m%d_%H%M}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(text + f"\n\nSaved to {path}")
        return 0

    if args.replay:
        return replay(args.replay, args.at)

    store = Store(config.DB_PATH)
    store.set_meta("service_feishu_config", json.dumps({
        "enabled": config.FEISHU_ENABLED, "test_mode": config.FEISHU_TEST_MODE,
        "departure_enabled": config.FEISHU_DEPARTURE_ENABLED, "recorded_at": utcnow().isoformat()}))
    monitor = Monitor(store)
    if args.once:
        if monitor.poll():
            monitor.engine.run(utcnow())
        return
    if not args.no_web:
        from .web import serve
        if config.WEB_HOST not in ("127.0.0.1", "localhost") and not config.ADMIN_TOKEN:
            print("WARNING: dashboard is reachable from the network but BWI_ADMIN_TOKEN is not set; "
                  "anyone on the network can start or end the shift.", file=sys.stderr, flush=True)
        serve(config.DB_PATH)
        print(f"Dashboard: http://{config.WEB_HOST}:{config.WEB_PORT}", flush=True)
    try:
        monitor.run_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    sys.exit(main())
