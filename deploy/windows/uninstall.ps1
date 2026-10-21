# Removes the scheduled task and firewall rule. Keeps C:\BWI-Monitor (data, secrets, .env) unless -RemoveFiles.
param([string]$InstallDir = "C:\BWI-Monitor", [int]$Port = 8766, [switch]$RemoveFiles)
$venvPython = Join-Path ([IO.Path]::GetFullPath($InstallDir)) "venv\Scripts\python.exe"
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
Stop-ScheduledTask -TaskName "BWI TMS Monitor" -ErrorAction SilentlyContinue
Stop-BwiMonitor $InstallDir $Port
Unregister-ScheduledTask -TaskName "BWI TMS Monitor" -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName "BWI TMS Monitor $Port" -ErrorAction SilentlyContinue
if ($RemoveFiles) { Remove-Item -Recurse -Force $InstallDir }
Write-Host "Removed."
