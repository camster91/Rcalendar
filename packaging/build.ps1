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

# setup.ps1 does not install the packer any more, because building is not the
# supported path and setup is. Without this check the missing module surfaces as
# a bare "PyInstaller failed" from the invocation below, which does not say what
# to do about it.
# 'Continue' around the probe: under 'Stop', PowerShell 5.1 turns a native
# command's redirected stderr (the ModuleNotFoundError traceback) into a
# terminating error, so the missing-packer case threw before this message.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $VenvPy -c "import PyInstaller" 2>$null
} finally {
    $ErrorActionPreference = $prevEap
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "  PyInstaller is not installed in this venv." -ForegroundColor Red
    Write-Host "  Install it if you mean to build:"
    Write-Host "    .\.venv\Scripts\python.exe -m pip install pyinstaller" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  (setup.ps1 leaves it out on purpose - see README.md.)" -ForegroundColor DarkGray
    exit 1
}

# The frozen app does not embed the browser; it relies on the per-user
# Playwright cache, so make sure that is populated before shipping. Guarded
# like every other native call here: this used to be the one without a
# $LASTEXITCODE check, so an offline build reported green and shipped a
# bundle whose browser-cache step had silently failed.
Write-Host "  Ensuring Chromium is present..."
& $VenvPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "playwright install chromium failed (offline? proxy?) - the frozen app relies on this per-user browser cache" }

# A single-file build from before the onedir change leaves
# dist\RotmanLSMCalendar.exe behind. Nothing else removes it -- COLLECT only
# cleans its own folder -- and a leftover named like the app is the worst kind
# of stale: it is the self-extracting shape the agents removed, so it invites
# exactly the detection this build exists to avoid, and it is what someone
# would try to run.
if (Test-Path $StaleOnefile) {
    Write-Host "  Removing stale single-file build from the previous layout..."
    Remove-Item $StaleOnefile -Force
}

# Regenerate the icon first: it is a committed build output, so it is only as
# current as the last time this ran, and the exe and the shortcuts both get
# whatever is on disk. Cheap, and it makes a stale icon impossible -- but only
# before PyInstaller, which embeds the .ico into the exe. It used to run just
# before ISCC, so the installer had the new icon and the exe the old one.
& $VenvPy (Join-Path $PSScriptRoot "make-icon.py")
if ($LASTEXITCODE -ne 0) { throw "make-icon.py failed" }

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

# -- Signing ---------------------------------------------------------------
#
# The exe is signed before it is packaged because the installer copies this
# folder as-is: after ISCC runs there is no second chance to sign what the
# installed app will actually execute. Unconditional, and a hard failure - a
# release that claims to be signed must be signed, and one that fails here
# must not reach verify-build, whose signature checks would only rediscover
# the same missing signature further from its cause.
& (Join-Path $PSScriptRoot "sign.ps1") -Path $DistExe

# -- The installer ---------------------------------------------------------
#
# Compiling the installer is part of building, not a separate errand: the .iss
# packages the folder built above, and an installer made from a stale dist\ is
# the one failure this ordering rules out. If Inno Setup is absent the folder
# build is still complete and is still usable on its own, so this is a warning
# with the fix in it rather than a failure.
# The search covers 6 and 7: installer.iss is written to compile under both
# (see its ArchitecturesAllowed note), and which one a machine or a CI
# runner has is not something to assume. A missing ISCC is a warning in this
# script and a hard failure in .github/workflows/release.yml, which throws
# when the installer the release ships is not there.
$Iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 7\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $Iscc) {
    Write-Host ""
    Write-Host "  Inno Setup 6 was not found, so no installer was built." -ForegroundColor Yellow
    Write-Host "  The folder build above is complete and usable as it stands."
    Write-Host "  For the installer, install Inno Setup 6 from jrsoftware.org, then:"
    Write-Host "    .\packaging\build.ps1" -ForegroundColor Yellow
    Write-Host ""
    exit 0
}

# The app's own version, so Add/Remove Programs cannot claim something the app
# does not. Read out of config.py rather than kept as a second copy here.
# Python puts the *current* directory on sys.path for -c, so the import only
# resolves from the repo root; run from packaging\ or anywhere else it found
# nothing and the script reported a config.py failure after a build that
# actually succeeded.
Push-Location $Root
try {
    $raw = & $VenvPy -c "import app.config as c; print(c.APP_VERSION)"
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        throw ("reading APP_VERSION failed (exit $LASTEXITCODE) - the " +
               "traceback above is the reason, this is only the consequence")
    }
    $AppVersion = $raw.Trim()
} finally {
    Pop-Location
}
if (-not $AppVersion) { throw "could not read APP_VERSION from app\config.py" }

Write-Host "  Compiling the installer (version $AppVersion)..."

& $Iscc "/DAppVersion=$AppVersion" (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$Setup = Get-ChildItem (Join-Path $Root "dist\RotmanLSMCalendar-Setup-*.exe") |
    Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $Setup) { throw "ISCC exited 0 but produced no setup exe in dist\" }

# Sign the installer too, and export the certificate beside it. The cert is
# the thing another machine imports to stop seeing "unknown publisher", so
# it ships with the release (release-local.ps1 attaches it). Signed here
# rather than later so the hash the release publishes is the hash of the
# signed binary - an unsigned-then-hashed release would describe a file
# nobody can download, because downloading replaces nothing and the
# published installer would be the unsigned one.
& (Join-Path $PSScriptRoot "sign.ps1") -Path $Setup.FullName `
    -ExportTo (Join-Path $Root "dist\RotmanLSMCalendar-CodeSigning.cer")

Write-Host ""
Write-Host "  Installer: $($Setup.FullName)" -ForegroundColor Green
Write-Host "    $([math]::Round($Setup.Length / 1MB, 1)) MB"
Write-Host ""

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
