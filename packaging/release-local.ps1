<#
.SYNOPSIS
Build, verify and publish a release locally - the Actions path, run here.

.DESCRIPTION
.github/workflows/release.yml is the documented release path; this script
is its local stand-in for as long as GitHub Actions cannot run (billing
exhausted, every private-repo job wedged in "queued"). It reproduces the
workflow's steps in the workflow's order, on this machine, and adds the
two things the runner did for free that a local run has to promise:

  1. The tag is created BY THE API, never pushed. `gh release create
     --target master` makes the tag through the API, which fires the
     *create* event; a `git push` of a v* tag fires the *push* event that
     release.yml listens for. Pushing the branch is fine and required -
     the release targets master - but pushing a tag would start the very
     workflow that cannot run.

  2. The hash is taken AFTER signing. build.ps1 signs the installer
     before this script hashes it, so sha256.txt and the release body
     describe the binary the release actually serves.

There are no flags to skip the suites or the verification. The runner
could not skip them, and a flag whose only use is the day it should not
be used is a hole waiting to be fallen through.

.EXAMPLE
.\packaging\release-local.ps1 -Version 1.1.2
#>

param(
    [Parameter(Mandatory = $true)][string]$Version
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Repo = "camster91/rotman-lsm-calendar"

if (-not (Test-Path $VenvPy)) {
    throw "no venv found - run .\packaging\setup.ps1 first"
}

# The release name and the app's version are the same claim made twice; a
# build that installs as one version and reports another is the mismatch
# this rules out. Read out of config.py the way build.ps1 does - Python
# puts the *current* directory on sys.path for -c, so the import only
# resolves from the repo root.
Push-Location $Root
try {
    $AppVersion = (& $VenvPy -c "import app.config as c; print(c.APP_VERSION)").Trim()
} finally {
    Pop-Location
}
if (-not $AppVersion) { throw "could not read APP_VERSION from app\config.py" }
if ($Version -ne $AppVersion) {
    throw "version mismatch: -Version $Version but app\config.py says $AppVersion"
}

# Inno Setup is a hard failure here. build.ps1 treats a missing ISCC as a
# warning because a folder build is still a build; this script exists to
# publish an installer, so "no installer" cannot pass through it.
$Iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 7\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { throw "Inno Setup was not found - the release needs its installer" }

Write-Host ""
Write-Host "  Rotman LSM Calendar v$Version - local release" -ForegroundColor Cyan
Write-Host "  =============================================" -ForegroundColor Cyan
Write-Host ""

# -- The suites, in the workflow's order ------------------------------------
$suites = @(
    "test_parse", "test_smoke", "test_changes", "test_filters",
    "test_session", "test_update", "test_worker", "test_web"
)
foreach ($suite in $suites) {
    Write-Host "  suite: $suite"
    & $VenvPy (Join-Path $Root "tests\$suite.py")
    if ($LASTEXITCODE -ne 0) { throw "$suite failed - the release stops here" }
    Write-Host ""
}

# -- Build (PyInstaller -> sign -> installer -> sign) -----------------------
& (Join-Path $Root "packaging\build.ps1")
if ($LASTEXITCODE -ne 0) { throw "build.ps1 failed" }
Write-Host ""

# -- Verify (selftest, dwell, inventory, signatures) -------------------------
& (Join-Path $Root "packaging\verify-build.ps1")
if ($LASTEXITCODE -ne 0) { throw "verify-build.ps1 failed" }
Write-Host ""

# -- Hash AFTER signing -------------------------------------------------------
$Setup = Get-ChildItem (Join-Path $Root "dist\RotmanLSMCalendar-Setup-*.exe") |
    Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $Setup) { throw "no setup exe in dist\ - nothing to publish" }

$hash = (Get-FileHash $Setup.FullName -Algorithm SHA256).Hash
$shaPath = Join-Path $Root "dist\sha256.txt"
("$hash  " + $Setup.Name) | Set-Content -LiteralPath $shaPath -Encoding Ascii

$notes = Get-Content (Join-Path $Root "packaging\release-notes.md") -Raw
if ($notes -notmatch "SHA256_PLACEHOLDER") {
    throw "packaging\release-notes.md has no SHA256_PLACEHOLDER to substitute"
}
$bodyPath = Join-Path $Root "dist\release_body.md"
$notes.Replace("SHA256_PLACEHOLDER", $hash) |
    Set-Content -LiteralPath $bodyPath -Encoding UTF8

$certPath = Join-Path $Root "dist\RotmanLSMCalendar-CodeSigning.cer"
if (-not (Test-Path $certPath)) { throw "no code-signing .cer in dist\ - build.ps1 should have written it" }

Write-Host "  publishing v$Version to $Repo" -ForegroundColor Cyan
Write-Host "    installer: $($Setup.Name)"
Write-Host "    sha256:    $hash"
Write-Host ""

# The tag is created by this call, through the API - see the header for why
# that is the difference between a release and a broken workflow run.
& gh release create "v$Version" --target master --title "v$Version" `
    --repo $Repo --notes-file $bodyPath `
    $Setup.FullName $shaPath $certPath
if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }

Write-Host ""
Write-Host "  Released v$Version." -ForegroundColor Green
Write-Host "  https://github.com/$Repo/releases/tag/v$Version"