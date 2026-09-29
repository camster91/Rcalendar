<#
.SYNOPSIS
Build, verify and publish a release locally - the Actions path, run here.

.DESCRIPTION
This IS the release path - not a stand-in for .github/workflows/release.yml
but its replacement. A release has to ship binaries signed with the
project's code-signing certificate, and that certificate is a per-user,
self-signed one in this machine's user store (packaging/sign.ps1): a
runner cannot hold it, and what a runner build signs with is an ephemeral
certificate build.ps1 mints for it, which dies with the runner (measured
2026-09-25 - the runner build that overwrote the first v1.1.2 publish was
signed by a certificate that no longer exists). The workflow is kept as a
manual/diagnostic path with a workflow_dispatch-only trigger; releases
are cut here. This script reproduces the workflow's steps in the
workflow's order, and adds the two things the runner did for free that a
local run has to promise:

  1. Nothing this script does may start release.yml - and the tag rule
     this file used to state is now known FALSE. Creating a tag through
     the API (gh release create --target master) was believed to fire
     only the *create* event, not the *push* event release.yml's
     tag trigger listens for; measured 2026-09-25, on the first v1.1.2
     publish, it fires the push event exactly like a pushed tag, and
     release.yml overwrote the signed release with a runner build
     within five minutes - a build signed with an ephemeral certificate
     the runner had minted for itself and took with it when it died.
     What makes this script safe is therefore not HOW the tag is made
     but release.yml's own trigger: since that day it listens for
     workflow_dispatch only, so no tag event of any kind can start it.
     The tag is still created through gh (never `git push` of a v*
     tag), because a release must name the commit it targets and the
     release IS the publish. Pushing the branch is fine and required -
     the release targets master.

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
$Repo = "camster91/lsm-calendar"
$Mirror = "camster91/rotman-lsm-calendar-releases"

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
    $raw = (& $VenvPy -c "import app.config as c; print(c.APP_VERSION)")
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        throw ("reading APP_VERSION failed (exit $LASTEXITCODE) - the " +
               "traceback above is the reason, this is only the consequence")
    }
    $AppVersion = $raw.Trim()
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

# The tag is created by this call, through the API. It fires the push event
# exactly like a pushed tag would (measured 2026-09-25 - see the header);
# what keeps that event from starting release.yml is the workflow's
# workflow_dispatch-only trigger, not the way this tag is made.
& gh release create "v$Version" --target master --title "v$Version" `
    --repo $Repo --notes-file $bodyPath `
    $Setup.FullName $shaPath $certPath
if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }

# -- The public mirror - what the updater actually reads ---------------------
# GitHub cannot serve a release publicly while its repo is private, and the
# app's updater is tokenless by design (app/config.py: UPDATES_REPO), so
# every release is published twice from this one run: on the private repo
# above (source, issues, history) and on the mirror (artifacts only). The
# same signed Setup, hash and .cer go to both, so the two carries cannot
# drift apart. The mirror's tag targets its own README commit - that repo
# deliberately holds no source (packaging\mirror-readme.md is that one file,
# seeded once via the GitHub contents API), so the tag is only the name the
# release hangs from. A failure here stops the script: this run says "released"
# only when the machine the app runs on can actually read what it shipped.
& gh release create "v$Version" --target main --title "v$Version" `
    --repo $Mirror --notes-file $bodyPath `
    $Setup.FullName $shaPath $certPath
if ($LASTEXITCODE -ne 0) { throw "gh release create on the mirror failed" }

Write-Host ""
Write-Host "  Released v$Version." -ForegroundColor Green
Write-Host "  https://github.com/$Repo/releases/tag/v$Version"
Write-Host "  https://github.com/$Mirror/releases/tag/v$Version  (what the app reads)"
