#!/bin/sh
# Mac development mode: own database, Feishu OFF, port 8768. Safe to run while Windows is the live production.
cd "$(dirname "$0")/.."
export BWI_ENV_FILE=/dev/null BWI_DB_PATH=dev/devmode.db BWI_LOG_DIR=dev/devmode_logs BWI_WEB_HOST=127.0.0.1 \
       BWI_WEB_PORT=8768 BWI_DASHBOARD_FILE=dashboard_v2.html FEISHU_ENABLED=false FEISHU_TEST_MODE=true
exec python3 -u -m bwi_monitor "$@"
