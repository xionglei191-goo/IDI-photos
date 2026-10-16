param([int]$Port = 8766, [string]$DataDir = '', [switch]$OpenBrowser, [switch]$ReadOnly)
$ErrorActionPreference = 'Stop'
$idiRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$idiPython = Join-Path $idiRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $idiPython)) { throw 'Run scripts/setup.ps1 first.' }
if (-not (Test-Path -LiteralPath (Join-Path $idiRoot 'frontend\dist\index.html'))) { throw 'Build the frontend with scripts/setup.ps1 first.' }
if (-not $DataDir) { $DataDir = Join-Path $idiRoot 'data' }
$env:IDI_DATA_DIR = [System.IO.Path]::GetFullPath($DataDir)
$env:IDI_PORT = [string]$Port
$env:IDI_PLATFORM_WRITE = if ($ReadOnly) { '0' } else { '1' }
$idiUrl = "http://127.0.0.1:$Port"
try { $idiHealth = Invoke-RestMethod -Uri "$idiUrl/api/health" -TimeoutSec 2 } catch { $idiHealth = $null }
if ($idiHealth -and $idiHealth.application -eq 'IDI') {
    $idiSystem = Invoke-RestMethod -Uri "$idiUrl/api/system" -TimeoutSec 2
    if ([System.IO.Path]::GetFullPath($idiSystem.data_dir) -ne $env:IDI_DATA_DIR) { throw 'This port is serving a different IDI archive.' }
    $idiSession = Invoke-RestMethod -Uri "$idiUrl/api/platform/session" -TimeoutSec 2
    if ([bool]$idiSession.write_enabled -ne (-not $ReadOnly)) { throw 'The running service has a different submission mode. Stop it with stop.cmd, then start it again.' }
    Write-Output "IDI is already running: $idiUrl"
    if ($OpenBrowser) { Start-Process -FilePath $idiUrl }
    return
}
Write-Output "IDI workbench: $idiUrl"
Write-Output 'Keep this service console open. Press Ctrl+C to stop.'
Push-Location -LiteralPath $idiRoot
try {
    if ($OpenBrowser) { & $idiPython -X utf8 -m idi.server --port $Port --open-browser }
    else { & $idiPython -X utf8 -m idi.server --port $Port }
}
finally { Pop-Location }
