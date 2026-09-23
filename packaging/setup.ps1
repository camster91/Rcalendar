<#
.SYNOPSIS
    One-time setup: create the venv, install dependencies, fetch Chromium.

.NOTES
    This machine has two Python 3.13 installs and the *broken* one wins
    the `py` launcher: a per-user install under
    %LOCALAPPDATA%\Programs\Python\Python313 that is missing its Lib
    directory entirely. Creating a venv from it fails with
    "Could not find platform independent libraries <prefix>".

    So rather than trusting `py` or `python` on PATH, this script
    validates candidate interpreters by actually importing sysconfig/ssl
    and only then uses one.
#>

[CmdletBinding()]
param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

function Test-Interpreter {
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path $Exe)) { return $false }
    try {
        & $Exe -c "import sysconfig, ssl, sqlite3, venv" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Interpreter {
    $candidates = [System.Collections.Generic.List[string]]::new()

    if ($Python) { $candidates.Add($Python) }

    # Prefer real system installs over per-user ones.
    foreach ($base in @("C:\Program Files", "C:\Program Files (x86)")) {
        Get-ChildItem $base -Filter "Python3*" -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                $exe = Join-Path $_.FullName "python.exe"
                if (Test-Path $exe) { $candidates.Add($exe) }
            }
    }

    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter "Python3*" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object {
            $exe = Join-Path $_.FullName "python.exe"
            if (Test-Path $exe) { $candidates.Add($exe) }
        }

    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates.Add($cmd.Source) }
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($v in @("3.13", "3.12", "3.11")) {
            try {
                $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
                if ($p) { $candidates.Add($p.Trim()) }
            } catch { }
        }
    }

    foreach ($c in $candidates) {
        if (Test-Interpreter $c) { return $c }
    }
    return $null
}

Write-Host ""
Write-Host "  Rotman LSM Calendar - setup" -ForegroundColor Cyan
Write-Host "  ===========================" -ForegroundColor Cyan
Write-Host ""

$py = Find-Interpreter
if (-not $py) {
    Write-Host "  ERROR: no working Python 3.11+ interpreter found." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Your per-user install at:"
    Write-Host "    $env:LOCALAPPDATA\Programs\Python\Python313"
    Write-Host "  appears to be incomplete (it has no Lib directory)."
    Write-Host ""
    Write-Host "  Fix either by repairing that install, or pass a good one:"
    Write-Host "    .\packaging\setup.ps1 -Python 'C:\Program Files\Python313\python.exe'"
    exit 1
}

$version = & $py -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "  Using Python $version" -ForegroundColor Green
Write-Host "    $py"
Write-Host ""

if (Test-Path $Venv) {
    Write-Host "  Removing existing venv..."
    Remove-Item -Recurse -Force $Venv
}

Write-Host "  Creating virtual environment..."
& $py -m venv $Venv
if (-not (Test-Path $VenvPy)) { throw "venv creation failed" }

Write-Host "  Upgrading pip..."
& $VenvPy -m pip install --upgrade pip --quiet

Write-Host "  Installing dependencies (this takes a minute)..."
# From requirements.txt, not from a list typed here: a floor bumped in the
# file would silently miss this path while CI (which installs -r) stays
# green, and two lists maintained by hand are a drift trap the moment
# either one changes.
& $VenvPy -m pip install --quiet -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

Write-Host "  Downloading Chromium for Playwright..."
& $VenvPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "playwright install chromium failed - run .\packaging\setup.ps1 again once the network allows it" }

Write-Host ""
Write-Host "  Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Run the app:"
Write-Host "    .\.venv\Scripts\python.exe -m app.main"
Write-Host ""
Write-Host "  Then click 'Sign in to LSM' once, and approve Duo."
Write-Host ""
