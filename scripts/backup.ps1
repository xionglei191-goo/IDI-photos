param([string]$DataDir = '')
$ErrorActionPreference = 'Stop'
$idiRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $DataDir) { $DataDir = Join-Path $idiRoot 'data' }
Push-Location -LiteralPath $idiRoot
try {
    & (Join-Path $idiRoot '.venv\Scripts\python.exe') -X utf8 -m idi.cli backup --data-dir $DataDir
    if ($LASTEXITCODE -ne 0) { throw 'Backup failed.' }
} finally { Pop-Location }
