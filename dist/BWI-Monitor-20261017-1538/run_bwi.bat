@echo off
rem BWI TMS monitor - started by the "BWI TMS Monitor" scheduled task (install.ps1). Settings: .env in this folder.
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist data mkdir data
"%~dp0venv\Scripts\python.exe" -u -m bwi_monitor >> "%~dp0data\service.log" 2>&1
