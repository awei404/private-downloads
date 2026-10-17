@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist data mkdir data
"%~dp0venv\Scripts\python.exe" -u -m bwi_monitor.windows_service >> "%~dp0data\service.log" 2>&1
exit /b %errorlevel%
