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
    python -m app.main --install-browser   # fetch Chromium, then exit

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
    APP_NAME, APP_VERSION, BACKFILL_MONTHS, BUNDLE_DIR, DATA_DIR, DB_PATH,
    WEB_DIR, WEB_HOST, WEB_PORT, WINDOW_TITLE, log,
)
from app.icon import paint as paint_icon
from app.scheduler import Orchestrator
from app.server import create_app

BASE_URL = f"http://{WEB_HOST}:{WEB_PORT}"


# ── Tray icon ────────────────────────────────────────────────────────────

# The mark itself lives in app/icon.py, because packaging/make-icon.py renders
# the same function to the .ico the exe and the Start-menu shortcut use. Drawn
# here rather than loaded from a file so the tray cannot be the one surface
# that goes blank when an asset is missing.
TRAY_ICON_SIZE = 64


def closing_action(quitting: bool) -> str:
    """What a window-close event should do: ``"close"`` or ``"hide"``.

    Pure, and kept out of the closure that wires it to pywebview for the same
    reason `session.login_progress` is pure: the decision is the part that can
    be wrong, and it is the part that can be tested without opening a window.

    Two different closes arrive here as the same event. The user clicking X
    should hide to tray -- the app keeps scraping, and that is the documented
    behaviour. `window.destroy()` from Tray._quit fires *this same event*, and
    that one has to be allowed through, or the quit cancels itself. Nothing in
    the event says which it is, so the caller passes that in.
    """
    return "close" if quitting else "hide"


class Tray:
    def __init__(self, orch: Orchestrator, window: Any,
                 quitting: threading.Event | None = None) -> None:
        self.orch = orch
        self.window = window
        # Shared with the window's `closing` handler, which must be able to
        # tell "the user clicked X" from "we are quitting". See _quit.
        self.quitting = quitting if quitting is not None else threading.Event()
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
            pystray.MenuItem("Check for updates", self._check_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Data Folder", self._open_data),
            pystray.MenuItem("Open in Browser", self._open_browser),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

        self._icon = pystray.Icon(
            "rotman-lsm", paint_icon(TRAY_ICON_SIZE), APP_NAME, menu
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

    def _check_update(self) -> None:
        # Guarded like the scrape: a check during a download would otherwise
        # queue behind the download's own busy window and surprise the user
        # with a result arriving minutes later.
        if self.orch.is_busy():
            self._notify("Busy", "Something else is running right now.")
            return
        self.orch.request_update_check()
        self._notify("Checking for updates", "Asking GitHub for the latest "
                                             "release…")

    def _quit(self) -> None:
        log.info("quit requested")
        # Set *before* destroy(), and the ordering is the whole point.
        # window.destroy() fires the `closing` event, and the handler that
        # hides to tray answers every close -- including this one -- by
        # returning False, which cancels it. So a quit that destroyed the
        # window before announcing itself had its own quit cancelled, and by
        # then orch.stop() and icon.stop() had already run: no window, no tray
        # icon, and nothing left that could end the process. Measured against
        # pywebview 6.2.1 -- destroy() returned in 0.01s, the handler ran,
        # webview.start() never returned.
        #
        # This has a second caller: the updater's quit hook, invoked from the
        # worker thread once a verified installer is running. Everything here
        # is already thread-safe from that direction -- pystray's own menu
        # items call _quit on the icon's thread, orch.stop() is guarded
        # against the worker joining itself, and quitting.set() is the
        # ordering this comment exists for.
        self.quitting.set()
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
    """Wait until *this app's* server answers — not until the port does.

    A bare TCP connect succeeds against whatever already holds the port, so
    on its own it proves the wrong thing: when the bind fails, the web
    thread dies, and something else owns the port, the connect "succeeds"
    and the window opens on that other process's content. What only our
    server does is answer /api/status with the session key, so that is the
    handshake.
    """
    import json
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"{BASE_URL}/api/status", timeout=2
            ) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if "session" in body:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _startup_failure_notice(gui: bool = False) -> None:
    """Say the UI never came up, where the reader will actually see it.

    The windowed build has no console, so the log line this accompanies is
    not something the person who double-clicked the exe will ever read —
    before this notice a failed start was a silent no-op. The usual cause is
    another process holding the port.
    """
    message = (
        f"{APP_NAME} could not start its window.\n\n"
        f"Another program may be using port {WEB_PORT}, or the app could "
        f"not create the UI.\n\n"
        f"Details are in the log at:\n{DATA_DIR}\\app.log\n\n"
        f"If {APP_NAME} is already running, look for its icon in the "
        f"notification area (it may be under the hidden-icons arrow)."
    )
    log.error("web UI failed to start on %s", BASE_URL)
    print(message)
    if not gui:
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    except Exception:
        log.exception("could not show the startup-failure notice")


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
    session.shutdown()
    # The exit code is the only thing Task Scheduler can see. The status
    # dict's "session" would be the wrong oracle: the probe sets it "ok"
    # before the report runs, and a scrape that fails never touches it, so a
    # failed run still looked successful here. The run row is what the run
    # actually did. "empty" is a real outcome rather than a failure — the
    # report ran and held nothing — so a scheduled empty day is not an alarm.
    last = store.last_run()
    outcome = (last or {}).get("status") or "no run recorded"
    print(f"scrape-once: {outcome}"
          + (f" — {last['message']}" if last and last.get("message") else ""))
    return 0 if outcome in ("ok", "empty") else 1


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
        # selftest.json is the artefact someone verifying a shipped build
        # reaches for, so it names the build as well as the checks it passed.
        "version": APP_VERSION,
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


def run_install_browser() -> int:
    """Put Chromium in the per-user cache, then exit.

    The one thing an installed copy cannot do for itself on a machine that has
    never run this app. Everything else it needs is inside its own folder; the
    browser is deliberately not (see the spec: bundling it would add ~150 MB to
    every copy), so a fresh machine has an empty %LOCALAPPDATA%\\ms-playwright
    and the app starts, serves the UI, and fails every scrape -- which the UI
    does not mention until someone tries to sign in.

    The installer runs this, so the failure mode it exists to prevent is "the
    app is installed and cannot work". Nothing is sent to LSM: this downloads a
    browser from Playwright's CDN and stops.

    Prints the driver's own output, so a network failure or a proxy that blocks
    the download says so in the installer's log rather than exiting 1 in
    silence.
    """
    import subprocess

    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not browsers:
        # Set at import time by app/config.py when frozen. Unfrozen it is
        # unset, and Playwright then installs to its own default, which is the
        # same directory -- so this is a note, not a failure.
        log.info("PLAYWRIGHT_BROWSERS_PATH is unset; Playwright will use its "
                 "own default directory")

    try:
        from playwright._impl._driver import compute_driver_executable
    except Exception as exc:
        log.exception("could not locate the Playwright driver")
        print(f"could not locate the Playwright driver: {exc}")
        return 1

    driver = compute_driver_executable()
    # A newer Playwright returns (node, cli.js); older ones returned just node
    # and expected `-m playwright`. Both are handled because the installed
    # version is whatever the bundle was built with, not what this file was
    # written against.
    argv = [str(p) for p in (driver if isinstance(driver, tuple) else (driver,))]
    if len(argv) > 1:
        argv = [*argv, "install", "chromium"]
    else:
        argv = [*argv, "-m", "playwright", "install", "chromium"]

    print(f"installing Chromium: {' '.join(argv)}")
    log.info("installing Chromium via %s", " ".join(argv))
    env = dict(os.environ)
    if browsers:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    try:
        completed = subprocess.run(argv, env=env, check=False)
    except OSError as exc:
        log.exception("could not run the Playwright driver")
        print(f"could not run the Playwright driver: {exc}")
        return 1
    if completed.returncode != 0:
        # A windowed build has no console, so the installer would otherwise
        # have a silent failure to report; the log is where this survives.
        log.error("Playwright exited %s installing Chromium", completed.returncode)
        print(f"Playwright exited {completed.returncode}")
        return 1

    where = browsers or "(Playwright's default browser directory)"
    log.info("Chromium is installed in %s", where)
    print(f"Chromium is installed in {where}")
    return 0


def run_app(show_window: bool = True, start_hidden: bool = False) -> int:
    # Before touching the database or starting a worker: a second instance would
    # otherwise open the same SQLite file, run its own scrape and heartbeat
    # against LSM, and share one Chromium profile with the first.
    if _another_instance_is_running(gui=show_window):
        return 1

    # The version was logged by main() before dispatching here, so the GUI
    # mode says it in the same place every other mode does.
    store.init_db()

    orch = Orchestrator()
    orch.start()

    threading.Thread(target=_serve, args=(orch,), name="web", daemon=True).start()

    if not _wait_for_server():
        # The bind may have failed against a port held by something else —
        # in which case no window should open on that process's content —
        # or the server died for another reason. Either way the user gets
        # one clear message instead of a silent no-op.
        _startup_failure_notice(gui=show_window)
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

    # hidden=True is the tray promise: the window exists -- the tray's
    # Open Calendar can show it -- but it does not arrive on the desktop at
    # login. --tray exists because before it there was no such mode: the
    # autostart shortcut's own message promised "starts minimized to the
    # tray" while the app had exactly one path, an unconditional window.
    if start_hidden:
        log.info("starting in the tray (--tray) — the window opens from "
                 "the tray's Open Calendar")

    window = webview.create_window(
        WINDOW_TITLE,
        BASE_URL,
        width=1400,
        height=920,
        min_size=(900, 600),
        text_select=True,
        hidden=start_hidden,
    )

    # Shared with Tray._quit, which sets it to say "this close is a quit, let
    # it through". Without it the handler below cancels the programmatic
    # destroy exactly as it cancels the user's X, and the process can no
    # longer be ended at all -- see Tray._quit.
    quitting = threading.Event()

    def on_closing() -> bool:
        # Closing hides to tray; the app keeps scraping in the background,
        # unless this close is the quit itself.
        if closing_action(quitting.is_set()) == "close":
            return True  # a real quit: let the close through
        try:
            window.hide()
        except Exception:
            pass
        return False  # False cancels the close

    window.events.closing += on_closing

    tray = Tray(orch, window, quitting)

    # The updater's two ways of reaching the outside world: a found update
    # announces itself through the tray, and a staged-and-verified installer
    # ends the process through the same quit path the tray's own Quit item
    # takes. Wired here rather than inside the scheduler so the worker never
    # imports the window; a check that fires before this line (none can — the
    # worker's first tick is 20 s away and the tray exists at webview.start)
    # would simply skip the toast.
    orch.set_update_hooks(
        notify=lambda title, message: tray._notify(title, message),
        quit=lambda: tray._quit(),
    )

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
    # The two window shapes are mutually exclusive: --no-window has no
    # tray icon and no window, --tray has both but keeps the window hidden
    # until asked for.
    window_mode = parser.add_mutually_exclusive_group()
    window_mode.add_argument("--no-window", action="store_true",
                             help="run the web UI without the desktop window")
    window_mode.add_argument("--tray", action="store_true",
                             help="start with the window hidden in the tray "
                                  "(what autostart passes; the tray's Open "
                                  "Calendar shows it)")
    parser.add_argument("--selftest", action="store_true",
                        help="check that the bundle is intact, write "
                             "selftest.json, and exit (for build verification)")
    parser.add_argument("--install-browser", action="store_true",
                        help="download Chromium into the per-user cache and "
                             "exit (run once after installing on a new "
                             "machine; the installer does it for you)")
    args = parser.parse_args(argv)

    # The version, in the log, at the top of every run — whichever of the
    # modes below it takes. The point of it is the machine nobody can look
    # at: a log from someone else's computer should say which build produced
    # it without being asked, and the CLI modes that run unattended
    # (--scrape-once under Task Scheduler especially) are precisely the ones
    # whose logs outlive their machines.
    log.info("%s %s starting (data: %s)", APP_NAME, APP_VERSION, DATA_DIR)

    if args.selftest:
        return run_selftest()
    if args.install_browser:
        return run_install_browser()
    if args.probe:
        return run_probe()
    if args.scrape_once:
        return run_scrape_once()
    if args.backfill:
        return run_backfill(months=args.months)
    return run_app(show_window=not args.no_window, start_hidden=args.tray)


if __name__ == "__main__":
    sys.exit(main())
