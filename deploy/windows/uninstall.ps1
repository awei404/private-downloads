# Removes the scheduled task and firewall rule. Keeps C:\BWI-Monitor (data, secrets, .env) unless -RemoveFiles.
param([string]$InstallDir = "C:\BWI-Monitor", [int]$Port = 8766, [switch]$RemoveFiles)
$venvPython = Join-Path ([IO.Path]::GetFullPath($InstallDir)) "venv\Scripts\python.exe"
Stop-ScheduledTask -TaskName "BWI TMS Monitor" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -eq $venvPython -and $_.CommandLine -match '-m\s+bwi_monitor(?:\.[A-Za-z_][A-Za-z_0-9]*)?(?:\s|$)' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Unregister-ScheduledTask -TaskName "BWI TMS Monitor" -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName "BWI TMS Monitor $Port" -ErrorAction SilentlyContinue
if ($RemoveFiles) { Remove-Item -Recurse -Force $InstallDir }
Write-Host "Removed."
