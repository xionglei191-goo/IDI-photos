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
$idiNetworkJson = & $idiPython -X utf8 -c 'import json; from idi.config import Settings; s=Settings.from_env(); print(json.dumps(dict(local_only=s.local_only,listen_host=s.host,lan_urls=s.lan_urls,lan_subnets=list(s.lan_subnets) if not s.local_only else [],public_urls=list(s.public_origins),trusted_proxies=list(s.trusted_proxies))))'
if ($LASTEXITCODE -ne 0) { throw 'Invalid network configuration. Check network.json in the archive directory.' }
$idiNetwork = $idiNetworkJson | ConvertFrom-Json
$idiLocalAddress = if ($idiNetwork.listen_host -eq '0.0.0.0') { '127.0.0.1' } else { $idiNetwork.listen_host }
$idiUrl = "http://${idiLocalAddress}:$Port"
try { $idiHealth = Invoke-RestMethod -Uri "$idiUrl/api/health" -TimeoutSec 2 } catch { $idiHealth = $null }
if ($idiHealth -and $idiHealth.application -eq 'IDI') {
    $idiSystem = Invoke-RestMethod -Uri "$idiUrl/api/system" -TimeoutSec 2
    if ([System.IO.Path]::GetFullPath($idiSystem.data_dir) -ne $env:IDI_DATA_DIR) { throw 'This port is serving a different IDI archive.' }
    if ([bool]$idiSystem.local_only -ne [bool]$idiNetwork.local_only -or (($idiSystem.lan_urls | Sort-Object) -join ',') -ne (($idiNetwork.lan_urls | Sort-Object) -join ',') -or (($idiSystem.lan_subnets | Sort-Object) -join ',') -ne (($idiNetwork.lan_subnets | Sort-Object) -join ',')) { throw 'Network configuration changed. Stop the service with stop.cmd, then start it again.' }
    if ($idiSystem.listen_host -and $idiSystem.listen_host -ne $idiNetwork.listen_host) { throw 'Listen address changed. Stop the service with stop.cmd, then start it again.' }
    foreach ($idiNetworkKey in @('public_urls','trusted_proxies')) { if ((($idiSystem.$idiNetworkKey | Sort-Object) -join ',') -ne (($idiNetwork.$idiNetworkKey | Sort-Object) -join ',')) { throw 'Tunnel configuration changed. Stop the service with stop.cmd, then start it again.' } }
    $idiSession = Invoke-RestMethod -Uri "$idiUrl/api/platform/session" -TimeoutSec 2
    if ([bool]$idiSession.write_enabled -ne (-not $ReadOnly)) { throw 'The running service has a different submission mode. Stop it with stop.cmd, then start it again.' }
    Write-Output "IDI is already running: $idiUrl"
    foreach ($idiLanUrl in $idiNetwork.lan_urls) { Write-Output "LAN workbench: $idiLanUrl" }
    foreach ($idiPublicUrl in $idiNetwork.public_urls) { Write-Output "Tunnel workbench: $idiPublicUrl" }
    if ($OpenBrowser) { Start-Process -FilePath $idiUrl }
    return
}
Write-Output "IDI workbench: $idiUrl"
foreach ($idiLanUrl in $idiNetwork.lan_urls) { Write-Output "LAN workbench: $idiLanUrl" }
foreach ($idiPublicUrl in $idiNetwork.public_urls) { Write-Output "Tunnel workbench: $idiPublicUrl" }
Write-Output 'Keep this service console open. Press Ctrl+C to stop.'
Push-Location -LiteralPath $idiRoot
try {
    if ($OpenBrowser) { & $idiPython -X utf8 -m idi.server --port $Port --open-browser }
    else { & $idiPython -X utf8 -m idi.server --port $Port }
}
finally { Pop-Location }
