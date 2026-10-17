# BWI TMS 监控系统 · Windows 正式环境安装说明

## 准备
1. 安装 **Python 3.12**（python.org）：勾选 “Add python.exe to PATH”，并选 “Customize installation → Install Python for all users”（开机任务以系统账户运行，需要全员可用的 Python）。
2. 把安装包解压到任意位置，例如桌面 `BWI-Monitor-package`。
3. 在 `secrets` 文件夹放两个文本文件（只有一行，不要多余空格）：
   - `gofo_auth`：TMS 登录凭证（`Bearer eyJ...`，从浏览器开发者工具复制）
   - `feishu_webhook`：飞书机器人 Webhook 地址
   安装包里如果已经带了这两个文件，就不用再放。

## 安装
1. 开始菜单搜索 PowerShell → 右键“以管理员身份运行”。
2. 进入解压目录，执行：
   ```
   Set-ExecutionPolicy -Scope Process Bypass
   .\install.ps1
   ```
3. 程序装到 `C:\BWI-Monitor`，开机自动启动，出错 1 分钟后自动重启，电脑不会自动睡眠。
4. 本机打开 http://127.0.0.1:8766 ；仓库其他电脑打开安装结束时显示的地址（http://这台电脑IP:8766）。

## 第一天试运行
- 默认 **飞书测试模式**（只记录，不发送）。确认监控台数据正常后，把 `C:\BWI-Monitor\.env` 里
  `FEISHU_TEST_MODE=true` 改成 `false`，然后重启任务：`Restart-ScheduledTask "BWI TMS Monitor"`（或重启电脑）。
- **同一时间只能有一台电脑正式发送飞书**：Windows 正式发送后，Mac 必须切到开发模式。
- 修改 `.env` 里的 `BWI_ADMIN_TOKEN`（上下班、关闭告警时需要输入）。

## 日常
- TMS 登录过期：监控台出现红色“数据可能过期”。更新 `C:\BWI-Monitor\secrets\gofo_auth` 即可，10 分钟内自动生效，不用重启。
- 日志：`C:\BWI-Monitor\data\service.log`；数据库：`C:\BWI-Monitor\data\bwi_monitor.db`（请定期备份）。
- 升级：用新安装包再运行一次 `install.ps1`（数据、密钥和设置都会保留）。
- 停用：`.\uninstall.ps1`（保留数据）。
