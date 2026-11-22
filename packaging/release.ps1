param(
    [Parameter(Mandatory = $true)]
    [string]$Ap1RuntimePath,

    [Parameter(Mandatory = $true)]
    [string]$Ap1SourceUrl,

    [string[]]$Python = @('python'),

    [string]$Iscc = 'ISCC.exe'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path $projectRoot 'dist'
Push-Location $projectRoot
try {
    $version = (& @Python 'tools\release.py' version | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read the release version.' }

    & @Python 'tools\release.py' check
    if ($LASTEXITCODE -ne 0) { throw 'Version sources are inconsistent.' }
    Write-Host "Release version: $version"

    & "$PSScriptRoot\build-integrated.ps1" -Ap1RuntimePath $Ap1RuntimePath -Ap1SourceUrl $Ap1SourceUrl

    $sourceDir = Join-Path $distDir 'WSJTX-AutoPilot'
    & $Iscc "/DAppSourceDir=$sourceDir" "/DAppVersion=$version" "$PSScriptRoot\WSJTX-AutoPilot.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }

    & @Python 'tools\release.py' checksums --dist $distDir
    if ($LASTEXITCODE -ne 0) { throw 'SHA256SUMS.txt generation failed.' }

    & @Python 'tools\release.py' verify --dist $distDir
    if ($LASTEXITCODE -ne 0) { throw 'SHA256SUMS.txt verification failed.' }

    & @Python 'tools\release.py' check-artifact --dist $distDir
    if ($LASTEXITCODE -ne 0) { throw 'Built artifact validation failed.' }

    Write-Host "Release files ready: $distDir\WSJTX-AutoPilot-Setup.exe and $distDir\SHA256SUMS.txt"
}
finally {
    Pop-Location
}
