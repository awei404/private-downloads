"""Settings. Business rules that are confirmed are constants; notification policy
(warning offsets, repeats, channel) is configurable through environment variables
because it has not been finalised by the business side."""
import os


def _int(name, default):
    return int(os.environ.get(name, default))


SITE_CODE = "BWI.H"
SITE_TRANSIT_ID = _int("BWI_TRANSIT_ID", 319)

# Confirmed rules (实时预警规则 V1.0 §3, §4)
UNLOAD_LIMIT_MINUTES = 60
RULE_VERSION = "BWI-V2.0"

# Flag GPS vs manual arrival differences above this many minutes; 0 = off (threshold not confirmed).
ARRIVAL_DIFF_FLAG_MINUTES = _int("BWI_ARRIVAL_DIFF_FLAG_MINUTES", 0)

# Collection: confirmed 10 minutes (2026-10-10)
POLL_SECONDS = _int("BWI_POLL_SECONDS", 600)
TICK_SECONDS = _int("BWI_TICK_SECONDS", 60)          # clock-driven re-evaluation
STALE_AFTER_MINUTES = _int("BWI_STALE_AFTER_MINUTES", 25)

def _minutes_list(name, default):
    return sorted({int(x) for x in os.environ.get(name, default).split(",") if x.strip()}, reverse=True)


# Departure reminders: minutes before the planned departure (unchanged candidate policy).
WARN_MINUTES = _minutes_list("BWI_WARN_MINUTES", "30,15")
# Unload reminders (confirmed 2026-10-14): 30 min left = first reminder, 5 min left (55 min elapsed) = strong,
# high-priority reminder re-checked against TMS first; 60 min = overdue.
UNLOAD_WARN_MINUTES = _minutes_list("BWI_UNLOAD_WARN_MINUTES", "30,5")

# Shifts (phase 3, 2026-10-14). A shift starts and ends only when an employee clicks Start / End Shift.
# DAY_BOUNDARY is the reporting-day boundary only (earliest-manual-arrival reference, reminder, open-overnight
# warning); it never starts or closes a shift. REMINDER_TIME triggers the morning Start Shift reminder.
DAY_BOUNDARY = os.environ.get("BWI_DAY_BOUNDARY", "05:00")
REMINDER_TIME = os.environ.get("BWI_SHIFT_REMINDER_TIME", "08:15")
# The reminder is only sent within this many minutes after REMINDER_TIME (not when the service starts mid-day).
REMINDER_WINDOW_MINUTES = _int("BWI_SHIFT_REMINDER_WINDOW_MINUTES", 180)
# Receiving (2026-10-17): business hours 12:00-19:00 apply to receiving only. First-scan deadline = MAX(arrival + 60,
# 14:00) for arrivals before 19:00; 14:00 next day for arrivals at/after 19:00.
RECEIVING_CLOSE = os.environ.get("BWI_RECEIVING_CLOSE", "19:00")
RECEIVING_BATCH_DEADLINE = os.environ.get("BWI_RECEIVING_BATCH_DEADLINE", "14:00")
# Receiving board: a truck that left its origin more than this many days ago and never arrived is a stale TMS task.
IN_TRANSIT_MAX_DAYS = _int("BWI_IN_TRANSIT_MAX_DAYS", 5)

# Vehicle arrival notices are sent only for arrivals at most this old when first seen (no backlog flood).
ARRIVAL_NOTIFY_MAX_AGE_MINUTES = _int("BWI_ARRIVAL_NOTIFY_MAX_AGE_MINUTES", 30)

# Operation compliance: minutes allowed to record a manual arrival / departure after GPS shows it (not confirmed).
OP_GRACE_MINUTES = _int("BWI_OP_GRACE_MINUTES", 30)

# Manual close on the dashboard (2026-10-17). Alerts listed here are records the team must keep: they cannot be
# closed by hand and clear only when TMS shows the operation (an overdue departure stays until the manual
# departure appears). Everything else can be closed with a name and a reason; the record and history remain.
KEEP_OPEN = {s.strip() for s in os.environ.get("BWI_KEEP_OPEN",
             "BRANCH_DEPARTURE:OVERDUE,UNLOAD:OVERDUE,OP:DEPARTURE:MISSING,OP:LOADING:MISSING,OP:UNLOADING:MISSING")
             .split(",") if s.strip()}


def closable(alert_type, stage):
    return f"{alert_type}:{stage}" not in KEEP_OPEN and not alert_type.startswith("SYSTEM")


# Optional shared token for the Start / End Shift buttons; empty = anyone who can open the dashboard.
ADMIN_TOKEN = os.environ.get("BWI_ADMIN_TOKEN", "")
REPEAT_OVERDUE_MINUTES = _int("BWI_REPEAT_OVERDUE_MINUTES", 0)   # 0 = no repeats
# Only segments whose anchor time (planned departure / actual arrival) is this recent are monitored.
LOOKBACK_HOURS = _int("BWI_LOOKBACK_HOURS", 48)

DB_PATH = os.environ.get("BWI_DB_PATH", "bwi_monitor.db")
LOG_DIR = os.environ.get("BWI_LOG_DIR", "logs")
WEB_HOST = os.environ.get("BWI_WEB_HOST", "127.0.0.1")
WEB_PORT = _int("BWI_WEB_PORT", 8765)
# Feishu custom-bot notifications (feishu.py). Off by default. In test mode operational messages are recorded
# but not delivered; only `python3 -m bwi_monitor --feishu-test` sends one controlled test message.
# The webhook (and optional signing secret) is read at send time from a protected file or the environment and is
# never logged.
def _bool(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


FEISHU_ENABLED = _bool("FEISHU_ENABLED", "false")
FEISHU_TEST_MODE = _bool("FEISHU_TEST_MODE", "true")
FEISHU_WEBHOOK_FILE = os.environ.get("FEISHU_WEBHOOK_FILE", "secrets/feishu_webhook")
FEISHU_SECRET_FILE = os.environ.get("FEISHU_SECRET_FILE", "secrets/feishu_secret")
FEISHU_ADMIN_WEBHOOK_FILE = os.environ.get("FEISHU_ADMIN_WEBHOOK_FILE", "secrets/feishu_admin_webhook")
FEISHU_INCLUDE_DEPARTURE = _bool("FEISHU_INCLUDE_DEPARTURE", "false")  # TMS-planned departure overdue (non-branch runs)
FEISHU_MAX_PER_TICK = _int("FEISHU_MAX_PER_TICK", 5)  # more messages in one cycle are sent as one digest
FEISHU_RETRIES = _int("FEISHU_RETRIES", 3)
# Branch-line departure schedule (2026-10-15): destination=HH:MM+day offset from the operational date.
BRANCH_SCHEDULE = os.environ.get("BWI_BRANCH_SCHEDULE", "ORF01=23:00+0,RIC01=00:00+1,DCA01=02:00+1,IAD01=02:30+1")
BRANCH_WARN_MINUTES = _minutes_list("BWI_BRANCH_WARN_MINUTES", "30")
# Feishu message types (2026-10-15). Departure messages stay off until matching is verified with the user.
FEISHU_DEPARTURE_ENABLED = _bool("FEISHU_DEPARTURE_ENABLED", "false")
FEISHU_ARRIVAL_NOTICE = _bool("FEISHU_ARRIVAL_NOTICE", "false")        # immediate arrival notices removed
FEISHU_UNLOAD_OVERDUE = _bool("FEISHU_UNLOAD_OVERDUE", "false")        # 60-min overdue: dashboard only for now
# Compliance warnings sent to Feishu; the others stay on the dashboard.
FEISHU_OP_TYPES = {t.strip() for t in os.environ.get("FEISHU_OP_TYPES", "OP:ARRIVAL").split(",") if t.strip()}

# Pilot safeguard: even with FEISHU_ENABLED and not in test mode, live delivery needs a per-shift authorization
# (python3 -m bwi_monitor --authorize-feishu-shift). It is used by the next Start Shift and ends with End Shift.
FEISHU_SHIFT_AUTHORIZATION = _bool("FEISHU_SHIFT_AUTHORIZATION", "true")


def secret(env_name, file_path):
    """A credential from the environment or a protected file; None if not configured."""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:
        with open(file_path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None
