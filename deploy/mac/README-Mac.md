# BWI Monitor · Mac 安装（三步）

1. 下载 `BWI-Monitor-Mac.zip`，双击解压。
2. 双击 **Install-BWI-Monitor.command**。
   - 如果提示“无法打开 / 无法验证开发者”：打开 **系统设置 → 隐私与安全性**，在下方点 **“仍要打开”**，再双击一次。
   - 或者打开“终端”，输入 `bash ` 后把 `install.sh` 拖进窗口，回车。
   - 没有 Python 会提示并打开下载页，装好后再双击一次。
3. 弹出两个粘贴窗口：**飞书 Webhook** 和 **TMS 登录 token**（Bearer 开头）。跳过也可以，之后在监控台红色按钮输入。

装完自动打开监控台，并弹窗显示 **管理员口令（6 位数字）**，每个浏览器只需输入一次。程序装在 `~/BWI-Monitor`，登录 Mac 后自动运行，出错自动重启，运行时 Mac 不会自动睡眠。

**注意：合上 MacBook 盖子会睡眠，监控暂停。** 请保持盖子打开、接电源。

**升级**：下载新的 ZIP，同样双击安装，直接覆盖；数据库、token、webhook 和设置保留（旧版本备份在 `~/BWI-Monitor/backups`）。
**停用**：双击 `Uninstall-BWI-Monitor.command`（数据保留）。

## TMS 登录过期

监控台顶部变红“TMS 登录失效”，飞书收到“⛔ BWI 监控数据中断”。用浏览器登录 TMS → F12 → Network → 任意 `pageList` → Request Headers → 复制 `Authorization`（Bearer 开头）→ 在监控台点 **“更新 TMS 登录”** 粘贴。验证有效才保存，马上重新拉取，不用重启。

## 飞书

默认测试模式（只记录不发送）。正式发送：点监控台顶部“飞书 …⇄”切换，再点“上班 Start Shift”。**同一时间只能有一台电脑正式发送**（Mac 和 Windows 不要同时开）。

## 日常

- 日志：`~/BWI-Monitor/data/service.log`；数据库：`~/BWI-Monitor/data/bwi_monitor.db`。
- 改了 `~/BWI-Monitor/.env` 后，再双击一次安装程序即可生效。
- 其他电脑访问监控台：把 `.env` 里 `BWI_WEB_HOST` 改成 `0.0.0.0`，再双击安装；用 `http://这台Mac的IP:端口` 打开。
