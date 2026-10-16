param([string]$Python = '', [switch]$ImportSample)
$ErrorActionPreference = 'Stop'
$idiRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ($ImportSample -and -not (Test-Path -LiteralPath (Join-Path $idiRoot 'scripts\seed_sample.py'))) {
    throw 'Private sample files are not included in this repository. Run setup without -ImportSample and import your own photos in the workbench.'
}
$idiVenvPython = Join-Path $idiRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $idiVenvPython)) {
    if ($Python) { & $Python -m venv (Join-Path $idiRoot '.venv') }
    else {
        $idiPyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if (-not $idiPyLauncher) { throw 'Install Python 3.11 or 3.12, or pass -Python with its executable path.' }
        & py.exe -3.12 -m venv (Join-Path $idiRoot '.venv')
        if ($LASTEXITCODE -ne 0) { & py.exe -3.11 -m venv (Join-Path $idiRoot '.venv') }
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $idiVenvPython)) { throw 'Python virtual environment creation failed.' }
}
Push-Location -LiteralPath $idiRoot
try {
    & $idiVenvPython -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    & $idiVenvPython -m pip install --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw 'IDI package installation failed.' }
    Push-Location -LiteralPath (Join-Path $idiRoot 'frontend')
    try {
        & npm.cmd ci --no-fund --no-audit
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
    if ($ImportSample) {
        & $idiVenvPython -X utf8 (Join-Path $PSScriptRoot 'seed_sample.py') --ocr
        if ($LASTEXITCODE -ne 0) { throw 'Sample import verification failed.' }
    }
    Write-Output 'Setup complete. Run scripts/start.ps1.'
} finally { Pop-Location }
