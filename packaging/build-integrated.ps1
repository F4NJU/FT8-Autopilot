param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path (Join-Path $_ 'bin\wsjtx.exe') })]
    [string]$Ap1RuntimePath,

    [Parameter(Mandatory = $true)]
    [string]$Ap1SourceUrl
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:WSJTX_AP1_RUNTIME_DIR = (Resolve-Path -LiteralPath $Ap1RuntimePath).Path
$env:WSJTX_AP1_SOURCE_URL = $Ap1SourceUrl.Trim()
$env:FT8_REQUIRE_AP1_BUNDLE = '1'

Push-Location $projectRoot
try {
    py -m PyInstaller -y packaging\wsjtx-autopilot.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
    Remove-Item Env:WSJTX_AP1_RUNTIME_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:WSJTX_AP1_SOURCE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:FT8_REQUIRE_AP1_BUNDLE -ErrorAction SilentlyContinue
}

