"""End-of-shift report for one shift session, built only from the database (python3 -m bwi_monitor --shift-report).

Counts come from recorded notifications (one row per alert transition, delivered or muted), poll runs and the
stored TMS segments, so every number can be traced back to a row.
"""
import json
from collections import Counter
from datetime import timedelta

from . import config, rules, shift
from . import departures as branch
from .store import from_iso, utcnow


def _rows(store, sql, args=()):
    return [dict(r) for r in store.db.execute(sql, args)]


def build(store, session_id=None, now=None):
    now = now or utcnow()
    sessions = store.sessions()
    if not sessions:
        return "No shift sessions recorded."
    s = next((x for x in sessions if x["id"] == session_id), None) if session_id else sessions[-1]
    if s is None:
        return f"Shift session {session_id} not found."
    start, end = from_iso(s["started_at"]), from_iso(s["ended_at"]) or now
    a, b = s["started_at"], (s["ended_at"] or now.isoformat(timespec="seconds"))
    hours = (end - start).total_seconds() / 3600
    fmt = rules.fmt_et

    # ---- service health -------------------------------------------------------
    polls = _rows(store, "SELECT * FROM poll_runs WHERE started_at>=? AND started_at<=? ORDER BY id", (a, b))
    ok = [from_iso(p["finished_at"]) for p in polls if p["ok"]]
    failed = [p for p in polls if not p["ok"]]
    marks = [start] + ok + [end]
    gaps = [(x, y) for x, y in zip(marks, marks[1:]) if y - x > timedelta(minutes=config.POLL_SECONDS / 60 + 5)]
    gap_minutes = sum((y - x).total_seconds() / 60 - config.POLL_SECONDS / 60 for x, y in gaps)
    expected = max(1, int((end - start).total_seconds() // config.POLL_SECONDS))
    availability = max(0.0, 1 - gap_minutes / max(1, (end - start).total_seconds() / 60))

    # ---- transitions recorded during the shift ----------------------------------
    notes = _rows(store, "SELECT n.*, al.alert_type, al.stage AS alert_stage, al.segment_id, al.reason AS alert_reason "
                         "FROM notifications n LEFT JOIN alerts al ON al.id = n.alert_id "
                         "WHERE n.at>=? AND n.at<=? ORDER BY n.id", (a, b))
    local = [n for n in notes if n["channel"] in ("log", "muted")]
    first_line = lambda n: (n["text"] or "").split("\n")[0]
    count = Counter()
    for n in local:
        line, t = first_line(n), n["alert_type"] or ""
        if t == "ARRIVAL":
            count["arrival notices"] += 1
        elif t == "UNLOAD" and "HIGH PRIORITY" in line:
            count["unload 55-min urgent"] += 1
        elif t == "UNLOAD" and "min left" in line:
            count["unload 30-min reminder"] += 1
        elif t == "UNLOAD" and "OVERDUE" in line:
            count["unload overdue"] += 1
        elif t == "DEPARTURE" and "OVERDUE" in line:
            count["departure overdue"] += 1
        elif t == "OP:ARRIVAL":
            count["missing manual check-in"] += 1
        elif t == "OP:DEPARTURE":
            count["missing manual departure"] += 1
        elif t.startswith("OP:"):
            count["other operation warnings"] += 1
        elif t.startswith("DATA:"):
            count["data checks"] += 1
    feishu = [n for n in notes if (n["channel"] or "").startswith("feishu")]
    live = [n for n in feishu if not n["channel"].endswith("test-mode") and n["channel"] not in ("feishu-test", "feishu-e2e-test")]

    # ---- traffic seen during the shift -----------------------------------------
    segs = store.segments()
    by_task = {}
    for x in segs:
        by_task.setdefault(x.get("taskNo"), []).append(x)
    ctx = dict(shift.context(store, segs), by_task=by_task)
    arrivals, departures, routes, uncertain, inconsistent = [], [], Counter(), [], []
    for x in segs:
        if x.get("destination") == config.SITE_CODE:
            t = rules.arrival(x)[0]
            if t and start <= t <= end:
                arrivals.append(x)
                routes["multi-stop" if "multiple unloadings" in (x.get("handlingModeStr") or "").lower() else "single-stop"] += 1
        if x.get("placeOfOrigin") == config.SITE_CODE:
            t = rules.departure_times(x)[0]
            if t and start <= t <= end:
                departures.append(x)
        if x in arrivals or x in departures:
            for f in rules.evaluate(x, end, ctx):
                st = f.evidence.get("opStatus")
                if f.stage in (rules.UNCERTAIN,) or st == "UNCERTAIN":
                    (inconsistent if f.evidence.get("inconsistency") else uncertain).append((x, f))

    # ---- departures: branch-line tasks whose internal deadline fell inside the shift ---------------
    dep = Counter()
    dep_rows = []
    for x in segs:
        m = branch.match(x)
        if not m or "unmatched" in m or not (start <= m["deadline"] <= end):
            continue
        if x.get("_validity"):
            dep["voided / excluded"] += 1
            continue
        dep["valid outbound tasks"] += 1
        manual = rules.seg_time(x, "manualActualDepartureTimeStrFormat")
        if manual is None:
            dep["missing manual departure"] += 1
            dep_rows.append(f"- {m['route']} {x['taskNo']}: deadline {fmt(m['deadline'])} — no manual departure")
            continue
        margin = int((m["deadline"] - manual).total_seconds())
        dep["early" if margin > 0 else "exactly at deadline" if margin == 0 else "late"] += 1
        if margin < 0:
            dep_rows.append(f"- {m['route']} {x['taskNo']}: manual {fmt(manual)} — {branch.margin_text(margin)}")
    delivered = [n for n in live if n["ok"]]
    prefix = lambda p: sum(1 for n in delivered if (n["text"] or "").startswith(p))

    open_alerts = [al for al in store.open_alerts() if al["segment_id"]]
    seg_by_id = {x["id"]: x for x in segs}
    backfill = _rows(store, "SELECT * FROM alerts WHERE alert_type='ARRIVAL' AND outcome LIKE 'arrived (backfill%' "
                            "AND created_at>=? AND created_at<=?", (a, b))
    missing = [n for n in local if (n["alert_type"] or "").startswith("OP:") and "missing in TMS records" in first_line(n)]
    risks = [n for n in local if (n["alert_type"] or "").startswith("OP:") and "pending risk" in first_line(n)]

    def task(sid):
        x = seg_by_id.get(sid) or {}
        return f"{x.get('taskNo')} {x.get('placeOfOrigin')}→{x.get('destination')}"

    L = [f"# BWI TMS pilot shift report — session {s['id']}", "",
         "## Shift", f"- Start Shift: {fmt(start)} ET by {s['started_by']}",
         f"- End Shift: {fmt(from_iso(s['ended_at'])) + ' ET by ' + str(s['ended_by']) if s['ended_at'] else 'still open (report as of ' + fmt(now) + ' ET)'}",
         *(f"- Live Feishu authorized: {fmt(from_iso(e['at']))} ET by {e['user']}" for e in reversed(store.shift_events(200))
           if "authorized for this open shift" in (e["event"] or "") and a <= e["at"] <= b),
         f"- Monitored: {hours:.1f} h; polls OK {len(ok)} / expected ~{expected}; failed {len(failed)}; "
         f"service availability ≈ {availability:.0%}",
         *(f"  - polling gap {fmt(x)} → {fmt(y)} ET ({(y - x).total_seconds() / 60:.0f} min)" for x, y in gaps),
         *(f"  - poll failure {fmt(from_iso(p['started_at']))}: {p['error']}" for p in failed[:10]),
         "", "## Transportation monitored",
         f"- BWI arrivals during the shift: {len(arrivals)} (single-stop {routes['single-stop']}, multi-stop {routes['multi-stop']})",
         f"- BWI departures during the shift: {len(departures)}",
         f"- Arrival events recorded (no Feishu arrival notices): {count['arrival notices']}; recorded as backfill (older than "
         f"{config.ARRIVAL_NOTIFY_MAX_AGE_MINUTES} min when first seen): {len(backfill)}",
         "", "## Departures (internal deadlines inside the shift)",
         f"- Valid outbound tasks: {dep['valid outbound tasks']}; voided / excluded: {dep['voided / excluded']}",
         f"- Early: {dep['early']}; exactly at deadline: {dep['exactly at deadline']}; late: {dep['late']}; "
         f"missing manual departure: {dep['missing manual departure']}",
         f"- Feishu sent: 🟡 reminders {prefix('🟡 BWI 发车提醒')}, 🔴 deadline alerts {prefix('🔴 BWI 发车截止告警')}, "
         f"✅ completions {prefix('✅ BWI 发车完成')}",
         *dep_rows,
         f"- Unloading Feishu sent: 🟡 30-min {prefix('🟡 BWI TMS — 卸车提醒')}, 🟠 55-min {prefix('🟠 BWI TMS — 卸车紧急提醒')}, "
         f"⚠️ check-in {prefix('⚠️ BWI TMS — TMS人工签到异常')}",
         "", "## Time alerts",
         f"- 30-min unloading reminders: {count['unload 30-min reminder']}",
         f"- 55-min urgent reminders: {count['unload 55-min urgent']}",
         f"- Unloading overdue: {count['unload overdue']}",
         f"- Departure overdue: {count['departure overdue']}",
         "", "## Compliance alerts",
         f"- Missing manual check-in (risk/missing): {count['missing manual check-in']}",
         f"- Missing manual departure: {count['missing manual departure']}",
         f"- Other operation warnings: {count['other operation warnings']}",
         f"- Data checks: {count['data checks']}",
         f"- Uncertain records: {len(uncertain)}; inconsistent TMS timestamps: {len(inconsistent)}",
         "", "## Feishu delivery",
         f"- Live attempts: {len(live)}; delivered: {sum(1 for n in live if n['ok'])}; failed: {sum(1 for n in live if not n['ok'])}",
         f"- Recorded without delivery (test mode / not authorized): {sum(1 for n in feishu if n['channel'].endswith('test-mode'))}",
         f"- Muted (shift closed): {sum(1 for n in notes if n['channel'] == 'muted')}",
         f"- Duplicates prevented (all time): {store.get_meta('duplicates_prevented') or 0}",
         *(f"  - failed {fmt(from_iso(n['at']))}: {n['error']}" for n in live if not n["ok"]),
         "", "## Accuracy review",
         "### Missing in TMS records (operation not recorded; verify before treating as a staff violation)",
         *(f"- {task(n['segment_id'])}: {first_line(n)}" for n in missing), *(["- none"] if not missing else []),
         "### Operational risks (not yet recorded, may be delayed)",
         *(f"- {task(n['segment_id'])}: {first_line(n)}" for n in risks), *(["- none"] if not risks else []),
         "### Uncertain / inconsistent TMS data",
         *(f"- {x.get('taskNo')} {f.alert_type}: {f.reason}" for x, f in inconsistent + uncertain),
         *(["- none"] if not (inconsistent or uncertain) else []),
         "### Still open at report time (manual review)",
         *(f"- {task(al['segment_id'])}: {al['alert_type']} {al['stage']} — {al['reason']}" for al in open_alerts),
         *(["- none"] if not open_alerts else [])]
    return "\n".join(L)
