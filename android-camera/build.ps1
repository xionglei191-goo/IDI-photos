param([ValidateSet('debug','release')][string]$Variant='debug', [switch]$Test, [string]$GradlePath='')
$ErrorActionPreference = 'Stop'
$cameraRoot = $PSScriptRoot
$cameraTemp = Join-Path (Split-Path $cameraRoot -Parent) 'tmp\android-jvm'
New-Item -ItemType Directory -Path $cameraTemp -Force | Out-Null
$cameraOldOptions = $env:JAVA_TOOL_OPTIONS
try {
    # Short AF_UNIX path avoids Windows JDK selector failures under long temp paths.
    $env:JAVA_TOOL_OPTIONS = "$cameraOldOptions -Djava.io.tmpdir=$cameraTemp -Djdk.net.unixdomain.tmpdir=$cameraTemp".Trim()
    if (-not $env:ANDROID_HOME -and (Test-Path -LiteralPath 'F:\Android\Sdk')) { $env:ANDROID_HOME='F:\Android\Sdk' }
    if ($Variant -eq 'release' -and -not (Test-Path -LiteralPath (Join-Path $cameraRoot 'signing.properties'))) { throw 'Release signing is not configured. See README.md; do not replace an existing release key.' }
    $cameraGradle = if ($GradlePath) { (Resolve-Path -LiteralPath $GradlePath).Path } else { Join-Path $cameraRoot 'gradlew.bat' }
    Push-Location -LiteralPath $cameraRoot
    try {
        $cameraTaskName = ':app:assemble' + (Get-Culture).TextInfo.ToTitleCase($Variant)
        $cameraTasks = @($cameraTaskName, '--console=plain', '--no-daemon')
        if ($Test) { $cameraTasks += ':app:testDebugUnitTest' }
        & $cameraGradle @cameraTasks
        if ($LASTEXITCODE -ne 0) { throw "Android build failed: $LASTEXITCODE" }
    } finally { Pop-Location }
} finally { $env:JAVA_TOOL_OPTIONS = $cameraOldOptions }
