#!/bin/bash
# CI smoke test (macOS): install + upgrade keep data/settings/credentials, then the installed monitor really runs.
# Usage: deploy/mac/test_install.sh <unzipped BWI-Monitor-Mac folder>
set -euo pipefail
PKG="$1"; ROOT="$(mktemp -d)/BWI Monitor smoke"
export BWI_INSTALL_DIR="$ROOT" BWI_NO_PROMPT=1 BWI_NO_AGENT=1
/bin/bash "$PKG/install.sh"
[ -f "$ROOT/bwi_monitor/driver_print.py" ] || { echo "program not installed"; exit 1; }
echo "# preserved" >> "$ROOT/.env"; printf 'synthetic-test-token-not-valid' > "$ROOT/secrets/gofo_auth"
"$ROOT/venv/bin/python" -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("CREATE TABLE preservation(v)"); c.commit()' "$ROOT/data/bwi_monitor.db"
ENV_H=$(shasum "$ROOT/.env"); SEC_H=$(shasum "$ROOT/secrets/gofo_auth"); echo old > "$ROOT/bwi_monitor/obsolete_marker.txt"
/bin/bash "$PKG/install.sh"
[ "$ENV_H" = "$(shasum "$ROOT/.env")" ] || { echo "settings changed on upgrade"; exit 1; }
[ "$SEC_H" = "$(shasum "$ROOT/secrets/gofo_auth")" ] || { echo "credentials changed on upgrade"; exit 1; }
"$ROOT/venv/bin/python" -c 'import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute("SELECT * FROM preservation")' "$ROOT/data/bwi_monitor.db"
[ ! -f "$ROOT/bwi_monitor/obsolete_marker.txt" ] || { echo "old program not replaced"; exit 1; }
ls "$ROOT"/backups/*/bwi_monitor.db >/dev/null && ls "$ROOT"/backups/*/bwi_monitor/obsolete_marker.txt >/dev/null
echo "PASS: Mac install + upgrade retain data, settings and credentials, with local backups."

PORT=$(sed -n 's/^BWI_WEB_PORT=//p' "$ROOT/.env"); ADMIN=$(sed -n 's/^BWI_ADMIN_TOKEN=//p' "$ROOT/.env")
[[ "$ADMIN" =~ ^[0-9]{6}$ ]] || { echo "admin password is not 6 digits: $ADMIN"; exit 1; }
cd "$ROOT"; PYTHONUTF8=1 "$ROOT/venv/bin/python" -u -m bwi_monitor.windows_service > "$ROOT/smoke.log" 2>&1 &
PID=$!; trap 'kill $PID 2>/dev/null || true' EXIT
for i in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || { cat "$ROOT/smoke.log"; echo "monitor did not start"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/" | grep -q updateToken || { echo "dashboard page incomplete"; exit 1; }
curl -fsS --max-time 60 "http://127.0.0.1:$PORT/api/state" | "$ROOT/venv/bin/python" -c 'import json,sys; s=json.load(sys.stdin); assert "receiving" in s and "health" in s'
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 -X POST -H 'Content-Type: application/json' -H "X-Admin-Token: $ADMIN" \
  -d '{"token":"Bearer synthetic-test-token-not-valid-0123456789"}' "http://127.0.0.1:$PORT/api/tms-token")
[ "$CODE" = 400 ] || [ "$CODE" = 502 ] || { echo "fake token not refused (HTTP $CODE)"; exit 1; }
[ "$SEC_H" = "$(shasum "$ROOT/secrets/gofo_auth")" ] || { echo "refused token overwrote the saved one"; exit 1; }
kill -0 $PID || { cat "$ROOT/smoke.log"; echo "monitor stopped"; exit 1; }
! grep -q Traceback "$ROOT/smoke.log" || { cat "$ROOT/smoke.log"; echo "python error while running"; exit 1; }
echo "PASS: installed monitor runs on macOS (dashboard, state, token check HTTP $CODE)."
