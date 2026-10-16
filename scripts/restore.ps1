param([Parameter(Mandatory=$true)][string]$Archive, [Parameter(Mandatory=$true)][string]$Target)
$ErrorActionPreference = 'Stop'
$idiRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$idiArchive = (Resolve-Path -LiteralPath $Archive).Path
$idiTarget = [System.IO.Path]::GetFullPath($Target)
if ((Test-Path -LiteralPath $idiTarget) -and (Get-ChildItem -LiteralPath $idiTarget -Force | Select-Object -First 1)) { throw 'Restore target must be a new empty directory.' }
Push-Location -LiteralPath $idiRoot
try {
    & (Join-Path $idiRoot '.venv\Scripts\python.exe') -X utf8 -m idi.cli restore $idiArchive --target $idiTarget
    if ($LASTEXITCODE -ne 0) { throw 'Restore verification failed.' }
    Write-Output "Restore complete. Start with scripts/start.ps1 -DataDir '$idiTarget'"
} finally { Pop-Location }
