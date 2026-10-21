param(
    [ValidateSet('start', 'stop', 'status', 'logs')]
    [string]$Action = 'start',
    [switch]$OpenBrowser
)
$ErrorActionPreference = 'Stop'
$idiRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$idiCompose = @(
    '--context', 'desktop-linux', 'compose',
    '--project-name', 'idi-dev',
    '--project-directory', $idiRoot,
    '--file', (Join-Path $idiRoot 'compose.dev.yaml')
)
$idiDevUrl = 'http://127.0.0.1:18767/'

switch ($Action) {
    'start' {
        & docker @idiCompose up -d --build --wait --wait-timeout 90 idi
        if ($LASTEXITCODE -ne 0) { throw 'IDI development startup failed. Check Docker Desktop and port 18767.' }
        $idiHealth = Invoke-RestMethod ($idiDevUrl + 'api/health') -TimeoutSec 10
        if ($idiHealth.application -ne 'IDI') { throw 'The development port is not serving IDI.' }
        Write-Output "IDI development only: $idiDevUrl"
        Write-Output 'Separate test data and key volumes; production website writes disabled.'
        if ($OpenBrowser) { Start-Process -FilePath $idiDevUrl }
    }
    'stop' {
        & docker @idiCompose stop idi
        if ($LASTEXITCODE -ne 0) { throw 'IDI development stop failed.' }
        Write-Output 'Development container stopped; test data and key volumes retained.'
    }
    'status' {
        & docker @idiCompose ps --all
        if ($LASTEXITCODE -ne 0) { throw 'Could not read IDI development status.' }
    }
    'logs' {
        & docker @idiCompose logs --tail 100 idi
        if ($LASTEXITCODE -ne 0) { throw 'Could not read IDI development logs.' }
    }
}
