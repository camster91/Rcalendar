<#
.SYNOPSIS
    Register (or remove) the app to start automatically at login.

.DESCRIPTION
    Adds a shortcut to the current user's Startup folder - no admin rights
    needed, and trivially reversible.

    Points at the venv by default. The packaged build is a *folder*
    (dist\RotmanLSMCalendar\), so a shortcut aimed at it has to target the exe
    inside that folder and start in it -- _internal\ sits beside the exe and is
    resolved relative to it. The venv stays the default because it is the same
    app and a signed pythonw.exe is left alone by the endpoint agents. Pass
    -UseExe to prefer the packaged build.

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

$distDir = Join-Path $Root "dist\RotmanLSMCalendar"
$exe = Join-Path $distDir "RotmanLSMCalendar.exe"
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"

$workdir = $Root

if ($UseExe -and (Test-Path $exe)) {
    $target = $exe
    $arguments = ""
    $workdir = $distDir
    Write-Host "  Using packaged folder build."
} elseif (Test-Path $venvPy) {
    if ($UseExe) {
        Write-Host "  -UseExe given but no build in dist\RotmanLSMCalendar\ - using the venv." -ForegroundColor Yellow
    }
    # pythonw runs without a console window - python.exe would flash a
    # black box on every login.
    $target = if (Test-Path $pythonw) { $pythonw } else { $venvPy }
    $arguments = "-m app.main"
    Write-Host "  Using dev checkout via venv."
} elseif (Test-Path $exe) {
    $target = $exe
    $arguments = ""
    $workdir = $distDir
    Write-Host "  No venv found - falling back to the packaged build." -ForegroundColor Yellow
} else {
    Write-Host "  Neither .venv nor dist\RotmanLSMCalendar\RotmanLSMCalendar.exe found." -ForegroundColor Red
    Write-Host "  Run .\packaging\setup.ps1 first."
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($Link)
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $workdir
$shortcut.Description = "Rotman LSM Calendar"
$shortcut.Save()

Write-Host ""
Write-Host "  Autostart enabled." -ForegroundColor Green
Write-Host "    target: $target $arguments"
Write-Host "    start in: $workdir"
Write-Host "    link:   $Link"
Write-Host ""
Write-Host "  The app starts minimized to the tray and scrapes daily at 06:00."
Write-Host "  Remove with: .\packaging\install-autostart.ps1 -Remove"
Write-Host ""
