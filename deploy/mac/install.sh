#!/bin/bash
# BWI TMS monitor - Mac installer. Double-click Install-BWI-Monitor.command (or: bash install.sh).
# Installs to ~/BWI-Monitor, starts at login and restarts on failure (LaunchAgent), keeps the Mac awake while running.
# Re-running upgrades the program; the database, secrets and .env are kept (old program backed up to backups/).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="${BWI_INSTALL_DIR:-$HOME/BWI-Monitor}"
LABEL="${BWI_LABEL:-com.bwi.monitor}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
NO_PROMPT="${BWI_NO_PROMPT:-}"   # tests: never show dialogs
NO_AGENT="${BWI_NO_AGENT:-}"     # tests: do not register the LaunchAgent / start
UID_N="$(id -u)"

ask() {  # ask TITLE MESSAGE -> pasted text ("" when skipped)
  [ -n "$NO_PROMPT" ] && return 0
  osascript -e 'on run argv' -e 'try' \
    -e 'return text returned of (display dialog (item 2 of argv) with title (item 1 of argv) default answer "" buttons {"跳过", "确定"} default button "确定" cancel button "跳过")' \
    -e 'end try' -e 'return ""' -e 'end run' "$1" "$2" 2>/dev/null || true
}
alert() { [ -n "$NO_PROMPT" ] || osascript -e 'on run argv' -e 'display dialog (item 1 of argv) with title "BWI Monitor" buttons {"好"} default button "好"' -e 'end run' "$1" >/dev/null 2>&1 || true; }

[ "$SRC" = "${DIR}" ] && { echo "Unzip the package to a different folder than ${DIR} first."; exit 1; }

echo "1/6 检查 Python ..."
PY=""
for p in "${BWI_PYTHON:-}" /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -n "$p" ] && [ -x "$p" ] || continue
  if "$p" -c 'import sys; assert sys.version_info >= (3, 9)' >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  alert "需要先安装 Python 3（免费）。将打开下载页面：下载 macOS 安装包，装完后再双击本安装程序。"
  [ -n "$NO_PROMPT" ] || open "https://www.python.org/downloads/macos/"
  exit 1
fi
"$PY" --version

echo "2/6 准备程序环境（第一次需要联网，约 1 分钟）..."
mkdir -p "${DIR}/data" "${DIR}/secrets" "${DIR}/backups" "$HOME/Library/LaunchAgents"
chmod 700 "${DIR}/secrets"
[ -x "${DIR}/venv/bin/python" ] || "$PY" -m venv "${DIR}/venv"
"${DIR}/venv/bin/python" -m pip install -q --disable-pip-version-check -r "$SRC/requirements.txt"

echo "3/6 停止旧版本并备份 ..."
launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1 || true
pkill -f "${DIR}/venv/bin/python -u -m bwi_monitor" >/dev/null 2>&1 || true
BACKUP="${DIR}/backups/upgrade-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
[ -f "${DIR}/.env" ] && cp "${DIR}/.env" "$BACKUP/.env"
if [ -f "${DIR}/data/bwi_monitor.db" ]; then
  "${DIR}/venv/bin/python" -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' \
    "${DIR}/data/bwi_monitor.db" "$BACKUP/bwi_monitor.db"
fi
MOVED=""
restore() { if [ -n "$MOVED" ] && [ -d "$BACKUP/bwi_monitor" ]; then rm -rf "${DIR}/bwi_monitor"; mv "$BACKUP/bwi_monitor" "${DIR}/bwi_monitor"; echo "安装失败，已恢复旧版本。"; fi; }
trap restore ERR

echo "4/6 更新程序（数据、密钥、设置保留）..."
if [ -d "${DIR}/bwi_monitor" ]; then mv "${DIR}/bwi_monitor" "$BACKUP/bwi_monitor"; MOVED=1; fi
cp -R "$SRC/bwi_monitor" "${DIR}/bwi_monitor"
cp "$SRC/requirements.txt" "$SRC/VERSION.txt" "$SRC/README-Mac.md" "${DIR}/"
[ -d "$SRC/tools" ] && cp -R "$SRC/tools" "${DIR}/"
"${DIR}/venv/bin/python" -m compileall -q "${DIR}/bwi_monitor" >/dev/null
if [ ! -f "${DIR}/.env" ]; then
  PORT=8766
  for p in 8766 8770 8771 8772 8773 8774 8775; do
    if ! lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then PORT=$p; break; fi
  done
  sed -e "s/^BWI_WEB_PORT=.*/BWI_WEB_PORT=${PORT}/" -e "s/^BWI_ADMIN_TOKEN=.*/BWI_ADMIN_TOKEN=$(( RANDOM % 900000 + 100000 ))/" \
    "$SRC/env.mac.example" > "${DIR}/.env"
fi
PORT="$(sed -n 's/^BWI_WEB_PORT=//p' "${DIR}/.env" | head -1)"; PORT="${PORT:-8766}"
ADMIN="$(sed -n 's/^BWI_ADMIN_TOKEN=//p' "${DIR}/.env" | head -1)"

echo "5/6 TMS 登录和飞书 ..."
for name in gofo_auth feishu_webhook; do
  if [ ! -s "${DIR}/secrets/$name" ] && [ -s "$SRC/secrets/$name" ]; then cp "$SRC/secrets/$name" "${DIR}/secrets/$name"; fi
done
if [ ! -s "${DIR}/secrets/feishu_webhook" ]; then
  HOOK="$(ask "BWI Monitor · 飞书" "粘贴飞书群机器人 Webhook 地址（https://open.feishu.cn/... 开头）。跳过 = 暂时不发飞书。")"
  [ -n "$HOOK" ] && printf '%s' "$HOOK" > "${DIR}/secrets/feishu_webhook"
fi
if [ ! -s "${DIR}/secrets/gofo_auth" ]; then
  TOKEN="$(ask "BWI Monitor · TMS 登录" "粘贴 TMS 登录 token（Authorization 值，Bearer 开头）。TMS 网页按 F12 → Network → 任意 pageList → Request Headers → Authorization。跳过 = 之后在监控台点红色按钮输入。")"
  if [ -n "$TOKEN" ]; then
    case "$TOKEN" in [Bb]earer\ *) ;; *) TOKEN="Bearer $TOKEN" ;; esac
    printf '%s' "$TOKEN" > "${DIR}/secrets/gofo_auth"
  fi
fi
chmod 600 "${DIR}/secrets/"* 2>/dev/null || true
trap - ERR

if [ -n "$NO_AGENT" ]; then echo "6/6 完成（测试模式：未启动）。"; exit 0; fi
echo "6/6 设置开机自动运行并启动 ..."
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>${DIR}/venv/bin/python</string><string>-u</string><string>-m</string><string>bwi_monitor.windows_service</string>
  </array>
  <key>WorkingDirectory</key><string>${DIR}</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUTF8</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>${DIR}/data/service.log</string>
  <key>StandardErrorPath</key><string>${DIR}/data/service.log</string>
</dict></plist>
PLISTEOF
launchctl bootstrap "gui/$UID_N" "$PLIST"
UP=""
for i in $(seq 1 45); do
  sleep 2
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then UP=1; break; fi
done
if [ -z "$UP" ]; then
  echo "监控没有启动，请查看 ${DIR}/data/service.log"; tail -20 "${DIR}/data/service.log" 2>/dev/null || true
  alert "监控没有启动。请把 ${DIR}/data/service.log 发给技术支持。"; exit 1
fi
echo
echo "✅ 安装完成：http://127.0.0.1:${PORT}"
echo "管理员口令（每个浏览器只问一次）：${ADMIN}"
[ -n "$NO_PROMPT" ] || open "http://127.0.0.1:${PORT}/"
alert "✅ 安装完成，监控台已打开：http://127.0.0.1:${PORT}

管理员口令：${ADMIN}（每个浏览器只需输入一次）

飞书默认是测试模式（只记录不发送）。需要正式发送时，点监控台顶部“飞书 …⇄”切换，然后点“上班”。
Mac 合上盖子会睡眠，监控会暂停：请保持盖子打开并接电源。"
