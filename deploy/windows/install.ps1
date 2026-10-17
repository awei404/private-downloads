# BWI TMS monitor - Windows installer. Double-click Install-BWI-Monitor.bat (or, as Administrator:
#   Set-ExecutionPolicy -Scope Process Bypass; .\install.ps1)
# Re-running is safe: it updates the program and keeps data, secrets and .env.
param([string]$InstallDir = "C:\BWI-Monitor", [int]$Port = 8766)
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
  foreach ($p in "$env:ProgramFiles\Python312\python.exe", "$env:ProgramFiles\Python313\python.exe") { if (Test-Path $p) { return $p } }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { return (& py -3 -c "import sys; print(sys.executable)") }
  return $null
}
Write-Host "1/6 Checking Python 3 ..."
$python = Find-Python
if (-not $python) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "Python 3 not found. Install Python 3.12 from python.org (Install for all users, Add to PATH), then run again."
  }
  Write-Host "   Installing Python 3.12 for all users (a few minutes) ..."
  winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
  $python = Find-Python
  if (-not $python) { throw "Python install did not finish. Install Python 3.12 from python.org, then run again." }
}
& $python --version

Write-Host "2/6 Copying program to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir, "$InstallDir\data", "$InstallDir\secrets" | Out-Null
$existing = Get-ScheduledTask -TaskName "BWI TMS Monitor" -ErrorAction SilentlyContinue
if ($existing) { Stop-ScheduledTask -TaskName "BWI TMS Monitor" -ErrorAction SilentlyContinue; Start-Sleep 3 }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*-m bwi_monitor*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
Copy-Item -Recurse -Force "$src\bwi_monitor" $InstallDir
Copy-Item -Force "$src\requirements.txt", "$src\run_bwi.bat" $InstallDir
if (-not (Test-Path "$InstallDir\.env")) { Copy-Item "$src\env.windows.example" "$InstallDir\.env" }
if ((Test-Path "$src\data\bwi_monitor.db") -and -not (Test-Path "$InstallDir\data\bwi_monitor.db")) {
  Copy-Item "$src\data\bwi_monitor.db" "$InstallDir\data\bwi_monitor.db"   # history from the Mac pilot
}

Write-Host "3/6 Creating Python environment and installing packages ..."
if (-not (Test-Path "$InstallDir\venv")) { & $python -m venv "$InstallDir\venv" }
& "$InstallDir\venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
& "$InstallDir\venv\Scripts\python.exe" -m pip install -r "$InstallDir\requirements.txt"

Write-Host "4/6 Start at boot and restart on failure (scheduled task 'BWI TMS Monitor') ..."
$action = New-ScheduledTaskAction -Execute "$InstallDir\run_bwi.bat" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "BWI TMS Monitor" -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Force | Out-Null

Write-Host "5/6 Opening port $Port for the warehouse network and disabling sleep ..."
if (-not (Get-NetFirewallRule -DisplayName "BWI TMS Monitor $Port" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "BWI TMS Monitor $Port" -Direction Inbound -Protocol TCP -LocalPort $Port `
    -Action Allow -Profile Domain,Private | Out-Null
}
powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0

Write-Host "6/6 Checking secrets and starting ..."
$missing = @("gofo_auth", "feishu_webhook") | Where-Object { -not (Test-Path "$InstallDir\secrets\$_") }
if ($missing) {
  Write-Warning "Missing in $InstallDir\secrets : $($missing -join ', '). Add them (see README-Windows.md), then run: Start-ScheduledTask 'BWI TMS Monitor'"
} else {
  Start-ScheduledTask -TaskName "BWI TMS Monitor"
  Start-Sleep 20
  try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/healthz" | Out-Null; Write-Host "Running: http://127.0.0.1:$Port"; Start-Process "http://127.0.0.1:$Port" }
  catch { Write-Warning "Not answering yet. Check $InstallDir\data\service.log" }
}
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } | Select-Object -First 1).IPAddress
Write-Host "Other warehouse computers: http://${ip}:$Port   (edit settings in $InstallDir\.env)"
