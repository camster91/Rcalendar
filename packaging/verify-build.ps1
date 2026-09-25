<#
.SYNOPSIS
    Verify the packaged folder build: does it run, and does it survive?

.DESCRIPTION
    NOT THE SUPPORTED PATH. There is nothing to verify unless you have just
    built the exe with build.ps1, which is itself unsupported -- building is
    what creates the detection, and the venv path writes no binary at all. See
    the "Not the supported path" section of README.md.

    Answers two separate questions, and reports on them separately:

      1. Does the bundle work?   Runs the exe with --selftest, which imports
         the GUI stack, checks the web assets and the Playwright driver, and
         fetches the calendar over HTTP, then reads its verdict from
         selftest.json.

      2. Does the bundle survive? A single-file build on this machine was
         deleted within about ten seconds of execution. This watches the
         folder with a FileSystemWatcher across a dwell after the run, then
         re-inventories every file -- because losing one .pyd leaves an app
         that starts and is broken, which is worse than a clean kill and
         invisible to a single existence check.

    The app's own log is the only diagnostic available: a windowed build has
    no console, so a startup crash produces no stderr.

    The dwell covers deletion *after* execution, which is the behaviour that
    was reported. It does not observe the app while it is running, because
    holding the app open would mean --no-window, and that asks LSM over the
    network on every run -- not something a build check should do.

.PARAMETER DwellSeconds
    How long to watch the folder after the run. Default 90.

.PARAMETER Port
    Port for the selftest to serve on. Default 8799, chosen to stay clear of
    the 8765 an interactive instance uses.

.EXAMPLE
    .\packaging\verify-build.ps1
    .\packaging\verify-build.ps1 -DwellSeconds 180
#>

[CmdletBinding()]
param(
    [int]$DwellSeconds = 90,
    [int]$Port = 8799
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$DistDir = Join-Path $Root "dist\RotmanLSMCalendar"
$DistExe = Join-Path $DistDir "RotmanLSMCalendar.exe"
$StaleOnefile = Join-Path $Root "dist\RotmanLSMCalendar.exe"

$failures = New-Object System.Collections.ArrayList
$scratch = Join-Path $env:TEMP ("lsm-verify-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$proc = $null
$watcher = $null
$subs = @()
$prevData = $env:LSM_DATA_DIR
$prevPort = $env:LSM_PORT

function Note-Failure {
    param([string]$Message)
    $null = $failures.Add($Message)
    Write-Host "  FAIL  $Message" -ForegroundColor Red
}

function Note-Pass {
    param([string]$Message)
    Write-Host "  PASS  $Message" -ForegroundColor Green
}

function Get-Inventory {
    # Relative path -> size, so a renamed or resized file is visible too.
    param([string]$Path)
    $map = @{}
    Get-ChildItem $Path -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        $rel = $_.FullName.Substring($Path.Length).TrimStart("\")
        $map[$rel] = $_.Length
    }
    return $map
}

Write-Host ""
Write-Host "  Rotman LSM Calendar - build verification" -ForegroundColor Cyan
Write-Host "  ========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $DistExe)) {
    Write-Host "  No build at $DistExe" -ForegroundColor Red
    Write-Host "  Run .\packaging\build.ps1 first."
    exit 1
}

New-Item -ItemType Directory -Force -Path $scratch | Out-Null
Write-Host "  scratch data dir: $scratch"
Write-Host "  port:             $Port"
Write-Host "  dwell:            ${DwellSeconds}s"
Write-Host ""

try {
    # -- 1. Inventory and identity ------------------------------------------
    $before = Get-Inventory $DistDir
    $exeHash = (Get-FileHash $DistExe -Algorithm SHA256).Hash
    Write-Host "  Before: $($before.Count) files, sha256 $($exeHash.Substring(0,16))..."
    Write-Host ""

    # -- 1b. Signatures ------------------------------------------------------
    # build.ps1 signs both binaries; this is the pipeline's own proof, read
    # back from the files rather than trusted from the signing step. The
    # criterion is deliberately NOT Status -eq "Valid": a self-signed
    # certificate's chain terminates in itself, which is in no trusted-root
    # store here on purpose (root trust is each machine's decision, not the
    # build's), so the readback reads UnknownError here and on every machine
    # that has not imported the shipped .cer. The three things this gate
    # CAN prove are the three ways signing actually fails: no signature,
    # the wrong certificate, or no timestamp - the last being the case
    # nothing else catches, because a timestamp server that did not answer
    # degrades Set-AuthenticodeSignature to a warning and an untimestamped
    # release would otherwise ship green.
    $SignSubject = "CN=Rotman LSM Calendar (self-signed code signing)"
    $sig = Get-AuthenticodeSignature -FilePath $DistExe
    if ($sig.Status -eq "NotSigned") {
        Note-Failure "exe is not signed at all"
    } elseif (-not $sig.SignerCertificate -or $sig.SignerCertificate.Subject -ne $SignSubject) {
        Note-Failure "exe signature is not the project certificate"
    } elseif (-not $sig.TimeStamperCertificate) {
        Note-Failure "exe signature has no timestamp - the timestamp server did not answer"
    } else {
        Note-Pass "exe signature is present, the project's, and timestamped"
    }

    $setupExe = Get-ChildItem (Join-Path $Root "dist\RotmanLSMCalendar-Setup-*.exe") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime | Select-Object -Last 1
    if ($setupExe) {
        $sig2 = Get-AuthenticodeSignature -FilePath $setupExe.FullName
        if ($sig2.Status -eq "NotSigned") {
            Note-Failure "setup exe is not signed at all"
        } elseif (-not $sig2.SignerCertificate -or $sig2.SignerCertificate.Subject -ne $SignSubject) {
            Note-Failure "setup signature is not the project certificate"
        } elseif (-not $sig2.TimeStamperCertificate) {
            Note-Failure "setup signature has no timestamp - the timestamp server did not answer"
        } else {
            Note-Pass "setup signature is present, the project's, and timestamped ($($setupExe.Name))"
        }
    } else {
        # A folder build without an installer is a state build.ps1 can leave
        # on purpose (ISCC absent); not a verdict, so not a failure here.
        Write-Host "  NOTE  no setup exe in dist\ - its signature was not checked"
    }
    Write-Host ""

    # -- 2. Watch for deletion while we work --------------------------------
    $watchLog = Join-Path $scratch "watcher.log"
    "watching $DistDir from $(Get-Date -Format 'HH:mm:ss')" | Set-Content -LiteralPath $watchLog

    $watcher = New-Object System.IO.FileSystemWatcher
    $watcher.Path = $DistDir
    $watcher.IncludeSubdirectories = $true
    $watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::Size

    $subs += Register-ObjectEvent -InputObject $watcher -EventName Deleted -MessageData $watchLog -Action {
        Add-Content -LiteralPath $Event.MessageData -Value ("{0}  DELETED  {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $Event.SourceEventArgs.FullName)
    }
    $subs += Register-ObjectEvent -InputObject $watcher -EventName Renamed -MessageData $watchLog -Action {
        Add-Content -LiteralPath $Event.MessageData -Value ("{0}  RENAMED  {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $Event.SourceEventArgs.FullName)
    }
    $watcher.EnableRaisingEvents = $true

    # -- 3. Run it ----------------------------------------------------------
    # Start-Process inherits this session's environment, so the override lands
    # in the child. It is restored in the finally block: leaking a port or a
    # data dir would break the next interactive run.
    $env:LSM_DATA_DIR = $scratch
    $env:LSM_PORT = "$Port"

    Write-Host "  Running --selftest..."
    $proc = Start-Process -FilePath $DistExe -ArgumentList @("--selftest") `
        -WorkingDirectory $DistDir -PassThru

    $deadline = (Get-Date).AddSeconds(120)
    while (-not $proc.HasExited -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }

    $selftestPath = Join-Path $scratch "selftest.json"
    $appLog = Join-Path $scratch "app.log"

    if (-not $proc.HasExited) {
        Note-Failure "the build did not exit within 120s (still running)"
    } else {
        $rc = $proc.ExitCode
        if ($rc -eq 0) {
            Note-Pass "the build ran and --selftest reported success (exit 0)"
        } else {
            Note-Failure "the build exited $rc"
        }
    }

    # -- 4. The bundle itself ----------------------------------------------
    if (Test-Path $selftestPath) {
        $st = Get-Content $selftestPath -Raw | ConvertFrom-Json
        foreach ($name in $st.checks.PSObject.Properties.Name) {
            $c = $st.checks.$name
            if ($c.ok) { Note-Pass "$name" } else { Note-Failure "$name -- $($c.detail)" }
        }
        Write-Host "    frozen=$($st.frozen)  web_dir=$($st.web_dir)"
    } else {
        # No report at all. Distinguish the two worlds rather than reporting a
        # flat failure: config.py opens its FileHandler at import, so the mere
        # existence of app.log proves the app got past the bootstrap stage.
        if (Test-Path $appLog) {
            Note-Failure "--selftest wrote no report, but app.log exists - the failure is after app.config loaded"
        } else {
            Note-Failure "--selftest wrote no report and there is no app.log - the failure is in the bootloader/PYZ stage"
        }
    }

    # -- 5. Dwell, then compare --------------------------------------------
    Write-Host ""
    Write-Host "  Watching for ${DwellSeconds}s..."
    $tick = (Get-Date).AddSeconds($DwellSeconds)
    while ((Get-Date) -lt $tick) {
        Start-Sleep -Seconds 5
        if (-not (Test-Path $DistExe)) { break }
    }

    $after = Get-Inventory $DistDir
    $gone = @($before.Keys | Where-Object { -not $after.ContainsKey($_) })
    $changed = @($before.Keys | Where-Object { $after.ContainsKey($_) -and $after[$_] -ne $before[$_] })
    $added = @($after.Keys | Where-Object { -not $before.ContainsKey($_) })

    if ($gone.Count -eq 0 -and $changed.Count -eq 0) {
        Note-Pass "all $($before.Count) files still present and unchanged after ${DwellSeconds}s"
    } else {
        if ($gone.Count -gt 0) { Note-Failure "$($gone.Count) file(s) disappeared, e.g. $($gone[0..([Math]::Min(4, $gone.Count-1))] -join ', ')" }
        if ($changed.Count -gt 0) { Note-Failure "$($changed.Count) file(s) changed size, e.g. $($changed[0..([Math]::Min(4, $changed.Count-1))] -join ', ')" }
    }
    if ($added.Count -gt 0) { Write-Host "    note: $($added.Count) new file(s) appeared" }

    $exeStill = Test-Path $DistExe
    if ($exeStill) { Note-Pass "the exe is still there" } else { Note-Failure "the exe is gone" }

    # A same-length in-place rewrite is the one tamper the size-based
    # inventory above cannot see, and the hash from section 1 exists for
    # exactly this comparison. Without it, a remediated binary passed as
    # "unchanged" on the strength of a byte count alone.
    if ($exeStill) {
        $exeHashAfter = (Get-FileHash $DistExe -Algorithm SHA256).Hash
        if ($exeHashAfter -eq $exeHash) {
            Note-Pass "the exe is byte-for-byte what was inventoried (sha256 unchanged)"
        } else {
            Note-Failure "the exe changed in place (sha256 differs from the start-of-run hash) - the bundle measured is not the bundle built"
        }
    }

    if (Test-Path $StaleOnefile) {
        Note-Failure "a stale single-file dist\RotmanLSMCalendar.exe is present"
    }

    # -- 6. What the watcher saw -------------------------------------------
    Write-Host ""
    Write-Host "  watcher log: $watchLog"
    Get-Content -LiteralPath $watchLog -ErrorAction SilentlyContinue |
        Select-Object -Skip 1 | Select-Object -First 20 | ForEach-Object { "    $_" }
} finally {
    # -- Cleanup ------------------------------------------------------------
    foreach ($s in $subs) { Unregister-Event -SourceIdentifier $s.Name -ErrorAction SilentlyContinue }
    Get-Event -SourceIdentifier "*" -ErrorAction SilentlyContinue |
        Where-Object { $subs.Name -contains $_.SourceIdentifier } |
        Remove-Event -ErrorAction SilentlyContinue
    if ($watcher) {
        $watcher.EnableRaisingEvents = $false
        $watcher.Dispose()
    }

    if ($proc -and -not $proc.HasExited) {
        # /T so anything the app spawned goes with it. In onedir the bootloader
        # loads python in-process, so the process to worry about is Playwright's
        # bundled node.exe and the Chromium it drives.
        & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null
    }

    # Belt and braces: anything still running out of the dist folder.
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($DistDir, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { & taskkill /PID $_.Id /T /F 2>&1 | Out-Null }

    $env:LSM_DATA_DIR = $prevData
    $env:LSM_PORT = $prevPort
}

Write-Host ""
Write-Host "  ========================================"
if ($failures.Count -eq 0) {
    Write-Host "  Verified: the folder build runs and survived." -ForegroundColor Green
} else {
    Write-Host "  $($failures.Count) problem(s):" -ForegroundColor Red
    foreach ($f in $failures) { Write-Host "    - $f" -ForegroundColor Red }
    Write-Host ""
    Write-Host "  The app log, if there is one:" -ForegroundColor Yellow
    Get-Content (Join-Path $scratch "app.log") -Tail 30 -ErrorAction SilentlyContinue | ForEach-Object { "    $_" }
}
Write-Host "  ========================================"
Write-Host ""
Write-Host "  scratch left at: $scratch"
Write-Host ""

if ($failures.Count -gt 0) { exit 1 }
exit 0
