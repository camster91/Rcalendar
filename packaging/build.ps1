<#
.SYNOPSIS
    Build the app into dist\RotmanLSMCalendar\ as a folder (PyInstaller onedir).

.DESCRIPTION
    NOT THE SUPPORTED PATH. The app is run from the venv --
    .venv\Scripts\python.exe -m app.main, set up by .\packaging\setup.ps1 --
    and that is what install-autostart.ps1 points at. Nothing in this
    repository aims at the exe any more.

    You do not need this. Build it only when you need a self-contained folder
    to hand to someone without a checkout, and read the "Not the supported
    path" section of README.md first.

    Expect the build to be reported. Compiling an unsigned exe on this machine
    puts entries in the SentinelOne console -- measured 2026-09-21: the exe is
    flagged under build\ and again under dist\, both as "Suspicious Activity",
    one second apart. Nothing is quarantined and the build verifies clean, but
    that detection lands when the file is *written*, not when it runs, so it
    appears before verify-build.ps1 has executed anything. Check the quarantine
    count, not the threat history, before concluding a build broke.

    Produces a folder, not a single exe. A one-file build unpacks itself into
    %TEMP% and executes from there; on this machine (SentinelOne + CrowdStrike)
    that freshly-compiled unsigned self-extracting pattern is removed on
    execution. onedir never self-extracts -- it runs from where it sits.

    Ship the whole folder. _internal\ must travel with the exe; the exe on its
    own is not the app. Run .\packaging\verify-build.ps1 to check the result.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Spec = Join-Path $PSScriptRoot "RotmanLSMCalendar.spec"
$DistDir = Join-Path $Root "dist\RotmanLSMCalendar"
$DistExe = Join-Path $DistDir "RotmanLSMCalendar.exe"
$Internal = Join-Path $DistDir "_internal"
$StaleOnefile = Join-Path $Root "dist\RotmanLSMCalendar.exe"

Write-Host ""
Write-Host "  Building Rotman LSM Calendar" -ForegroundColor Cyan
Write-Host "  ============================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $VenvPy)) {
    Write-Host "  No venv found - run .\packaging\setup.ps1 first." -ForegroundColor Red
    exit 1
}

# The frozen app does not embed the browser; it relies on the per-user
# Playwright cache, so make sure that is populated before shipping.
Write-Host "  Ensuring Chromium is present..."
& $VenvPy -m playwright install chromium

# A single-file build from before this change leaves dist\RotmanLSMCalendar.exe
# behind. Nothing else removes it -- COLLECT only cleans its own folder -- and
# install-autostart.ps1 checks that exact path, so a leftover would silently
# keep pointing at the old, unrunnable artefact.
if (Test-Path $StaleOnefile) {
    Write-Host "  Removing stale single-file build from the previous layout..."
    Remove-Item $StaleOnefile -Force
}

# COLLECT warns about a source it cannot read, *skips* it, and still exits 0.
# Under onedir the build assembles into build\ and then copies to dist\, so a
# file removed mid-build would otherwise give a green build with a hole in
# dist\. Strict mode turns that into a failure instead.
$prevStrict = $env:PYINSTALLER_STRICT_COLLECT_MODE
$env:PYINSTALLER_STRICT_COLLECT_MODE = "1"

Write-Host "  Running PyInstaller..."
Push-Location $Root
try {
    & $VenvPy -m PyInstaller $Spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally {
    Pop-Location
    $env:PYINSTALLER_STRICT_COLLECT_MODE = $prevStrict
}

if (-not (Test-Path $DistExe)) { throw "expected $DistExe but it was not produced" }
if (-not (Test-Path $Internal)) { throw "expected $Internal but it was not produced" }

$files = Get-ChildItem $DistDir -Recurse -File
$sizeMb = [math]::Round((($files | Measure-Object -Property Length -Sum).Sum) / 1MB, 1)

Write-Host ""
Write-Host "  Built: $DistDir" -ForegroundColor Green
Write-Host "    $($files.Count) files, $sizeMb MB"
Write-Host "    exe: $DistExe"
Write-Host "  Ship the whole folder - the exe alone will not run."
Write-Host ""

if (Test-Path $StaleOnefile) {
    throw "dist\RotmanLSMCalendar.exe reappeared during the build"
}

# Report which endpoint agents are present. This is context, not a verdict:
# whether this build survives them is what verify-build.ps1 measures. Do not
# claim survival here until that has actually passed.
$av = Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue |
    Where-Object { $_.displayName -match "Sentinel|CrowdStrike" } |
    Select-Object -ExpandProperty displayName

if ($av) {
    Write-Host "  Endpoint agent detected on this machine:" -ForegroundColor Yellow
    foreach ($a in $av) { Write-Host "    - $a" -ForegroundColor Yellow }
    Write-Host "  A build here IS reported: expect a SentinelOne entry for this exe"
    Write-Host "  under build\ and under dist\. That is the compiler writing an"
    Write-Host "  unsigned exe, not a block -- read the quarantine count (it should"
    Write-Host "  be 0) rather than the threat history, and do not rebuild to try to"
    Write-Host "  clear it: the next build is flagged too, and repeated detections on"
    Write-Host "  the same binary are how a console line becomes a ticket."
    Write-Host "  A single-file build was removed on execution here. Whether the"
    Write-Host "  folder build survives is not assumed - measure it:"
    Write-Host "    .\packaging\verify-build.ps1" -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "  Note: the app reads its data from"
Write-Host "    $env:LOCALAPPDATA\RotmanLSMCalendar"
Write-Host "  First run needs one interactive LSM sign-in (Duo)."
Write-Host ""
