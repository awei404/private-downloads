# BWI TMS real-time alert monitor

## 2026.10.21.1 · Windows 刷新打不开修复、PNE 货量

- **Windows 刷新就打不开**：升级时旧程序没被停掉（venv 的 python.exe 只是启动器，真正运行的是它的子进程），Windows 又允许两个程序占同一个端口，于是刷新时随机连到新旧两个程序，还可能重复轮询、重复发飞书。现在：Windows 独占端口，第二份程序直接退出；安装程序停掉所有正在运行的监控和占用端口的程序；页面底部显示“版本 · 进程号”。
- **其他电脑用电脑名打开（http://电脑名:8766）读不到数据**：现在接受本机电脑名（另可在 `.env` 设 `BWI_ALLOWED_HOSTS`）。
- 页面出错时显示具体原因，不再统一显示“无法连接监控服务”；服务器错误写入日志。
- **PNE01**：EWR→BWI→PNE01→EWR 车上的 PNE01 货也是我们的，直接按装车票数统计，不需要手动输入（`BWI_OWN_STOPS` 可增加站点）。

## 2026.10.19.2 · Mac 首次安装用终端

- Mac 会拦截网上下载、没有 Apple 签名的脚本（双击提示“无法打开”）。第一次请在“终端”运行：`bash ~/Downloads/BWI-Monitor-Mac/install.sh`。安装程序会清除下载标记，之后双击 `.command` 就能打开。包里附《双击打不开请看.txt》。

## 2026.10.19.1 · Mac 版

- 新增 Mac 安装包 `BWI-Monitor-Mac.zip`：解压后双击 `Install-BWI-Monitor.command`。装到 `~/BWI-Monitor`，登录后自动运行、出错自动重启；缺 token / webhook 时弹窗粘贴；升级直接覆盖，数据保留。详见 [Mac 说明](deploy/mac/README-Mac.md)。
- 支持 Mac 自带的 Python 3.9。GitHub 自动检查同时在 Windows 和 Mac 上安装、升级并启动监控。

## 2026.10.18.4 · 双击安装、网页更新 TMS 登录

- Windows：解压后双击 `Install-BWI-Monitor.bat`；缺 TMS token 或飞书 webhook 时弹窗粘贴，不用自己放文件。管理员口令改为 6 位数字。
- TMS 登录过期：监控台红色按钮“更新 TMS 登录”（或点顶部 TMS 标签）粘贴新 token，先验证有效才保存，并立即重新拉取。Mac 同样适用。
- Windows 自动检查（GitHub Actions）现在会真正启动监控程序，确认监控台、数据接口和 token 验证都能用。

## 2026.10.18.3 · 收车只看票数

- 货量一律按**票数**（TMS waybill 数）显示和统计，不再显示袋数；手动输入也只填票数。

## 2026.10.18.2 · 收车：卸车中与货量

- 收车分三块：**未到达 → 卸车中（已到达、TMS 尚未标记卸车完成）→ 已到达（卸车完成）**。前一班次到达、还没卸完的车留在“卸车中”。
- 未到达不再显示计划到达，只显示预计到达和货量。
- 每辆车显示到 BWI 的货量（袋 · 件）：手填 > TMS 实卸（卸完后）> TMS 装车（整车都是 BWI 的货）。CNO→BWI→JFK 这类车装车数含下一站的货，显示“待确认”，可点“填写”手动输入（需姓名，留空可清除）。
- 新增货量统计：总货量、已到达货量、未到达货量，并标出几辆待确认。

## 2026.10.18.1 · 不再需要命令

- **飞书**：每个班次不再需要运行授权命令。点“上班 Start Shift”后自动正式发送，“下班 End Shift”后停止。正式发送 / 测试模式在监控台顶部“飞书 …⇄”标签点击切换（`FEISHU_SHIFT_AUTHORIZATION=true` 可恢复试运行时的逐班授权）。
- **管理员口令、姓名**：每台电脑的浏览器只需输入一次，之后自动记住；口令改了会重新询问。Windows 安装结束时显示口令。
- **隐藏任务**：收车、发车表格每行有“隐藏”（需姓名和原因）。隐藏后不显示、不提醒，已开的预警以“hidden”结束；表格下方“已隐藏的任务”可恢复；班次报告列出隐藏记录。
- **监控中断提醒**：上班期间 TMS 登录失效或 25 分钟没有数据，飞书发“⛔ BWI 监控数据中断”，恢复后发“✅ 已恢复”。
- **电脑睡眠后**：唤醒后立即拉取，不再等满 10 分钟（Mac 合盖会睡眠，请保持打开）。

## Windows 新版与 Check-in 打印（2026.10.17.1）

[下载 Windows 最新 ZIP](https://github.com/awei404/private-downloads/releases/latest/download/BWI-Monitor-Windows.zip)。解压后双击 `Install-BWI-Monitor.bat` 安装或升级；本机数据库、凭证和设置保留，升级备份集中在安装目录 `backups`。详细步骤见 [Windows 说明](deploy/windows/README-Windows.md)。发布包不包含运营数据或凭证。

任务旁和任务详情中新增“打印 Check-in”。使用保存的实际到场、旧调度确认约定，以及 TMS 登记的司机、供应商、车牌；首站地址沿用旧 Check-in 页。打印页可补填 Dock、回货要求和备注，并有司机须知签字栏。使用 Letter 纸或另存 PDF。打印内容不写回 TMS、不改变评分；未登记的时间保持待填写。

本次新增司机功能前后，`rules.py`、`departures.py`、`compliance.py`、`engine.py`、`shift.py`、`tms.py`、`notify.py`、`feishu.py`、`config.py`、`__main__.py` 的检测和通知逻辑保持一致。Windows 使用单独入口组合原监控和只读司机同步。

## 司机到场、评分与 Excel 导出（2026-10-17）

本机入口：[司机看板](http://127.0.0.1:8768/dashboard-v2#tab=scores)。在“司机评分与导出”查看任务与司机汇总，或从发车列表打开任务详情。司机功能仅用于**整个 MT 任务首次从 BWI.H 出发，首段前往 ORF01、RIC01、DCA01、IAD01 的四条支线**；不包含从其他站始发后途经 BWI 的串点任务、收车任务或其他线路。BWI 始发后到支线再返回 BWI 的往返任务按首次发车支线统计，同一任务不重复计分。范围限制也适用于同步、编辑、评分和导出。

### 司机资料与现场到场

- 司机资料从 TMS 读取可用信息，也可点击“同步 TMS 司机”请求更新。已实测派车任务详情的司机姓名、电话字段；按同一 MT 的任务 ID 读取，列表中的空字段不会覆盖详情，车辆默认司机也不会代替任务登记司机。未登记的任务仍可能缺资料。缺失项显示待同步或待补齐；身份资料不可靠时，不用车牌代替司机、不按同名猜测合并，也不纳入司机总分。
- **“司机实际到场（美东）”由现场填写完整日期和时间**，可修改，清除前页面要求确认。它与 TMS 的 GPS、人工收车、发车时间独立，不从这些字段或当前时间自动补成事实。旧历史回填也不作为新登记的实际到场。
- 同一 MT 在 BWI 共用一条到场登记和一份任务评分；多航段和重复同步不会新增同趟扣分。变更记录保留，遇到其他人的修改时先重新读取，再核对保存。
- 约定到场只沿用旧调度中已确认匹配同一 MT 的具体约定。缺少约定则待确认；不用 TMS 计划发车、固定线路时刻或历史最常见时间推定。迟到比较的是**司机约定到 BWI 的时间与现场实际到场**，不是发车截止时间。

### 完整任务评分与司机总分

每趟任务以 100 分开始，按旧调度当前规则计算：

- 跑错线路：本趟直接记 0 分。
- 司机迟到：扣 45 分；按时或提前不扣分。
- 回货：无需回货或已完成不扣分；未完成扣 25 分；部分完成扣 10 分。
- 人工扣分：0–100 分，有扣分时必须填写原因；最终任务分最低为 0。

“其他扣分（发生时填写）”中记录线路错误、回货结果和人工扣分。缺实际到场、缺已确认约定，或回货结果为待确认时，任务保留待评，不按满分处理，也不进入自动均分；**已确认跑错线路则记 0 分，即使其他资料仍待补充**。到场项评分与完整任务评分分别显示，原有发车、收车、卸货及操作缺失预警不会自动变成司机扣分。

司机自动总分是严格近 **30 个营业日**内已评分任务的算术平均：沿用现有美东 **05:00** 营业日边界，以当前营业日及之前 29 个营业日为窗口；页面展示服务端计算的窗口。没有可计分任务显示待评。日期、线路筛选决定展示的任务和司机名单，每位司机的均分仍按固定近 30 个营业日计算。

“调整总分”在独立窗口维护人工总分（0–100），必须填写原因；人工总分覆盖显示值，自动均分继续保留。选择“恢复自动评分”并填写原因后取消覆盖。人工总分调整与单趟扣分分开存储，不覆盖任务评分或其他未保存输入。

### Excel 导出

“导出 Excel”按已应用的任务日期、线路筛选生成“说明”“四支线任务”“司机评分”三张工作表。顶部搜索只筛选页面显示，不影响 Excel 范围。**默认不导出电话**；明确勾选“导出含电话”后才包含电话。配置管理员口令时，导出也需输入口令；口令不放入下载链接。

### 运行与旧约定导入

原监控进程继续使用 8766，原有收车、卸货、发车和操作合规预警保留。8768 共用 `dev/dev.db`，提供司机看板、登记、评分、导出及司机资料只读同步；不启动第二套运输节点轮询、预警引擎或飞书发送。若入口未运行，在此目录双击 `司机实际到场.command`，或执行：

```bash
python3 -m bwi_monitor.driver_dashboard --database dev/dev.db --port 8768
```

沿用 `BWI_ADMIN_TOKEN` 和 `BWI_DASHBOARD_READONLY` 设置，独立入口只监听本机。只读模式可查看和导出，禁止编辑、评分调整及司机同步。离线验证可使用 `--no-driver-sync` 禁止读取 TMS。新增资料和评分记录保存在独立表中，登记及评分不会改写 TMS 原始字段或预警规则。

首次导入旧约定前备份数据库，先检查，再导入：

```bash
python3 tools/import_driver_appointments.py --target dev/dev.db --dry-run
python3 tools/import_driver_appointments.py --target dev/dev.db
```

导入默认优先读取已安装旧调度的数据库，只读来源，仅导入已确认 MT 的约定；不复制司机个人信息、旧实际到场或旧评分，不覆盖已有约定。新任务之后若在旧系统有确认约定，可重复导入。旧系统凌晨 03:00 前归前一营业日，导入时转为实际日历日期；不改变新看板原有的 05:00 营业日边界。

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
| `bwi_monitor/web.py`, `dashboard.html`, `dashboard_v2.html` | monitoring dashboard, driver-score UI and JSON API |
| `bwi_monitor/driver_arrivals.py`, `driver_scores.py` | independent on-site arrivals, task deductions and driver-score overrides |
| `bwi_monitor/driver_sync.py` | bounded read-only TMS driver-profile synchronization; missing fields stay pending |
| `bwi_monitor/driver_export.py`, `driver_dashboard.py` | scoped Excel export and local driver dashboard entry point |

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
