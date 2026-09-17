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

# This machine is UofT-managed and runs SentinelOne + CrowdStrike Falcon.
# Both quarantine a freshly-compiled unsigned exe within a minute or so.
# Say so here rather than letting it look like the build silently failed.
$av = Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue |
    Where-Object { $_.displayName -match "Sentinel|CrowdStrike" } |
    Select-Object -ExpandProperty displayName

if ($av) {
    Write-Host "  WARNING: endpoint agent detected on this machine:" -ForegroundColor Yellow
    foreach ($a in $av) { Write-Host "    - $a" -ForegroundColor Yellow }
    Write-Host "  It will likely quarantine this exe within a minute, and" -ForegroundColor Yellow
    Write-Host "  dist\ will be empty when you look again. That is expected." -ForegroundColor Yellow
    Write-Host "  Run the app from the venv instead:" -ForegroundColor Yellow
    Write-Host "    .\.venv\Scripts\python.exe -m app.main" -ForegroundColor Yellow
    Write-Host "  or use .\packaging\install-autostart.ps1 for login startup." -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "  Note: the app reads its data from"
Write-Host "    $env:LOCALAPPDATA\RotmanLSMCalendar"
Write-Host "  First run needs one interactive LSM sign-in (Duo)."
Write-Host ""
