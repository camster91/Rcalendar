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
    python -m app.main --probe         # report session state and exit
    python -m app.main --no-window     # run headless, web UI only
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from typing import Any

from app import session, store
from app.config import (
    APP_NAME, DATA_DIR, WEB_HOST, WEB_PORT, WINDOW_TITLE, log,
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
    # threaded=True so a slow scrape never blocks the UI's status polling.
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True,
            use_reloader=False, debug=False)


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
    store.init_db()
    orch = Orchestrator()
    orch._do_scrape(trigger="manual")
    st = orch.status()
    session.shutdown()
    return 0 if st.get("session") == "ok" else 1


def run_probe() -> int:
    store.init_db()
    state = session.probe(headless=True)
    print(f"session: {state.state}  {state.message}")
    if state.session_id:
        print(f"apex session id: {state.session_id}")
    session.shutdown()
    return 0 if state.ok else 1


def run_app(show_window: bool = True) -> int:
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
    parser.add_argument("--probe", action="store_true",
                        help="print session state and exit")
    parser.add_argument("--no-window", action="store_true",
                        help="run the web UI without the desktop window")
    args = parser.parse_args(argv)

    if args.probe:
        return run_probe()
    if args.scrape_once:
        return run_scrape_once()
    return run_app(show_window=not args.no_window)


if __name__ == "__main__":
    sys.exit(main())
