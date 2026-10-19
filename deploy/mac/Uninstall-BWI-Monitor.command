#!/bin/bash
# Double-click in Finder.
cd "$(dirname "$0")" || exit 1
/bin/bash ./uninstall.sh
echo
read -n 1 -s -r -p "按任意键关闭此窗口 / Press any key to close"
