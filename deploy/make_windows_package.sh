#!/bin/sh
# Code-only Windows package. Real data and credentials are never packaged.
set -eu
cd "$(dirname "$0")/.."
exec python3 deploy/build_windows_package.py "$@"
