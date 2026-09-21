<#
.SYNOPSIS
    Register (or remove) the app to start automatically at login.

.DESCRIPTION
    Adds a shortcut to the current user's Startup folder - no admin rights
    needed, and trivially reversible.

    Points at the venv, which is the only supported way to run the app: a
    signed pythonw.exe running source is left alone by the endpoint agents,
    and it compiles nothing. There used to be a -UseExe switch that aimed the
    shortcut at the packaged folder build instead; it is gone, because building
    that exe writes an unsigned binary the agents report (see the "Not the
    supported path" section of README.md). Nothing here can point Startup at
    the exe any more.

.EXAMPLE
    .\packaging\install-autostart.ps1
    .\packaging\install-autostart.ps1 -Remove
#>

[CmdletBinding()]
param(
    [switch]$Remove
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

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"

$workdir = $Root

if (Test-Path $venvPy) {
    # pythonw runs without a console window - python.exe would flash a
    # black box on every login.
    $target = if (Test-Path $pythonw) { $pythonw } else { $venvPy }
    $arguments = "-m app.main"
    Write-Host "  Using the venv."
} else {
    Write-Host "  No venv found at .venv\Scripts\python.exe." -ForegroundColor Red
    Write-Host "  Run .\packaging\setup.ps1 first."
    Write-Host ""
    Write-Host "  (There is no exe fallback. The packaged build is not the" -ForegroundColor DarkGray
    Write-Host "   supported path - see README.md.)" -ForegroundColor DarkGray
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
