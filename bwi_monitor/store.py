"""SQLite persistence: segment snapshots + history, poll runs, alerts, notifications.

Alert state lives here (not in memory) so a restart resumes open alerts without
re-sending notifications that already went out.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config, rules
from .driver_arrivals import DriverArrivals, SCHEMA as DRIVER_ARRIVAL_SCHEMA
from .driver_scores import DriverScores, SCHEMA as DRIVER_SCORE_SCHEMA

SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_runs (
    id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, ok INTEGER,
    records INTEGER, changed INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY, task_no TEXT, origin TEXT, destination TEXT,
    data TEXT NOT NULL, hash TEXT, first_seen TEXT, last_seen TEXT, last_changed TEXT
);
CREATE TABLE IF NOT EXISTS segment_history (
    segment_id INTEGER, seen_at TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS segment_history_seg ON segment_history(segment_id);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, segment_id INTEGER, alert_type TEXT, stage TEXT, warn_level INTEGER,
    status TEXT, outcome TEXT, deadline TEXT, minutes INTEGER, reason TEXT, action TEXT,
    evidence TEXT, rule_version TEXT, created_at TEXT, updated_at TEXT, closed_at TEXT,
    last_notified_at TEXT, acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS alerts_key ON alerts(segment_id, alert_type);
CREATE TABLE IF NOT EXISTS alert_events (
    alert_id INTEGER, at TEXT, from_stage TEXT, to_stage TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS shifts (
    op_date TEXT PRIMARY KEY, state TEXT, detected_start TEXT, start_record TEXT, alerts_enabled INTEGER,
    reminder_sent_at TEXT, ended_at TEXT, ended_by TEXT, resumed_at TEXT, resumed_by TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS shift_sessions (
    id INTEGER PRIMARY KEY, started_at TEXT, started_by TEXT, ended_at TEXT, ended_by TEXT,
    reporting_date TEXT, earliest_manual_ref TEXT, open_confirmed_at TEXT, open_confirmed_by TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notification_keys (key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS shift_events (
    op_date TEXT, at TEXT, event TEXT, user TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY, alert_id INTEGER, at TEXT, channel TEXT, ok INTEGER, error TEXT, text TEXT
);
"""


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def from_iso(s):
    return datetime.fromisoformat(s) if s else None


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.executescript(DRIVER_ARRIVAL_SCHEMA)
        self.driver_arrivals = DriverArrivals(self.db)
        self.db.executescript(DRIVER_SCORE_SCHEMA)
        self.driver_scores = DriverScores(self.db)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(segments)")}
        if "obs" not in cols:
            self.db.execute("ALTER TABLE segments ADD COLUMN obs TEXT")
        if "removed_at" not in cols:  # when TMS stopped returning the segment in the arrival list
            self.db.execute("ALTER TABLE segments ADD COLUMN removed_at TEXT")
        if "validity" not in cols:  # VOIDED (TMS taskStatus 6 Cancelled) | UNVERIFIED (missing, status not confirmed)
            self.db.execute("ALTER TABLE segments ADD COLUMN validity TEXT")
            self.db.execute("ALTER TABLE segments ADD COLUMN validity_detail TEXT")
            self.db.execute("UPDATE segments SET validity='UNVERIFIED', validity_detail='missing; re-check pending'"
                            " WHERE removed_at IS NOT NULL")
        if "priority" not in {r["name"] for r in self.db.execute("PRAGMA table_info(alerts)")}:
            self.db.execute("ALTER TABLE alerts ADD COLUMN priority TEXT")

    # ---- polls ---------------------------------------------------------------
    def start_poll(self, now):
        return self.db.execute("INSERT INTO poll_runs(started_at) VALUES (?)", (iso(now),)).lastrowid

    def finish_poll(self, run_id, ok, records=0, changed=0, error=None):
        self.db.execute("UPDATE poll_runs SET finished_at=?, ok=?, records=?, changed=?, error=? WHERE id=?",
                        (iso(utcnow()), int(ok), records, changed, error, run_id))

    def last_poll(self, ok_only=False):
        q = "SELECT * FROM poll_runs WHERE finished_at IS NOT NULL" + (" AND ok=1" if ok_only else "")
        return self.db.execute(q + " ORDER BY id DESC LIMIT 1").fetchone()

    # ---- segments ------------------------------------------------------------
    @staticmethod
    def observe(obs, seg, now):
        """Update unload observations: per step, first poll seen complete and last poll seen incomplete.
        Unknown counts leave observations unchanged; a count that goes back down restarts the step."""
        if seg.get("destination") != config.SITE_CODE:
            return obs
        obs = dict(obs)
        obs.setdefault("first_poll", iso(now))
        for step, state in rules.unload_states(seg).items():
            if state is None:
                continue
            if state:
                obs.setdefault(f"{step}_seen", iso(now))
            else:
                obs.pop(f"{step}_seen", None)
                obs[f"{step}_last_not"] = iso(now)
        return obs

    def upsert_segments(self, records, now):
        """Store records; append to history only when the content changed. Returns # changed."""
        changed = 0
        with self.db:
            self.db.execute("BEGIN")
            for r in records:
                r = {k: v for k, v in r.items() if k != "_obs"}
                data = json.dumps(r, sort_keys=True, ensure_ascii=False)
                h = hashlib.sha1(data.encode()).hexdigest()
                row = self.db.execute("SELECT hash, obs FROM segments WHERE id=?", (r["id"],)).fetchone()
                obs = json.dumps(self.observe(json.loads(row["obs"] or "{}") if row else {}, r, now))
                if row and row["hash"] == h:
                    self.db.execute("UPDATE segments SET last_seen=?, obs=?, removed_at=NULL, validity=NULL,"
                                    " validity_detail=NULL WHERE id=?", (iso(now), obs, r["id"]))
                    continue
                changed += 1
                self.db.execute(
                    "INSERT INTO segments(id, task_no, origin, destination, data, hash, first_seen, last_seen,"
                    " last_changed, obs) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET task_no=excluded.task_no,"
                    " origin=excluded.origin, destination=excluded.destination, data=excluded.data,"
                    " hash=excluded.hash, last_seen=excluded.last_seen, last_changed=excluded.last_changed,"
                    " obs=excluded.obs, removed_at=NULL, validity=NULL, validity_detail=NULL",
                    (r["id"], r.get("taskNo"), r.get("placeOfOrigin"), r.get("destination"), data, h,
                     iso(now), iso(now), iso(now), obs))
                self.db.execute("INSERT INTO segment_history VALUES (?,?,?)", (r["id"], iso(now), data))
        return changed

    @staticmethod
    def _seg(row):
        seg = json.loads(row["data"])
        seg["_obs"] = json.loads(row["obs"] or "{}")
        if row["validity"]:
            seg["_validity"] = row["validity"]
            seg["_validity_detail"] = row["validity_detail"]
        return seg

    def segments(self):
        return [self._seg(r) for r in self.db.execute("SELECT data, obs, validity, validity_detail FROM segments")]

    def segment(self, seg_id):
        row = self.db.execute("SELECT data, obs, validity, validity_detail FROM segments WHERE id=?",
                              (seg_id,)).fetchone()
        return self._seg(row) if row else None

    def missing_since(self, poll_started, since):
        """Segments not returned by the poll that started at poll_started, still marked present, and
        relevant (last seen after `since`)."""
        return [dict(r) for r in self.db.execute(
            "SELECT id, task_no, last_seen FROM segments WHERE (validity IS NULL OR validity='UNVERIFIED')"
            " AND last_seen < ? AND last_seen >= ?", (iso(poll_started), iso(since)))]

    def mark_validity(self, seg_id, now, validity, detail):
        self.db.execute("UPDATE segments SET removed_at=COALESCE(removed_at, ?), validity=?, validity_detail=?"
                        " WHERE id=?", (iso(now), validity, detail, seg_id))

    def segment_history(self, seg_id):
        return [dict(r) for r in self.db.execute(
            "SELECT seen_at, data FROM segment_history WHERE segment_id=? ORDER BY seen_at", (seg_id,))]

    # ---- alerts --------------------------------------------------------------
    def latest_alert(self, seg_id, alert_type):
        return self.db.execute("SELECT * FROM alerts WHERE segment_id=? AND alert_type=? ORDER BY id DESC LIMIT 1",
                               (seg_id, alert_type)).fetchone()

    def open_alerts(self):
        return self.db.execute("SELECT * FROM alerts WHERE status='open'").fetchall()

    def create_alert(self, seg_id, f, now, status="open", outcome=None):
        aid = self.db.execute(
            "INSERT INTO alerts(segment_id, alert_type, stage, warn_level, priority, status, outcome, deadline,"
            " minutes, reason, action, evidence, rule_version, created_at, updated_at, closed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (seg_id, f.alert_type, f.stage, f.warn_level, f.priority, status, outcome, iso(f.deadline), f.minutes,
             f.reason, f.action, json.dumps(f.evidence, ensure_ascii=False), config.RULE_VERSION, iso(now),
             iso(now), iso(now) if status == "closed" else None)).lastrowid
        self.event(aid, now, None, f.stage, "created")
        return aid

    def update_alert(self, alert, f, now, note):
        self.db.execute("UPDATE alerts SET stage=?, warn_level=?, priority=?, deadline=?, minutes=?, reason=?,"
                        " action=?, evidence=?, updated_at=? WHERE id=?",
                        (f.stage, f.warn_level, f.priority, iso(f.deadline), f.minutes, f.reason, f.action,
                         json.dumps(f.evidence, ensure_ascii=False), iso(now), alert["id"]))
        if alert["stage"] != f.stage or note != "refresh":
            self.event(alert["id"], now, alert["stage"], f.stage, note)

    def close_alert(self, alert, now, outcome, stage=None):
        self.db.execute("UPDATE alerts SET status='closed', outcome=?, stage=COALESCE(?, stage), closed_at=?,"
                        " updated_at=? WHERE id=?", (outcome, stage, iso(now), iso(now), alert["id"]))
        self.event(alert["id"], now, alert["stage"], stage or alert["stage"], f"closed: {outcome}")

    def manual_close(self, alert_id, now, user, reason):
        """Close an open alert by hand. The alert, its history and the reason are kept."""
        row = self.db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if row is None or row["status"] != "open":
            return None
        self.close_alert(row, now, f"manually closed by {user}: {reason}")
        return row

    def acknowledge(self, alert_id, now):
        """'Seen / in progress' marker. Does not close the business alert (SRS §8.3)."""
        self.db.execute("UPDATE alerts SET acknowledged_at=? WHERE id=?", (iso(now), alert_id))
        self.event(alert_id, now, None, None, "acknowledged")

    def event(self, alert_id, now, from_stage, to_stage, note):
        self.db.execute("INSERT INTO alert_events VALUES (?,?,?,?,?)", (alert_id, iso(now), from_stage, to_stage, note))

    def mark_notified(self, alert_id, now):
        self.db.execute("UPDATE alerts SET last_notified_at=? WHERE id=?", (iso(now), alert_id))

    def log_notification(self, alert_id, now, channel, ok, error, text):
        self.db.execute("INSERT INTO notifications(alert_id, at, channel, ok, error, text) VALUES (?,?,?,?,?,?)",
                        (alert_id, iso(now), channel, int(ok), error, text))

    # ---- shifts --------------------------------------------------------------
    def shift(self, op_date):
        row = self.db.execute("SELECT * FROM shifts WHERE op_date=?", (str(op_date),)).fetchone()
        return dict(row) if row else None

    def shifts(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM shifts ORDER BY op_date")]

    def save_shift(self, sh, now):
        sh = dict(sh, op_date=str(sh["op_date"]), updated_at=iso(now))
        sh.setdefault("created_at", iso(now))
        cols = ["op_date", "state", "detected_start", "start_record", "alerts_enabled", "reminder_sent_at",
                "ended_at", "ended_by", "resumed_at", "resumed_by", "created_at", "updated_at"]
        self.db.execute(f"INSERT OR REPLACE INTO shifts({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [sh.get(c) for c in cols])

    def sessions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM shift_sessions ORDER BY id")]

    def open_session(self):
        row = self.db.execute("SELECT * FROM shift_sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def start_session(self, now, user, reporting_date, earliest_ref):
        return self.db.execute("INSERT INTO shift_sessions(started_at, started_by, reporting_date, earliest_manual_ref)"
                               " VALUES (?,?,?,?)", (iso(now), user, str(reporting_date), earliest_ref)).lastrowid

    def end_session(self, session_id, now, user):
        self.db.execute("UPDATE shift_sessions SET ended_at=?, ended_by=? WHERE id=?", (iso(now), user, session_id))

    def confirm_open_session(self, session_id, now, user):
        self.db.execute("UPDATE shift_sessions SET open_confirmed_at=?, open_confirmed_by=? WHERE id=?",
                        (iso(now), user, session_id))

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))

    def claim_notification_key(self, key, now):
        """True the first time a key is claimed: one external notification per task/segment/type/severity."""
        try:
            self.db.execute("INSERT INTO notification_keys(key, at) VALUES (?,?)", (key, iso(now)))
            return True
        except sqlite3.IntegrityError:
            return False

    def last_delivery(self, channel_prefix):
        row = self.db.execute("SELECT * FROM notifications WHERE channel LIKE ? ORDER BY id DESC LIMIT 1",
                              (channel_prefix + "%",)).fetchone()
        return dict(row) if row else None

    def muted_alert_ids(self, alert_ids):
        if not alert_ids:
            return set()
        q = f"SELECT DISTINCT alert_id FROM notifications WHERE channel='muted' AND alert_id IN ({','.join('?' * len(alert_ids))})"
        return {r["alert_id"] for r in self.db.execute(q, alert_ids)}

    def shift_event(self, op_date, now, event, user=None, detail=None):
        self.db.execute("INSERT INTO shift_events VALUES (?,?,?,?,?)", (str(op_date), iso(now), event, user, detail))

    def shift_events(self, limit=50):
        return [dict(r) for r in self.db.execute("SELECT * FROM shift_events ORDER BY at DESC LIMIT ?", (limit,))]

    def alerts_for_segment(self, seg_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM alerts WHERE segment_id=? ORDER BY id", (seg_id,))]

    def alert_events(self, alert_ids):
        if not alert_ids:
            return []
        q = f"SELECT * FROM alert_events WHERE alert_id IN ({','.join('?' * len(alert_ids))}) ORDER BY at"
        return [dict(r) for r in self.db.execute(q, alert_ids)]

    def notifications_for(self, alert_ids):
        if not alert_ids:
            return []
        q = f"SELECT * FROM notifications WHERE alert_id IN ({','.join('?' * len(alert_ids))}) ORDER BY at"
        return [dict(r) for r in self.db.execute(q, alert_ids)]

    def recent_alerts(self, now, hours=24):
        since = iso(now - timedelta(hours=hours))
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM alerts WHERE status='open' OR updated_at>=? ORDER BY id DESC", (since,))]
