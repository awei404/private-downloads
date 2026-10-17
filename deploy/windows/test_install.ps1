param([string]$PackageDir, [string]$PythonExe)
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:TEMP ('BWI Monitor smoke ' + [guid]::NewGuid().ToString('N'))
& "$PackageDir\install.ps1" -InstallDir $root -PythonExe $PythonExe -NoStart -SkipSystemSetup
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
& "$PackageDir\install.ps1" -InstallDir $root -PythonExe $PythonExe -NoStart -SkipSystemSetup
if ((Get-FileHash $envPath -Algorithm SHA256).Hash -ne $envBefore) { throw 'Settings changed on upgrade' }
if ((Get-FileHash "$root\secrets\gofo_auth" -Algorithm SHA256).Hash -ne $secretBefore) { throw 'Credentials changed on upgrade' }
if ((Get-FileHash $db -Algorithm SHA256).Hash -ne $dbBefore) { throw 'Operational database changed on upgrade' }
if (Test-Path "$root\bwi_monitor\obsolete_test_marker.txt") { throw 'Old program directory was not replaced' }
if (-not (Get-ChildItem "$root\backups" -Filter bwi_monitor.db -Recurse)) { throw 'Database backup missing' }
if (-not (Get-ChildItem "$root\backups" -Filter obsolete_test_marker.txt -Recurse)) { throw 'Program backup missing' }
Write-Host 'PASS: Windows install + upgrade retain data, settings and credentials, with local backups.'
