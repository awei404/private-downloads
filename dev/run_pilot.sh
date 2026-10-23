#!/bin/sh
# Mac live instance: Feishu live while a shift is open (Start Shift), Mac kept awake (lid must stay open), detached.
# Restarts the monitor if it ever exits (2026-10-23: a crash left the Mac without TMS data for hours).
cd "$(dirname "$0")/.." || exit 1
export BWI_DB_PATH=dev/dev.db BWI_LOG_DIR=dev/logs BWI_WEB_HOST=127.0.0.1 BWI_WEB_PORT=8766 BWI_DASHBOARD_FILE=dashboard_v2.html
export FEISHU_ENABLED=true FEISHU_TEST_MODE=false FEISHU_DEPARTURE_ENABLED=true
while true; do
  caffeinate -is python3 -u -m bwi_monitor >> dev/service.log 2>&1
  code=$?
  [ "$code" = 3 ] && { echo "$(date '+%F %T') another monitor holds port 8766; not restarting" >> dev/service.log; exit 3; }
  echo "$(date '+%F %T') monitor exited ($code); restarting in 30 s" >> dev/service.log
  sleep 30
done
