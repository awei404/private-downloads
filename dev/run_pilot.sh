#!/bin/sh
# Pilot shift on the development instance: live Feishu (authorized per shift), Mac kept awake, detached.
cd "$(dirname "$0")/.." || exit 1
export BWI_DB_PATH=dev/dev.db BWI_LOG_DIR=dev/logs BWI_WEB_HOST=127.0.0.1 BWI_WEB_PORT=8766 BWI_DASHBOARD_FILE=dashboard_v2.html
export FEISHU_ENABLED=true FEISHU_TEST_MODE=false FEISHU_DEPARTURE_ENABLED=true
exec caffeinate -i python3 -u -m bwi_monitor >> dev/service.log 2>&1
