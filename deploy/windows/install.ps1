# Double-click Install-BWI-Monitor.bat. Updates preserve local data, credentials and .env.
param(
  [string]$InstallDir = "C:\BWI-Monitor",
  [ValidateRange(1,65535)][int]$Port = 8766,
  [string]$PythonExe = "",
  [switch]$NoStart,
  [switch]$SkipSystemSetup
)
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if ($InstallDir.TrimEnd('\') -eq ([IO.Path]::GetFullPath($src)).TrimEnd('\')) { throw "Extract the package to a separate folder before installing." }
function Run-Python([string]$Exe, [string[]]$PythonArgs) {
  & $Exe @PythonArgs
  if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE)." }
}
function Find-Python {
  if ($PythonExe) { return $PythonExe }
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if ($launcher) {
    $found = & $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path $found)) { return $found }
  }
  foreach ($candidate in @("$env:ProgramFiles\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
    if (Test-Path $candidate) { return $candidate }
  }
  return $null
}
Write-Host "1/6 Checking Python ..."
$python = Find-Python
if (-not $python) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Install Python 3.12 from python.org, then run this installer again." }
  & winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
  if ($LASTEXITCODE -ne 0) { throw "Python installation failed. Install Python 3.12 from python.org and retry." }
  $python = Find-Python
  if (-not $python) { throw "Python was not found after installation. Reopen this installer." }
}
Run-Python $python @('-c', 'import sys; assert sys.version_info >= (3,9)')
Write-Host "2/6 Preparing dependencies ..."
New-Item -ItemType Directory -Force -Path $InstallDir, "$InstallDir\data", "$InstallDir\secrets" | Out-Null
$venvPython = "$InstallDir\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Run-Python $python @('-m', 'venv', "$InstallDir\venv") }
Run-Python $venvPython @('-m', 'pip', 'install', '-r', "$src\requirements.txt")
# Prepare all program files before stopping the installed service.
$prepared = Join-Path $InstallDir ('.program-' + [guid]::NewGuid().ToString('N'))
Copy-Item -Recurse "$src\bwi_monitor" $prepared
Run-Python $venvPython @('-m', 'compileall', '-q', $prepared)
$backup = Join-Path $InstallDir ('backups\upgrade-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,6))
New-Item -ItemType Directory -Path $backup | Out-Null
$existing = $null
if (-not $SkipSystemSetup) {
  $existing = Get-ScheduledTask -TaskName 'BWI TMS Monitor' -ErrorAction SilentlyContinue
  if ($existing) { Stop-ScheduledTask -TaskName 'BWI TMS Monitor'; Start-Sleep -Seconds 2 }
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.ExecutablePath -eq $venvPython -and $_.CommandLine -match '-m\s+bwi_monitor(?:\.[A-Za-z_][A-Za-z_0-9]*)?(?:\s|$)'
  } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
$oldMoved = $false
try {
  Write-Host "3/6 Backing up local settings and database ..."
  $envPath = Join-Path $InstallDir '.env'
  if (Test-Path $envPath) { Copy-Item $envPath "$backup\.env" }
  if (Test-Path "$InstallDir\secrets") { Copy-Item -Recurse "$InstallDir\secrets" "$backup\secrets" }
  $dbPath = Join-Path $InstallDir 'data\bwi_monitor.db'
  if (Test-Path $envPath) {
    $dbLine = Get-Content $envPath | Where-Object { $_ -match '^\s*BWI_DB_PATH\s*=' } | Select-Object -First 1
    if ($dbLine) {
      $configured = ($dbLine -split '=',2)[1].Trim().Trim('"').Trim("'")
      $dbPath = if ([IO.Path]::IsPathRooted($configured)) { $configured } else { Join-Path $InstallDir $configured }
    }
    $portLine = Get-Content $envPath | Where-Object { $_ -match '^\s*BWI_WEB_PORT\s*=' } | Select-Object -First 1
    if ($portLine) { $Port = [int](($portLine -split '=',2)[1].Trim()) }
  }
  if (Test-Path $dbPath) {
    Run-Python $venvPython @('-c', 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()', $dbPath, "$backup\bwi_monitor.db")
  }
  foreach ($file in @('run_bwi.bat','requirements.txt','VERSION.txt')) {
    if (Test-Path "$InstallDir\$file") { Copy-Item "$InstallDir\$file" $backup }
  }
  Write-Host "4/6 Updating program; keeping operational data and credentials ..."
  if (Test-Path "$InstallDir\bwi_monitor") { Move-Item "$InstallDir\bwi_monitor" "$backup\bwi_monitor"; $oldMoved = $true }
  Move-Item $prepared "$InstallDir\bwi_monitor"
  Copy-Item -Force "$src\run_bwi.bat", "$src\requirements.txt", "$src\VERSION.txt", "$src\README-Windows.md" $InstallDir
  if (Test-Path "$src\tools") { Copy-Item -Recurse -Force "$src\tools" $InstallDir }
  if (-not (Test-Path $envPath)) {
    $settings = (Get-Content "$src\env.windows.example" -Raw).Replace('BWI_WEB_PORT=8766', "BWI_WEB_PORT=$Port")
    $settings = $settings.Replace('BWI_ADMIN_TOKEN=change-me', 'BWI_ADMIN_TOKEN=' + [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($envPath, $settings, (New-Object Text.UTF8Encoding($false)))
  }
  Write-Host "5/6 Configuring automatic startup ..."
  if (-not $SkipSystemSetup) {
    $action = New-ScheduledTaskAction -Execute "$InstallDir\run_bwi.bat" -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName 'BWI TMS Monitor' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    $network = (Get-Content $envPath | Where-Object { $_ -match '^\s*BWI_WEB_HOST\s*=\s*0\.0\.0\.0\s*$' })
    if ($network -and -not (Get-NetFirewallRule -DisplayName "BWI TMS Monitor $Port" -ErrorAction SilentlyContinue)) {
      New-NetFirewallRule -DisplayName "BWI TMS Monitor $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Domain,Private -RemoteAddress LocalSubnet | Out-Null
    }
  }
  Write-Host "6/6 Installation complete. Local backup: $backup"
  if (-not $NoStart -and -not $SkipSystemSetup) {
    $authPath = Join-Path $InstallDir 'secrets\gofo_auth'
    $authLine = Get-Content $envPath | Where-Object { $_ -match '^\s*GOFO_AUTH_FILE\s*=' } | Select-Object -First 1
    if ($authLine) {
      $configuredAuth = ($authLine -split '=',2)[1].Trim().Trim('"').Trim("'")
      $authPath = if ([IO.Path]::IsPathRooted($configuredAuth)) { $configuredAuth } else { Join-Path $InstallDir $configuredAuth }
    }
    if (Test-Path $authPath) {
      Start-ScheduledTask -TaskName 'BWI TMS Monitor'
      Write-Host "Opening dashboard. Initial TMS refresh may take a minute."
      Start-Process "http://127.0.0.1:$Port/dashboard-v2"
    } else {
      Write-Host "Add your local TMS token to $InstallDir\secrets\gofo_auth, then double-click $InstallDir\run_bwi.bat."
    }
  }
} catch {
  if ($oldMoved -and (Test-Path "$backup\bwi_monitor")) {
    if (Test-Path "$InstallDir\bwi_monitor") { Move-Item "$InstallDir\bwi_monitor" "$backup\failed-program" }
    Move-Item "$backup\bwi_monitor" "$InstallDir\bwi_monitor"
    foreach ($file in @('run_bwi.bat','requirements.txt','VERSION.txt')) {
      if (Test-Path "$backup\$file") { Copy-Item -Force "$backup\$file" "$InstallDir\$file" }
    }
    if ($existing -and -not $NoStart -and -not $SkipSystemSetup) { Start-ScheduledTask -TaskName 'BWI TMS Monitor' }
  }
  throw
}
