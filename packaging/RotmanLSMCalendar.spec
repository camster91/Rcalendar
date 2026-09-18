# PyInstaller spec — builds dist\RotmanLSMCalendar\ (onedir)
#
# One directory, not one file, and that is the whole point. A single-file
# build unpacks itself into %TEMP% and executes from there; on a machine
# running SentinelOne + CrowdStrike that freshly-compiled unsigned
# self-extracting pattern gets removed on execution. onedir never
# self-extracts — it ships a bootloader exe beside an _internal\ folder and
# runs from where it sits.
#
# _internal\ must travel with the exe: the folder is ~914 files and the exe
# alone is not the app. Note the bootloader embeds the PYZ, so it is a few MB
# rather than a thin shim.
#
# The Playwright *browser* is still deliberately NOT bundled: it is ~150 MB
# and only needs installing once per machine. setup.ps1 / build.ps1 run
# `playwright install chromium`, which places it in the normal user cache
# (%LOCALAPPDATA%\ms-playwright) where the frozen app finds it. The Playwright
# *driver* (a bundled node.exe) is different — it ships inside _internal\, and
# is exactly what --selftest checks for, since losing it would leave an app
# that serves the UI perfectly and cannot scrape.
#
# Build with:  pyinstaller packaging\RotmanLSMCalendar.spec --noconfirm

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "web"), "web")],
    hiddenimports=[
        # pystray resolves its backend dynamically.
        "pystray._win32",
        # pywebview picks a GUI backend at runtime.
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        # icalendar and playwright both import lazily in places.
        "icalendar",
        "playwright",
        "playwright.sync_api",
        "PIL._tkinter_finder",
        "clr_loader",
        "pythonnet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "numpy", "pandas", "scipy", "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # assemblies belong to COLLECT, not the exe
    name="RotmanLSMCalendar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX trips Windows Defender heuristics
    console=False,           # GUI app — no console window
    # Keep tracebacks ON. On a startup failure the windowed bootloader shows a
    # modal dialog containing the traceback, which is the only diagnostic a
    # console-less build can offer; disabling it shows a dialog that says less.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,               # tray icon is drawn at runtime
)

# COLLECT writes _internal\ beside the exe. Do not flatten this with
# contents_directory="." — the stock layout is the one that was measured to
# survive; a different shape is an untested AV profile.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="RotmanLSMCalendar",
)
