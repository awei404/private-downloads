# BWI TMS real-time alert monitor

Implements `BWI_TMS_实时预警规则_V1.0.md` and phase 1–2 of `BWI_TMS_实时预警系统_软件需求说明书_V1.0.md`.

A background service polls GOFO TMS (read-only) every 10 minutes and re-checks deadlines every 60 seconds
even without new data. It keeps the alert state and history in SQLite, sends notifications only when an
alert changes stage, and serves a dashboard.

`go_arrive.py` / `anomaly.py` are the earlier prototype (V2 串点 logic), kept for reference only.

## Run

```bash
pip3 install -r requirements.txt # requests + tzdata (Python 3.9+, macOS or Linux)
export GOFO_AUTH='Bearer eyJ...' # from DevTools → Network → pageList → Request Headers
export GOFO_TAG='unauthorized'
python3 -m bwi_monitor           # dashboard: http://127.0.0.1:8765
```

Other modes:

```bash
python3 -m bwi_monitor --once                                   # one poll, then exit
python3 -m bwi_monitor --replay records.json --at "2026-10-11 12:00"   # offline replay, in-memory DB
python3 -m unittest tests.test_monitor                          # acceptance tests (SRS §13)
```

The first successful poll on an empty database is a **baseline**. It records the alerts that are
already open without sending notifications. Deleting `bwi_monitor.db` resets everything.

**Token refresh without restart:** put the `Authorization` value in a file and
`export GOFO_AUTH_FILE=/path/to/file`. The service re-reads it on each poll. When the token expires, the
dashboard shows a red "data may be out of date" banner and a `Monitor feed` alert is sent.

## Environments and deployment

```
GOFO TMS ──► BWI always-on computer: production monitor ──► one database ──► dashboard (staff PCs on the LAN)
                 time engine + operation-compliance engine            └──► Feishu bot (staff phones)
Separate test computer / instance: own database, own port, Feishu off ──► verify ──► deploy
```

**Production (BWI computer).** Copy the project to `/opt/bwi-monitor`, then:

```bash
cp .env.example .env                                   # BWI_WEB_HOST=0.0.0.0, set BWI_ADMIN_TOKEN
printf '%s' 'Bearer eyJ...' > secrets/gofo_auth        # TMS token
printf '%s' 'https://open.feishu.cn/...' > secrets/feishu_webhook   # NEW webhook, see Feishu below
chmod 600 secrets/*
```

- **macOS:** `deploy/com.bwi.monitor.plist` (launchd: starts at boot, restarts if it stops, keeps the Mac
  awake with `caffeinate`). Also `sudo pmset -a sleep 0`.
- **Linux:** `deploy/bwi-monitor.service` (systemd, `Restart=always`).
- **Docker:** `docker compose up -d` (`Dockerfile`, `docker-compose.yml`, secrets mounted from `secrets/`).
  **Not yet built or tested** — Docker is not installed on the development Mac.

Staff open `http://<BWI computer IP>:8765`. Viewing is open to the LAN; Start/End Shift need the admin
token. Data collection runs regardless of shift state; restarts recover alerts, shift sessions and
notification dedupe keys without re-sending.

**Development / testing** uses its own database, port and logs with Feishu off (`.env.dev.example`):

```bash
env $(cat .env.dev.example | grep -v '^#' | xargs) python3 -m bwi_monitor     # http://127.0.0.1:8766
python3 -m unittest tests.test_monitor                                        # Feishu always mocked
```

## Shifts

- **Three separate times** (corrected 2026-10-14): *actual arrival* (GPS, else manual), *warehouse opening*
  (confirmed with "Set opening time" on the dashboard, else provisionally the earliest valid morning manual
  check-in after 05:00), and *monitoring activation* (the Start Shift click).
- **Unload countdown start = MAX(actual arrival, that day's warehouse opening).** A truck that arrives while
  the warehouse is open starts at its arrival; one that arrives before opening (incl. after midnight)
  starts at the opening. Unknown opening → `PENDING`. Start Shift and End Shift never move a deadline.
- **START SHIFT** records the click time as the monitoring activation and turns operational notifications on.
- **END SHIFT** records the click time as the shift end and mutes operational notifications. Polling,
  rule evaluation, alert creation and history continue; running countdowns and deadlines are not touched.
  Alerts raised while muted are stored (channel `muted`) and are **never replayed** on the next start.
- The earliest valid TMS manual arrival of the day is shown as a **reference only**. If TMS shows a manual
  arrival but nobody started the shift, the dashboard shows a banner with the Start button.
- `BWI_DAY_BOUNDARY` (05:00) is only the reporting-day boundary. It never starts or closes a shift. A shift
  still open after the boundary shows a warning until someone confirms it is still active or ends it.
- Morning reminder (🔔, also to Feishu) at `BWI_SHIFT_REMINDER_TIME` (08:15), within a 3-hour window, once
  per day, only while no shift is open.
- Each session stores start time and user, end time and user, the earliest-manual-arrival reference and
  any "still open" confirmation. All changes are in `shift_events`.

## Branch-line departure monitor (2026-10-15)

`bwi_monitor/departures.py`. Deadlines come from the branch-line schedule (`BWI_BRANCH_SCHEDULE`):
ORF01 Day 0 23:00, RIC01 Day 1 00:00, DCA01 Day 1 02:00, IAD01 Day 1 02:30 (America/New_York). BWI01 is
excluded. Every BWI outbound segment to these stations uses its route's internal deadline, extra runs
included (e.g. `BWI.H-ORF01(02:00)` → 23:00 the evening before; DCA planned 03:00 → 02:00). The
operational day is the TMS planned date, rolled back before 05:00. The TMS planned time is kept and shown
next to the internal deadline; internal compliance is separate from official TMS punctuality.

**Voided tasks (已作废).** The arrival list has no void status and drops voided tasks. The authoritative
status comes from the read-only transport-task list (`task/transportTask/pageList`): `taskStatus "6"` =
Cancelled (verified on MT2026101200593). After each poll, segments the arrival list did not return are
searched by task number; found again → refreshed; still missing → task status checked: `6` → **VOIDED**
(excluded, open alerts resolved, history kept); anything else → **UNVERIFIED** (no alerts, shown as
"validity unverified", re-checked every poll). Before any departure alert the task status is checked too.

- Completion = valid manual departure (GPS not needed and never a substitute). ≤ deadline: on time
  (early is fine); later: late with the delay.
- No manual departure: 🟡 reminder 30 min before, 🔴 alert at the deadline (TMS re-checked before both).
- ✅ one completion message per segment for a new manual departure; old ones are not replayed.
- Departure messages are off (`FEISHU_DEPARTURE_ENABLED=false`) until the matching is verified.

Feishu message set: 🟡/🔴/✅ departures (when enabled), 🟡 30-min and 🟠 55-min unloading, ⚠️ missing
check-in (`FEISHU_OP_TYPES`). Arrival notices and the 60-min unloading message are off
(`FEISHU_ARRIVAL_NOTICE`, `FEISHU_UNLOAD_OVERDUE`); their records stay on the dashboard.

## Operation compliance (separate engine)

`bwi_monitor/compliance.py` checks the four TMS operations per segment, following the GOFO V2 operation
rate split (origin: loading, departure; destination: arrival, unloading). Results never affect the time
alerts, and the time alerts never affect them.

| Operation | Required when | Evidence | MISSING (in TMS records) | RISK (pending) |
|---|---|---|---|---|
| Loading | BWI origin, mode Load Only / Load and Unload | `loadBagCount` recorded (0 counts) or loading photo | departure recorded, no load count and no photo | — |
| Departure | BWI origin | manual departure record | next stop already shows arrival | GPS departure, no record after 30 min |
| Arrival | BWI destination | manual arrival check-in | unloading scans exist | GPS arrival, no check-in 30 min after MAX(GPS, shift start) |
| Unloading | BWI destination, mode Unload Only / Load and Unload | `unLoadBagCount` > 0 | single-stop truck marked unloaded or moved on with no scans | — |

`UNCERTAIN` (TMS data cannot tell, e.g. a multi-stop truck whose BWI share is unknown) and `PENDING` are
shown on the dashboard only. An empty field alone is never treated as a missing operation.

## Feishu notifications

`bwi_monitor/feishu.py`, custom-bot webhook. Settings: `FEISHU_ENABLED` (default false),
`FEISHU_TEST_MODE` (default true: messages are recorded as `feishu-test-mode`, not delivered), webhook in
`secrets/feishu_webhook` or `FEISHU_WEBHOOK_URL`, optional signing secret in `secrets/feishu_secret`,
optional admin webhook in `secrets/feishu_admin_webhook` for monitor failures.

| Trigger | Message |
|---|---|
| 30 min | 🟡 卸车提醒 |
| 55 min (5 min left; TMS re-checked first) | 🟠 卸车紧急提醒 |
| 60 min (TMS re-checked first) | 🔴 卸车超时 |
| Operation MISSING / RISK (TMS re-checked first) | ⚠️ 操作合规预警 |
| Morning, no shift open | 🔔 Shift reminder (sent even while alerts are muted) |
| Token expired / collection failing | admin channel only, if configured |

Messages use only TMS fields (task, plate/trailer if present, route, arrival and source, monitoring
start, deadline, current TMS status, warning time); missing values are left out. One message per
segment + alert type + severity, ever (stored in `notification_keys`, survives restarts). More than
`FEISHU_MAX_PER_TICK` (5) messages in one cycle go out as one digest. Delivery needs HTTP 200 **and**
Feishu `code 0`; HTTP 429/5xx and Feishu rate-limit codes are retried up to 3 times with backoff. Errors
are stored with the webhook redacted. Departure alerts stay on the dashboard unless
`FEISHU_INCLUDE_DEPARTURE=true`.

**Activation:** generate a new webhook in Feishu (the earlier one was exposed), write it to
`secrets/feishu_webhook`, run `python3 -m bwi_monitor --feishu-test` once, then set
`FEISHU_ENABLED=true` and, after checking the dashboard's notification status, `FEISHU_TEST_MODE=false`.

## Rules implemented

| Alert | Applies to | Deadline | Stages |
|---|---|---|---|
| Departure | origin `BWI.H` | `plannedDepartureTime` (no 串点 adjustment; early is always OK) | WARN 30 → WARN 15 → OVERDUE → OK / LATE |
| Unload 60 min — single-stop | destination `BWI.H`, one unloading on the route | countdown start + 60 min | reminder 30 → strong 5 (re-checked) → OVERDUE → OK / LATE / UNCERTAIN |
| Unload 60 min — multi-stop | destination `BWI.H`, multiple unloadings | countdown start + 60 min | reminder 30 → strong 5 (re-checked) → OVERDUE → OK / LATE / UNCERTAIN |
| Data checks | both | — | CHECK until the data is fixed |

**Unload deadline** = countdown start + 60 minutes, where countdown start = MAX(actual arrival, warehouse
opening) (see Shifts). Reminders: 30 min left (first reminder), 5 min left (strong, high priority; TMS is
re-fetched for that truck first), then overdue "based on TMS records". Departures are unchanged.
- **Single-stop:** the whole truck must be unloaded: `unLoadBagCount >= loadBagCount`. A short count with
  TMS marked complete is flagged as a data check.
- **Multi-stop:** only the first unloading scan is required: `unLoadBagCount > 0`. BWI's share of the
  cargo is not checked, and there are no short-quantity alerts.

**Arrival** = valid GPS/geofence arrival (`fenceActualArrivalTime`, parseable and not before departure),
else the manual arrival, else `actualArrivalTime`. Each alert shows the source, both timestamps and
`manualMinusGpsMinutes`. Invalid GPS or manual times are flagged as data checks. A GPS-vs-manual
difference is flagged only if `BWI_ARRIVAL_DIFF_FLAG_MINUTES` is set (threshold not confirmed).

**Unload timing comes from bag counts.** The API has no scan or completion timestamps that have been
verified; `unloadCompleteTime` is kept for reference only.

The API has counts, not scan times, so each poll records the first time a step was observed complete
and the last time it was observed incomplete (from a known count). These are observation times, not scan
times. Complete by the deadline → OK; a known incomplete count in a poll at/after the deadline → LATE
("overdue based on TMS records", not proof of the physical operation); window crossing the deadline →
UNCERTAIN; already complete when the monitor first saw the task → not judged (no historical flood).
A missing count is unknown: never zero, never a bound on the window.

Data checks are: no planned departure; arrived but not departed; arrival earlier than departure;
unloaded bags without an arrival; Arrived status with no arrival time; invalid GPS or manual arrival;
loaded bag count missing (single-unload trucks); TMS marked unload complete with fewer bags than the
reliable BWI quantity; unparseable timestamps.
Data checks are flagged for verification and never treated as KPI results.

- Each (segment, alert type) is one alert. It notifies on: new, escalated, deadline changed, resolved,
  completed late. Polls that find nothing new and service restarts do not re-send.
- Before an OVERDUE notice, the segment is re-fetched by `taskNo` to avoid alerting on stale data.
- Only segments whose planned departure (or BWI arrival) is within the last 48 h are monitored. An alert
  still OVERDUE/CHECK when its segment leaves the window is closed as "unresolved", not "cleared".
- Times are Eastern wall-clock from the API and are converted to UTC internally (DST-safe).
  `unloadCompleteTime` raw is 3 h off from Eastern, so `unloadCompleteTimeStrFormat` is used.

## Configuration (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `BWI_POLL_SECONDS` | 600 | confirmed 10 min |
| `BWI_TICK_SECONDS` | 60 | clock-driven re-evaluation |
| `BWI_WARN_MINUTES` | `30,15` | departure reminders (unchanged) |
| `BWI_UNLOAD_WARN_MINUTES` | `30,5` | unload: first reminder, strong reminder (minutes left) |
| `BWI_DAY_BOUNDARY` | `05:00` | reporting-day boundary only (never starts or ends a shift) |
| `BWI_OP_GRACE_MINUTES` | 30 | time to record a manual arrival/departure after GPS shows it (not confirmed) |
| `BWI_SHIFT_REMINDER_WINDOW_MINUTES` | 180 | reminder only within this window after the reminder time |
| `FEISHU_ENABLED`, `FEISHU_TEST_MODE` | false, true | see Feishu |
| `FEISHU_INCLUDE_DEPARTURE`, `FEISHU_MAX_PER_TICK` | false, 5 | |
| `BWI_SHIFT_REMINDER_TIME` | `08:15` | morning Start Shift reminder |
| `BWI_ADMIN_TOKEN` | empty | required by Start / End Shift when set (set it in production) |
| `GOFO_AUTH_FILE` | — | file holding the TMS Authorization value |
| `BWI_REPEAT_OVERDUE_MINUTES` | 0 | repeat reminders while overdue; 0 = off |
| `BWI_LOOKBACK_HOURS` | 48 | monitoring window |
| `BWI_STALE_AFTER_MINUTES` | 25 | feed alert when no successful poll for this long |
| `BWI_EXTERNAL_NOTIFY` | 0 | external notifications are **off** until the channel is decided |
| `BWI_WEBHOOK_URL` | empty | used only when `BWI_EXTERNAL_NOTIFY=1`; POSTs `{"text": ...}` |
| `BWI_DB_PATH`, `BWI_LOG_DIR` | `bwi_monitor.db`, `logs/` | notifications also go to `logs/notifications.jsonl` |
| `BWI_WEB_HOST`, `BWI_WEB_PORT` | `127.0.0.1`, `8765` | |
| `BWI_ARRIVAL_DIFF_FLAG_MINUTES` | 0 | flag GPS vs manual arrival gaps above this; 0 = off |

## Layout

| File | Purpose |
|---|---|
| `bwi_monitor/rules.py` | pure rule functions (departure, unload, data checks) |
| `bwi_monitor/engine.py` | alert lifecycle, dedupe, escalation, feed health |
| `bwi_monitor/shift.py` | shift sessions (Start/End), reference arrival, reminder, countdown context |
| `bwi_monitor/compliance.py` | operation compliance engine (loading, departure, arrival, unloading) |
| `bwi_monitor/feishu.py` | Feishu messages and delivery |
| `bwi_monitor/store.py` | SQLite: polls, segments + change history, alerts, events, notifications |
| `bwi_monitor/tms.py` | read-only API client (BWI filter `transitId=319`, retries, 401 detection) |
| `bwi_monitor/notify.py` | console + JSONL log + optional webhook |
| `bwi_monitor/web.py`, `dashboard.html` | dashboard and JSON API (`/api/state`, `/api/segment/<id>`) |

## Still open (needs business confirmation)

1. **Unverified TMS API behaviour** (all from the list API `transportTaskArrival/pageList`):
   - `manualActualDepartureTime` / `manualActualArrivalTime` are assumed to be the employee's TMS
     departure / arrival operations, and `fence*` times the GPS geofence events.
   - `loadBagCount` (including 0) and `loadedImg` are assumed to prove a loading operation; `unLoadBagCount`
     an unloading operation. No scan-level timestamps are available.
   - `originLoadingModeStr` / `destinationLoadingModeStr` are assumed to say which operations BWI must do.
   - An empty `unLoadBagCount` and `unloadCompleteTime` meanings are unverified.
   - Polling interval limits, token lifetime and whether the `taskNo` filter is officially supported.
2. Recording grace for manual arrival / departure (`BWI_OP_GRACE_MINUTES`, 30) is a default, not a rule.
3. GPS vs manual arrival difference that counts as a conflict (`BWI_ARRIVAL_DIFF_FLAG_MINUTES`).
4. Feishu: a new webhook, the group, and whether departure alerts should go to Feishu.
5. Docker deployment: not built or tested yet.
6. A TMS service account instead of a personal browser token (the token expires).
