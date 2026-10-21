param([string]$PackageDir, [string]$PythonExe)
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:TEMP ('BWI Monitor smoke ' + [guid]::NewGuid().ToString('N'))
& "$PackageDir\install.ps1" -InstallDir $root -PythonExe $PythonExe -NoStart -SkipSystemSetup -NoPrompt
if (-not (Test-Path "$root\bwi_monitor\driver_print.py")) { throw 'Print feature not installed' }
$envPath = Join-Path $root '.env'
Add-Content $envPath "`n# smoke-test preserved configuration"
$envBefore = (Get-FileHash $envPath -Algorithm SHA256).Hash
Set-Content "$root\secrets\gofo_auth" 'synthetic-test-token-not-valid'
$secretBefore = (Get-FileHash "$root\secrets\gofo_auth" -Algorithm SHA256).Hash
$db = Join-Path $root 'data\bwi_monitor.db'
@'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("CREATE TABLE preservation(value INTEGER)")
    connection.execute("INSERT INTO preservation VALUES(42)")
'@ | Set-Content -Encoding UTF8 "$root\create_test_db.py"
& $PythonExe "$root\create_test_db.py" $db
if ($LASTEXITCODE -ne 0) { throw 'Could not create test database' }
$dbBefore = (Get-FileHash $db -Algorithm SHA256).Hash
Set-Content "$root\bwi_monitor\obsolete_test_marker.txt" 'old program file'
& "$PackageDir\install.ps1" -InstallDir $root -PythonExe $PythonExe -NoStart -SkipSystemSetup -NoPrompt
if ((Get-FileHash $envPath -Algorithm SHA256).Hash -ne $envBefore) { throw 'Settings changed on upgrade' }
if ((Get-FileHash "$root\secrets\gofo_auth" -Algorithm SHA256).Hash -ne $secretBefore) { throw 'Credentials changed on upgrade' }
if ((Get-FileHash $db -Algorithm SHA256).Hash -ne $dbBefore) { throw 'Operational database changed on upgrade' }
if (Test-Path "$root\bwi_monitor\obsolete_test_marker.txt") { throw 'Old program directory was not replaced' }
if (-not (Get-ChildItem "$root\backups" -Filter bwi_monitor.db -Recurse)) { throw 'Database backup missing' }
if (-not (Get-ChildItem "$root\backups" -Filter obsolete_test_marker.txt -Recurse)) { throw 'Program backup missing' }
Write-Host 'PASS: Windows install + upgrade retain data, settings and credentials, with local backups.'

# ---- the installed program really runs: start it as the scheduled task would and open the dashboard ----
$py = Join-Path $root 'venv\Scripts\python.exe'
$admin = ((Get-Content $envPath | Where-Object { $_ -match '^\s*BWI_ADMIN_TOKEN\s*=' } | Select-Object -First 1) -split '=',2)[1].Trim()
if ($admin -notmatch '^\d{6}$') { throw "Admin password is not a 6-digit number: $admin" }
$out = Join-Path $root 'smoke-out.log'; $err = Join-Path $root 'smoke-err.log'
$proc = Start-Process -FilePath $py -ArgumentList '-u', '-m', 'bwi_monitor.windows_service' -WorkingDirectory $root `
  -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -WindowStyle Hidden
try {
  $up = $false
  foreach ($i in 1..60) {
    Start-Sleep -Seconds 2
    try { if ((Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8766/healthz' -TimeoutSec 5).StatusCode -eq 200) { $up = $true; break } } catch {}
    if ($proc.HasExited) { break }
  }
  if (-not $up) { Get-Content $out, $err -ErrorAction SilentlyContinue | Write-Host; throw 'Monitor did not start' }
  $page = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8766/' -TimeoutSec 20
  if ($page.Content -notmatch 'BWI TMS MONITOR' -or $page.Content -notmatch 'updateToken') { throw 'Dashboard page incomplete' }
  $state = Invoke-RestMethod 'http://127.0.0.1:8766/api/state' -TimeoutSec 60
  if ($null -eq $state.receiving -or $null -eq $state.health) { throw 'Dashboard state incomplete' }
  # a fake token is refused (400 from TMS, or 502 if TMS is unreachable from CI) and never saved
  $code = 0
  try {
    Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8766/api/tms-token' -Method Post -ContentType 'application/json' `
      -Headers @{ 'X-Admin-Token' = $admin } -Body '{"token":"Bearer synthetic-test-token-not-valid-0123456789"}' -TimeoutSec 120 | Out-Null
    $code = 200
  } catch { $code = [int]$_.Exception.Response.StatusCode }
  if ($code -ne 400 -and $code -ne 502) { throw "Fake token not refused (HTTP $code)" }
  if ((Get-FileHash "$root\secrets\gofo_auth" -Algorithm SHA256).Hash -ne $secretBefore) { throw 'Refused token overwrote the saved one' }
  if ($proc.HasExited) { Get-Content $out, $err | Write-Host; throw 'Monitor stopped' }
  if (Select-String -Path $err -Pattern 'Traceback' -Quiet) { Get-Content $err | Write-Host; throw 'Python error while running' }
  Write-Host "PASS: installed monitor runs on Windows (dashboard, state, token check HTTP $code)."
  # a second copy on the same port must refuse to start (no two pollers / Feishu senders, no random answers)
  $pid1 = (Invoke-RestMethod 'http://127.0.0.1:8766/healthz' -TimeoutSec 30).pid
  $dup = Start-Process -FilePath $py -ArgumentList '-u', '-m', 'bwi_monitor.windows_service' -WorkingDirectory $root `
    -RedirectStandardOutput "$root\dup-out.log" -RedirectStandardError "$root\dup-err.log" -PassThru -WindowStyle Hidden
  if (-not $dup.WaitForExit(60000)) { Stop-Process -Id $dup.Id -Force; throw 'Second copy kept running on the same port' }
  if ($dup.ExitCode -ne 3) { Get-Content "$root\dup-err.log" | Write-Host; throw "Second copy exit code $($dup.ExitCode), expected 3" }
  foreach ($i in 1..10) {
    $pidNow = (Invoke-RestMethod 'http://127.0.0.1:8766/healthz' -TimeoutSec 30).pid
    if ($pidNow -ne $pid1) { throw "Requests answered by another process ($pidNow, expected $pid1)" }
  }
  Write-Host "PASS: a second copy refuses the port; every request answered by process $pid1."
} finally {
  if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
}
