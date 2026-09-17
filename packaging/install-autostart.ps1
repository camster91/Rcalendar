<#
.SYNOPSIS
    Register (or remove) the app to start automatically at login.

.DESCRIPTION
    Adds a shortcut to the current user's Startup folder — no admin
    rights needed, and trivially reversible.

    Points at the venv by default. On a UofT-managed machine SentinelOne
    and CrowdStrike quarantine the unsigned PyInstaller exe shortly after
    it is built, so a shortcut aimed at it would break silently at the
    next login. A signed pythonw.exe is left alone. Pass -UseExe to
    override on an unmanaged machine.

.EXAMPLE
    .\packaging\install-autostart.ps1
    .\packaging\install-autostart.ps1 -Remove
    .\packaging\install-autostart.ps1 -UseExe
#>

[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$UseExe
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Startup = [Environment]::GetFolderPath("Startup")
$Link = Join-Path $Startup "Rotman LSM Calendar.lnk"

Write-Host ""

if ($Remove) {
    if (Test-Path $Link) {
        Remove-Item $Link -Force
        Write-Host "  Removed autostart entry." -ForegroundColor Green
    } else {
        Write-Host "  No autostart entry was present." -ForegroundColor Yellow
    }
    Write-Host ""
    exit 0
}

$exe = Join-Path $Root "dist\RotmanLSMCalendar.exe"
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"

if ($UseExe -and (Test-Path $exe)) {
    $target = $exe
    $arguments = ""
    Write-Host "  Using packaged executable."
} elseif (Test-Path $venvPy) {
    if ($UseExe) {
        Write-Host "  -UseExe given but no exe in dist\ — using the venv." -ForegroundColor Yellow
    }
    # pythonw runs without a console window — python.exe would flash a
    # black box on every login.
    $target = if (Test-Path $pythonw) { $pythonw } else { $venvPy }
    $arguments = "-m app.main"
    Write-Host "  Using dev checkout via venv."
} elseif (Test-Path $exe) {
    $target = $exe
    $arguments = ""
    Write-Host "  No venv found — falling back to the packaged executable." -ForegroundColor Yellow
} else {
    Write-Host "  Neither .venv nor dist\RotmanLSMCalendar.exe found." -ForegroundColor Red
    Write-Host "  Run .\packaging\setup.ps1 first."
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($Link)
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $Root
$shortcut.Description = "Rotman LSM Calendar"
$shortcut.Save()

Write-Host ""
Write-Host "  Autostart enabled." -ForegroundColor Green
Write-Host "    target: $target $arguments"
Write-Host "    link:   $Link"
Write-Host ""
Write-Host "  The app starts minimized to the tray and scrapes daily at 06:00."
Write-Host "  Remove with: .\packaging\install-autostart.ps1 -Remove"
Write-Host ""
