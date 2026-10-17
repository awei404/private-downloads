#!/bin/sh
# Build the Windows install package on the Mac:  deploy/make_windows_package.sh [--with-data] [--with-secrets]
#   --with-data     include a consistent copy of the current pilot database (history, shift, dedupe keys)
#   --with-secrets  include secrets/gofo_auth and secrets/feishu_webhook (keep the zip private!)
set -e
cd "$(dirname "$0")/.."
OUT="dist/BWI-Monitor-$(date +%Y%m%d-%H%M)"
rm -rf "$OUT"; mkdir -p "$OUT/secrets" "$OUT/data"
cp -R bwi_monitor "$OUT/"; find "$OUT" -name __pycache__ -type d -prune -exec rm -rf {} +
cp requirements.txt deploy/windows/install.ps1 deploy/windows/uninstall.ps1 deploy/windows/run_bwi.bat \
   deploy/windows/env.windows.example deploy/windows/README-Windows.md "$OUT/"
for arg in "$@"; do
  case "$arg" in
    --with-data) python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d)" \
                   dev/dev.db "$OUT/data/bwi_monitor.db" ;;
    --with-secrets) [ -n "$GOFO_AUTH" ] && printf '%s' "$GOFO_AUTH" > "$OUT/secrets/gofo_auth"
                    cp secrets/feishu_webhook "$OUT/secrets/" ;;
  esac
done
(cd dist && zip -qr "$(basename "$OUT").zip" "$(basename "$OUT")")
echo "Package: $OUT.zip"
