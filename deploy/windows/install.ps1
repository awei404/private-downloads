# Double-click Install-BWI-Monitor.bat. Updates preserve local data, credentials and .env.
param(
  [string]$InstallDir = "C:\BWI-Monitor",
  [ValidateRange(1,65535)][int]$Port = 8766,
  [string]$PythonExe = "",
  [switch]$NoStart,
  [switch]$SkipSystemSetup,
  [switch]$NoPrompt
)
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if ($InstallDir.TrimEnd('\') -eq ([IO.Path]::GetFullPath($src)).TrimEnd('\')) { throw "Extract the package to a separate folder before installing." }
function Run-Python([string]$Exe, [string[]]$PythonArgs) {
  & $Exe @PythonArgs
  if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE)." }
}
function Stop-BwiMonitor([string]$Dir, [int]$DefaultPort) {
  # Stop every BWI monitor on this computer: the venv python.exe is only a launcher, the process doing the work is
  # its child (base Python), so match on the command line, and also stop whatever still holds the dashboard port.
  $p = $DefaultPort
  $envFile = Join-Path $Dir '.env'
  if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*BWI_WEB_PORT\s*=' } | Select-Object -First 1
    if ($line) { $p = [int](($line -split '=',2)[1].Trim()) }
  }
  foreach ($i in 1..3) {
    Get-CimInstance Win32_Process | Where-Object {
      $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match '-m\s+bwi_monitor(?:\.[A-Za-z_][A-Za-z_0-9]*)?(?:\s|"|$)'
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
      $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue
      if ($owner -and $owner.Name -match '^pythonw?\.exe$') { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Seconds 2
    if (-not (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)) { return }
  }
  Write-Warning "Port $p is still in use by another program. Close it, or change BWI_WEB_PORT in $envFile."
}
function Ask-Text([string]$Title, [string]$Message) {
  # A simple paste window (no length limit). Returns "" when skipped or when no one can answer (tests, services).
  if ($NoPrompt -or -not [Environment]::UserInteractive) { return "" }
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  $form = New-Object Windows.Forms.Form -Property @{ Text = $Title; Width = 640; Height = 260; StartPosition = 'CenterScreen'; TopMost = $true; FormBorderStyle = 'FixedDialog'; MaximizeBox = $false; MinimizeBox = $false }
  $label = New-Object Windows.Forms.Label -Property @{ Text = $Message; Left = 12; Top = 10; Width = 600; Height = 90 }
  $box = New-Object Windows.Forms.TextBox -Property @{ Left = 12; Top = 105; Width = 600; MaxLength = 100000 }
  $ok = New-Object Windows.Forms.Button -Property @{ Text = 'OK'; Left = 432; Top = 150; Width = 85; DialogResult = 'OK' }
  $skip = New-Object Windows.Forms.Button -Property @{ Text = 'Skip'; Left = 527; Top = 150; Width = 85; DialogResult = 'Cancel' }
  $form.Controls.AddRange(@($label, $box, $ok, $skip)); $form.AcceptButton = $ok; $form.CancelButton = $skip
  if ($form.ShowDialog() -eq 'OK') { return $box.Text.Trim() }
  return ""
}
function Save-Text([string]$Path, [string]$Text) {
  [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))   # no BOM
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
  Stop-BwiMonitor $InstallDir $Port
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
    $settings = $settings.Replace('BWI_ADMIN_TOKEN=change-me', 'BWI_ADMIN_TOKEN=' + (Get-Random -Minimum 100000 -Maximum 999999))
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
  $tokenLine = Get-Content $envPath | Where-Object { $_ -match '^\s*BWI_ADMIN_TOKEN\s*=' } | Select-Object -First 1
  if ($tokenLine) {
    Write-Host ""
    Write-Host ("Dashboard admin password (asked once per browser): " + ($tokenLine -split '=',2)[1].Trim()) -ForegroundColor Yellow
    Write-Host "Write it down. It is also in $envPath (BWI_ADMIN_TOKEN)."
  }
  # Credentials: taken from the package when it carries them, otherwise asked in a paste window. Existing ones are kept.
  foreach ($name in @('gofo_auth', 'feishu_webhook')) {
    if (-not (Test-Path "$InstallDir\secrets\$name") -and (Test-Path "$src\secrets\$name")) {
      Copy-Item "$src\secrets\$name" "$InstallDir\secrets\$name"
    }
  }
  if (-not (Test-Path "$InstallDir\secrets\feishu_webhook")) {
    $hook = Ask-Text 'BWI Monitor - Feishu' "Paste the Feishu group bot webhook (starts with https://open.feishu.cn/...).`nSkip = no Feishu messages for now."
    if ($hook) { Save-Text "$InstallDir\secrets\feishu_webhook" $hook }
  }
  if (-not (Test-Path "$InstallDir\secrets\gofo_auth")) {
    $token = Ask-Text 'BWI Monitor - TMS login' "Paste the TMS login token (Authorization value, starts with Bearer).`nTMS website: F12 > Network > any pageList request > Request Headers > Authorization.`nSkip = enter it later on the dashboard (red button)."
    if ($token) {
      if ($token -notmatch '^(?i)bearer ') { $token = 'Bearer ' + $token }
      Save-Text "$InstallDir\secrets\gofo_auth" $token
    }
  }
  if (-not $NoStart -and -not $SkipSystemSetup) {
    # Always start: without a valid TMS login the dashboard shows a red button to paste one.
    Start-ScheduledTask -TaskName 'BWI TMS Monitor'
    Write-Host "Opening dashboard. The first TMS refresh takes about a minute."
    Start-Sleep -Seconds 8
    Start-Process "http://127.0.0.1:$Port/"
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
