"""
Windows app entry point.

Three moving parts, deliberately separated:

  Flask (daemon thread)      serves the calendar on 127.0.0.1
  Orchestrator (daemon)      owns Playwright; scrapes, keeps the session warm
  pywebview (main thread)    the native window; WebView2, so it is the real
                             system browser engine rather than a bundled one

The tray icon runs on its own thread. Closing the window hides it rather
than quitting, so the daily scrape keeps happening in the background.

Usage:
    python -m app.main                 # the app
    python -m app.main --scrape-once   # scrape and exit (for Task Scheduler)
    python -m app.main --backfill      # redo the one-time history fill and exit
    python -m app.main --probe         # report session state and exit
    python -m app.main --no-window     # run headless, web UI only
    python -m app.main --selftest      # check a packaged build is intact, exit

The history fill is not something you ask for: the app gives it to itself
once, on the first launch that has a live session, and never offers it again
(see Orchestrator._queue_backfill_if_owed). --backfill is the repair path for
a fill that did not complete, and the only way to ask for it a second time.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from app import session, store
from app.config import (
    APP_NAME, BACKFILL_MONTHS, BUNDLE_DIR, DATA_DIR, DB_PATH, WEB_DIR, WEB_HOST,
    WEB_PORT, WINDOW_TITLE, log,
)
from app.scheduler import Orchestrator
from app.server import create_app

BASE_URL = f"http://{WEB_HOST}:{WEB_PORT}"


# ── Tray icon ────────────────────────────────────────────────────────────

def _make_icon_image() -> Any:
    """Draw the tray icon at runtime so there is no binary asset to ship."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    navy = (0, 53, 95, 255)
    d.rounded_rectangle([4, 10, 60, 58], radius=8, fill=navy)

    white = (255, 255, 255, 255)
    d.rectangle([4, 10, 60, 22], fill=white)
    d.rectangle([14, 4, 20, 16], fill=white)
    d.rectangle([44, 4, 50, 16], fill=white)

    # A few "bookings" on the grid.
    d.rectangle([12, 30, 26, 36], fill=(59, 130, 246, 255))
    d.rectangle([32, 30, 52, 36], fill=(16, 185, 129, 255))
    d.rectangle([12, 42, 40, 48], fill=(245, 158, 11, 255))
    return img


class Tray:
    def __init__(self, orch: Orchestrator, window: Any) -> None:
        self.orch = orch
        self.window = window
        self._icon = None

    def start(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Open Calendar", self._open, default=True),
            pystray.MenuItem("List View", self._open_list),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _: self._label(),
                lambda: None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Scrape Now", self._scrape),
            pystray.MenuItem("Sign in to LSM", self._login),
            pystray.MenuItem("Check Session", self._probe),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Data Folder", self._open_data),
            pystray.MenuItem("Open in Browser", self._open_browser),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

        self._icon = pystray.Icon(
            "rotman-lsm", _make_icon_image(), APP_NAME, menu
        )
        self._icon.run()

    def _label(self) -> str:
        st = self.orch.status()
        if st.get("busy"):
            return f"⏳ {st.get('busy_action') or 'Working'}…"
        sess = st.get("session")
        if sess == "ok":
            return "● Connected to LSM"
        if sess == "expired":
            return "● Session expired"
        return "● Session unknown"

    # ── Menu actions ─────────────────────────────────────────────────────

    def _open(self) -> None:
        try:
            self.window.show()
            self.window.restore()
        except Exception:
            webbrowser.open(BASE_URL)

    def _open_list(self) -> None:
        try:
            self.window.load_url(f"{BASE_URL}/list")
            self.window.show()
        except Exception:
            webbrowser.open(f"{BASE_URL}/list")

    def _open_browser(self) -> None:
        webbrowser.open(BASE_URL)

    def _open_data(self) -> None:
        import subprocess

        subprocess.Popen(["explorer", str(DATA_DIR)])

    def _scrape(self) -> None:
        if self.orch.is_busy():
            self._notify("Already running", "A scrape is already in progress.")
            return
        self.orch.request_scrape(interactive=False)
        self._notify("Scraping LSM", "Fetching the latest bookings…")

    def _login(self) -> None:
        if self.orch.is_busy():
            self._notify("Busy", "Something else is running right now.")
            return
        self.orch.request_login()
        self._notify("Sign in", "A browser window opened — approve Duo there.")

    def _probe(self) -> None:
        self.orch.request_probe()
        self._notify("Checking session", "Asking LSM whether we are signed in…")

    def _quit(self) -> None:
        log.info("quit requested")
        self.orch.stop()
        if self._icon:
            self._icon.stop()
        try:
            self.window.destroy()
        except Exception:
            pass

    def _notify(self, title: str, message: str) -> None:
        try:
            if self._icon:
                self._icon.notify(message, title)
        except Exception:
            pass


# ── Server ───────────────────────────────────────────────────────────────

def _serve(orch: Orchestrator) -> None:
    app = create_app(orch)
    try:
        # threaded=True so a slow scrape never blocks the UI's status polling.
        app.run(host=WEB_HOST, port=WEB_PORT, threaded=True,
                use_reloader=False, debug=False)
    except OSError:
        # This thread is a daemon with nobody joining it, so an unhandled
        # exception here was invisible: the thread died, and _wait_for_server
        # then *succeeded* by connecting to whatever already held the port.
        # A windowed build has no stdout either, so the traceback went nowhere.
        log.exception("web UI could not bind %s — another process holds the "
                      "port, or it is in TIME_WAIT", BASE_URL)


# ── Single instance ──────────────────────────────────────────────────────

def _claim_instance(path: Path) -> int | None:
    """Take an exclusive lock on this data directory.

    Returns the held descriptor, or None when another instance already has it.
    The descriptor *is* the lock: it must stay open for the life of the
    process, and closing it releases the lock. Nothing has to hold on to it
    either — an int descriptor has no finalizer, so dropping the value leaks
    the open file and the lock still stands (measured, because the obvious
    guess is the opposite). The kernel drops it when the process ends, however
    it ends, which is what keeps a crash from wedging every later launch.

    The lock is on the *data directory*, not on the web port, because the data
    directory is the thing two instances must not share: one SQLite file and one
    Chromium profile holding a live LSM session. Probing the port would answer a
    different question — it would call a TIME_WAIT socket "already running", and
    it would not notice an instance whose server had died but whose worker was
    still scraping. A reader who wants a second copy side by side sets
    LSM_DATA_DIR, which gives it a different directory and so a different lock.

    -1 means this platform has no file locking and the guard was skipped.
    """
    try:
        import msvcrt
    except ImportError:  # not Windows; the app is Windows-only, but do not wedge
        log.warning("no file locking on this platform — single-instance guard skipped")
        return -1

    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        # A byte must exist for the range to be meaningfully locked, and the
        # pid makes the file useful to a human looking at a stuck lock.
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return None

    return fd


def _already_running_notice(gui: bool = False) -> None:
    """Say so where the reader will actually see it.

    A windowed build has no console, so print() and the log both go somewhere
    nobody is looking; gui=True adds the one surface still left. The CLI modes
    have a console, so they pass gui=False and just print.
    """
    message = (
        f"{APP_NAME} is already running.\n\n"
        f"Look for its icon in the notification area (it may be under the "
        f"hidden-icons arrow).\n\n"
        f"Its data is at:\n{DATA_DIR}\n\n"
        f"To run a second, separate copy, set LSM_DATA_DIR and LSM_PORT to "
        f"different values first."
    )
    log.error("another instance already owns %s — not starting", DATA_DIR)
    print(message)
    if not gui:
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
    except Exception:
        log.exception("could not show the already-running notice")


def _another_instance_is_running(gui: bool = False) -> bool:
    """Claim the data directory, or say who has it.

    False means this process now owns the directory for the rest of its life.
    True means another instance has it and the reader has already been told
    why, so the caller only has to return 1 — every entry point opens with
    this, before the database is touched or a worker is started.

    Asking is claiming, so call it once. A second call in the same process
    reports the first call's own lock as a second instance.
    """
    if _claim_instance(DATA_DIR / "app.lock") is not None:
        return False
    _already_running_notice(gui=gui)
    return True


def _wait_for_server(timeout: float = 20.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((WEB_HOST, WEB_PORT)) == 0:
                return True
        time.sleep(0.2)
    return False


# ── Modes ────────────────────────────────────────────────────────────────

def run_scrape_once() -> int:
    # These modes drive the same Chromium profile the app uses, so they must not
    # run beside it either — two Playwright browsers on one profile is how a
    # live session gets clobbered. The running app scrapes on its own schedule
    # anyway, which is what a scheduled task firing under it would have wanted.
    if _another_instance_is_running():
        return 1
    store.init_db()
    orch = Orchestrator()
    orch._do_scrape(trigger="manual")
    st = orch.status()
    session.shutdown()
    return 0 if st.get("session") == "ok" else 1


def run_backfill(months: int | None = None) -> int:
    """Fetch history from the command line.

    The repair path: the app runs this by itself once per install, so this is
    only for redoing a fill that failed or was cut short. Safe to repeat — a
    month already fetched reconciles to zero changes and stores nothing.
    """
    if _another_instance_is_running():
        return 1
    store.init_db()
    orch = Orchestrator()
    orch._do_backfill(months=months)
    bf = orch.status().get("backfill") or {}
    session.shutdown()
    print(f"backfill: stored {bf.get('stored', 0)} bookings over "
          f"{bf.get('done', 0)}/{bf.get('total', 0)} months, "
          f"{bf.get('failed', 0)} skipped")
    return 0 if not bf.get("failed") else 1


def run_probe() -> int:
    if _another_instance_is_running():
        return 1
    store.init_db()
    state = session.probe(headless=True)
    print(f"session: {state.state}  {state.message}")
    if state.session_id:
        print(f"apex session id: {state.session_id}")
    session.shutdown()
    return 0 if state.ok else 1


def run_selftest() -> int:
    """Prove a packaged build is intact, then exit. Written for the verifier.

    Deliberately does not start the Orchestrator: no scrape, no backfill, and
    nothing sent to LSM. The question here is whether the *bundle* shipped and
    loads, which is a question about the build rather than the session.

    Findings go to selftest.json because a windowed build has no stdout —
    print() is a silent no-op there, so a report on the console would be lost.
    """
    import json
    import urllib.request

    checks: dict[str, Any] = {}
    failures: list[str] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            failures.append(name)

    # The GUI stack. Importing the winforms backend runs
    # clr.AddReference("System.Windows.Forms") at module level, so this single
    # import proves pythonnet, the CLR and .NET interop all resolve inside the
    # bundle. It is the check that catches the most likely packaging failure.
    try:
        import webview.platforms.winforms  # noqa: F401
        record("webview.winforms", True)
    except Exception as exc:
        record("webview.winforms", False, f"{type(exc).__name__}: {exc}")

    try:
        import pystray._win32  # noqa: F401
        record("pystray.win32", True)
    except Exception as exc:
        record("pystray.win32", False, f"{type(exc).__name__}: {exc}")

    # The web assets, i.e. that datas= landed where WEB_DIR looks for them.
    # filters.js is listed because both pages load it: without it they render
    # blank, and a bundle that shipped one and not the other would pass every
    # other check in this report.
    for asset in ("calendar.html", "list.html", "filters.js"):
        path = WEB_DIR / asset
        record(f"web/{asset}", path.is_file(), str(path))

    # The Playwright driver, then a browser. Two checks, because they fail
    # independently and the second is the one that matters: an earlier build
    # passed every other check in this list and could not scrape at all,
    # because resolving the driver says nothing about whether a browser
    # starts. playwright/_impl/_transport.py forces
    # PLAYWRIGHT_BROWSERS_PATH="0" when frozen, which points at a browsers
    # directory inside the bundle that nothing ever installs into — and this
    # report called it green. Only launching one exercises the path the app
    # will really use.
    #
    # Still no network, and still nothing sent to LSM: launching a browser is
    # local, and it is never navigated anywhere.
    try:
        from playwright._impl._driver import compute_driver_executable

        resolved = compute_driver_executable()
        driver = resolved[0] if isinstance(resolved, tuple) else resolved
        record("playwright.driver", Path(str(driver)).is_file(), str(driver))
    except Exception as exc:
        record("playwright.driver", False, f"{type(exc).__name__}: {exc}")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        record("playwright.browser", True,
               os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "(unset)"))
    except Exception as exc:
        record("playwright.browser", False, f"{type(exc).__name__}: {exc}")

    try:
        store.init_db()
        record("store.init_db", True, str(DB_PATH))
    except Exception as exc:
        record("store.init_db", False, f"{type(exc).__name__}: {exc}")

    # Serve for real and fetch the calendar. A listening socket proves nothing
    # here: WEB_DIR is resolved lazily inside the route, and _wait_for_server
    # only does a TCP connect. This GET is the assertion that the UI is
    # actually reachable.
    try:
        orch = Orchestrator()   # unstarted — status() reads defaults, no worker
        threading.Thread(
            target=_serve, args=(orch,), name="selftest-web", daemon=True
        ).start()
        if _wait_for_server():
            with urllib.request.urlopen(f"{BASE_URL}/", timeout=15) as resp:
                body = resp.read().decode("utf-8", "replace")
            record("http.get_root", "Rotman Room Bookings" in body,
                   f"{len(body)} bytes from {BASE_URL}/")
        else:
            record("http.get_root", False, f"nothing listening on {BASE_URL}")
    except Exception as exc:
        record("http.get_root", False, f"{type(exc).__name__}: {exc}")

    report = {
        "ok": not failures,
        "failures": failures,
        "checks": checks,
        "frozen": bool(getattr(sys, "frozen", False)),
        "data_dir": str(DATA_DIR),
        "bundle_dir": str(BUNDLE_DIR),
        "web_dir": str(WEB_DIR),
        "web_port": WEB_PORT,
    }
    (Path(DATA_DIR) / "selftest.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    # No-op without a console, which is the normal case for the frozen build.
    print(json.dumps(report, indent=2))
    return 0 if not failures else 1


def run_app(show_window: bool = True) -> int:
    # Before touching the database or starting a worker: a second instance would
    # otherwise open the same SQLite file, run its own scrape and heartbeat
    # against LSM, and share one Chromium profile with the first.
    if _another_instance_is_running(gui=show_window):
        return 1

    store.init_db()

    orch = Orchestrator()
    orch.start()

    threading.Thread(target=_serve, args=(orch,), name="web", daemon=True).start()

    if not _wait_for_server():
        log.error("web UI failed to start on %s", BASE_URL)
        return 1
    log.info("web UI ready at %s", BASE_URL)

    if not show_window:
        # Headless: the web UI is up, keep serving until interrupted.
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            orch.stop()
        return 0

    import webview

    window = webview.create_window(
        WINDOW_TITLE,
        BASE_URL,
        width=1400,
        height=920,
        min_size=(900, 600),
        text_select=True,
    )

    def on_closing() -> bool:
        # Closing hides to tray; the app keeps scraping in the background.
        try:
            window.hide()
        except Exception:
            pass
        return False  # False cancels the close

    window.events.closing += on_closing

    tray = Tray(orch, window)

    def after_start() -> None:
        try:
            tray.start()
        except Exception:
            log.exception("tray icon failed — app continues without it")

    webview.start(
        after_start,
        private_mode=False,
        storage_path=str(DATA_DIR / "webview"),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rotman-lsm-calendar", description=APP_NAME
    )
    parser.add_argument("--scrape-once", action="store_true",
                        help="run one scrape and exit")
    parser.add_argument("--backfill", action="store_true",
                        help=f"fetch {BACKFILL_MONTHS} months of history and exit")
    parser.add_argument("--months", type=int, default=None,
                        help="how many months --backfill should reach back")
    parser.add_argument("--probe", action="store_true",
                        help="print session state and exit")
    parser.add_argument("--no-window", action="store_true",
                        help="run the web UI without the desktop window")
    parser.add_argument("--selftest", action="store_true",
                        help="check that the bundle is intact, write "
                             "selftest.json, and exit (for build verification)")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if args.probe:
        return run_probe()
    if args.scrape_once:
        return run_scrape_once()
    if args.backfill:
        return run_backfill(months=args.months)
    return run_app(show_window=not args.no_window)


if __name__ == "__main__":
    sys.exit(main())
