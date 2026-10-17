# BWI TMS 监控系统 · Windows 正式环境安装说明

## 准备
1. Python 会自动安装。若自动安装失败，再手动装 **Python 3.12**（python.org）：勾选 “Add python.exe to PATH”，并选 “Customize installation → Install Python for all users”。
2. 把安装包解压到任意位置，例如桌面 `BWI-Monitor-package`。
3. 在 `secrets` 文件夹放两个文本文件（只有一行，不要多余空格）：
   - `gofo_auth`：TMS 登录凭证（`Bearer eyJ...`，从浏览器开发者工具复制）
   - `feishu_webhook`：飞书机器人 Webhook 地址
   安装包里如果已经带了这两个文件，就不用再放。

## 安装（双击即可）
1. 解压安装包。
2. 双击 **Install-BWI-Monitor.bat**，弹出“是否允许更改”时点“是”。
   - 没装 Python 会自动安装 Python 3.12（需要几分钟）。
   - 装完自动打开监控台。窗口最后显示的 http://这台电脑IP:8766 给仓库其他电脑用。
3. 程序装到 `C:\BWI-Monitor`，开机自动启动，出错 1 分钟后自动重启，电脑不会自动睡眠。
4. 升级：解压新安装包，再双击一次 Install-BWI-Monitor.bat（数据、密钥、设置都会保留）。
   停用：双击 Uninstall-BWI-Monitor.bat（保留数据）。

## 第一天试运行
- 默认 **飞书测试模式**（只记录，不发送）。确认监控台数据正常后，把 `C:\BWI-Monitor\.env` 里
  `FEISHU_TEST_MODE=true` 改成 `false`，然后重启任务：`Restart-ScheduledTask "BWI TMS Monitor"`（或重启电脑）。
- **同一时间只能有一台电脑正式发送飞书**：Windows 正式发送后，Mac 必须切到开发模式。
- 修改 `.env` 里的 `BWI_ADMIN_TOKEN`（上下班、关闭告警时需要输入）。

## 日常
- TMS 登录过期：监控台出现红色“数据可能过期”。更新 `C:\BWI-Monitor\secrets\gofo_auth` 即可，10 分钟内自动生效，不用重启。
- 日志：`C:\BWI-Monitor\data\service.log`；数据库：`C:\BWI-Monitor\data\bwi_monitor.db`（请定期备份）。
