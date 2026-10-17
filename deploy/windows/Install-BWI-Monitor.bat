@echo off
set "BWI_INSTALLER_PATH=%~f0"
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath $env:BWI_INSTALLER_PATH -Verb RunAs"
  exit /b
)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "BWI_INSTALL_EXIT=%errorlevel%"
echo.
if not "%BWI_INSTALL_EXIT%"=="0" echo Installation did not complete. Review the message above.
pause
exit /b %BWI_INSTALL_EXIT%
