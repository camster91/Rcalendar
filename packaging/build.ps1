<#
.SYNOPSIS
    Build dist\RotmanLSMCalendar.exe with PyInstaller.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Spec = Join-Path $PSScriptRoot "RotmanLSMCalendar.spec"

Write-Host ""
Write-Host "  Building Rotman LSM Calendar" -ForegroundColor Cyan
Write-Host "  ============================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $VenvPy)) {
    Write-Host "  No venv found — run .\packaging\setup.ps1 first." -ForegroundColor Red
    exit 1
}

# The frozen app does not embed the browser; it relies on the per-user
# Playwright cache, so make sure that is populated before shipping.
Write-Host "  Ensuring Chromium is present..."
& $VenvPy -m playwright install chromium

Write-Host "  Running PyInstaller..."
Push-Location $Root
try {
    & $VenvPy -m PyInstaller $Spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally {
    Pop-Location
}

$exe = Join-Path $Root "dist\RotmanLSMCalendar.exe"
if (-not (Test-Path $exe)) { throw "expected $exe but it was not produced" }

$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "  Built: $exe ($sizeMb MB)" -ForegroundColor Green
Write-Host ""
Write-Host "  Note: the app reads its data from"
Write-Host "    $env:LOCALAPPDATA\RotmanLSMCalendar"
Write-Host "  First run needs one interactive LSM sign-in (Duo)."
Write-Host ""
