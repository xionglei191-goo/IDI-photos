param([string]$DataDir = '')
$ErrorActionPreference = 'Stop'
$idiRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $DataDir) { $DataDir = Join-Path $idiRoot 'data' }
$idiData = [System.IO.Path]::GetFullPath($DataDir)
$idiPidFile = Join-Path $idiData 'service.json'
if (-not (Test-Path -LiteralPath $idiPidFile)) { Write-Output 'No managed IDI service record.'; return }
$idiRecord = Get-Content -LiteralPath $idiPidFile -Raw | ConvertFrom-Json
$idiProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$idiRecord.pid)" -ErrorAction SilentlyContinue
if ($idiProcess) {
    $idiExpectedPython = [System.IO.Path]::GetFullPath((Join-Path $idiRoot '.venv\Scripts\python.exe'))
    $idiBasePython = & $idiExpectedPython -c 'import sys; print(sys._base_executable)'
    $idiProcessAge = [Math]::Abs(($idiProcess.CreationDate.ToUniversalTime() - ([DateTime]$idiRecord.started_at).ToUniversalTime()).TotalSeconds)
    if (($idiProcess.ExecutablePath -ne $idiExpectedPython -and $idiProcess.ExecutablePath -ne $idiBasePython) -or $idiRecord.executable -ne $idiExpectedPython -or $idiProcessAge -gt 30 -or $idiProcess.CommandLine -notmatch '-m\s+idi\.server' -or $idiProcess.CommandLine -notmatch "--port\s+$([int]$idiRecord.port)(\s|$)") { throw 'PID ownership does not match this IDI service; no process was stopped.' }
    Stop-Process -Id ([int]$idiRecord.pid)
    Wait-Process -Id ([int]$idiRecord.pid) -Timeout 10 -ErrorAction SilentlyContinue
    Write-Output "Stopped IDI PID $($idiRecord.pid)."
} else { Write-Output 'IDI process is already stopped.' }
Remove-Item -LiteralPath $idiPidFile -Force
