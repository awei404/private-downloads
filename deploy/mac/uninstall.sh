#!/bin/bash
# Stops the monitor and removes the LaunchAgent. Keeps ~/BWI-Monitor (database, secrets, settings).
DIR="${BWI_INSTALL_DIR:-$HOME/BWI-Monitor}"; LABEL="${BWI_LABEL:-com.bwi.monitor}"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -f "$DIR/venv/bin/python -u -m bwi_monitor" >/dev/null 2>&1 || true
echo "已停止并移除开机启动。数据保留在 $DIR"
