# PyInstaller spec — builds dist\RotmanLSMCalendar.exe
#
# The Playwright browser is deliberately NOT bundled: it is ~150 MB and
# only needs installing once per machine. setup.ps1 / build.ps1 run
# `playwright install chromium`, which places it in the normal user cache
# (%LOCALAPPDATA%\ms-playwright) where the frozen app finds it.
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
    a.binaries,
    a.datas,
    [],
    name="RotmanLSMCalendar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX trips Windows Defender heuristics
    runtime_tmpdir=None,
    console=False,           # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,               # tray icon is drawn at runtime
)
