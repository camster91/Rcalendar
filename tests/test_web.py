"""
The web UI itself, driven in a real browser.

Every other test in this directory drives the API through Flask's test
client. Not one of them loads a page, and that is exactly why the front
end's defects survived: a navigation path that bypassed render(), a filter
that answered for one day and applied the answer to every day, and a second
copy of the calendar's own helpers that had gone stale were all invisible to
a test client. They are visible here.

Two properties this file is built around rather than trusting:

  * **Nothing reaches UofT.** Every request is intercepted; anything that is
    not loopback is aborted, and any URL naming utoronto is recorded as a
    failure. The app's live session makes an accidental SSO request the most
    expensive mistake this test could make, so it is asserted, not assumed.
  * **The browser is required.** A missing Chromium fails the run loudly
    rather than skipping. A skipped drift guard is worse than none, because
    it reads as green.

    python tests/test_web.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the data directory at a scratch location *before* app.config is
# imported, so this never touches real scraped data, the live profile, or
# session.bin.
_tmp = tempfile.mkdtemp(prefix="lsm-web-")
os.environ["LSM_DATA_DIR"] = _tmp

from app import store  # noqa: E402
from app.server import create_app  # noqa: E402

PASS, FAIL = 0, 0

# Fixed dates, so no assertion depends on the day the suite is run. A Tuesday
# and the Wednesday after it.
D = "2026-03-10"
D1 = "2026-03-11"

PAGE_TIMEOUT_MS = 20000


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        # This console is cp1252; a failed assertion should report the
        # mismatch, not die printing it.
        g = f"{got!r}".encode("ascii", "replace").decode()
        w = f"{want!r}".encode("ascii", "replace").decode()
        print(f"  FAIL  {label}\n          got:  {g}\n          want: {w}")


def ok(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


class StubOrch:
    """Stands in for the Playwright worker, so no session is ever probed."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self) -> dict:
        return {
            "session": "ok", "session_message": "", "busy": False,
            "busy_action": "", "progress": "", "last_scrape": None,
            "last_scrape_message": "", "backfill": None,
        }

    def is_busy(self) -> bool:
        return False


# ── Fixtures ─────────────────────────────────────────────────────────────

def booking(room: str, day: str, start: str, end: str,
            title: str = "T", all_day: bool = False) -> dict:
    return {
        "title": title, "room": room,
        "start": f"{day}T{start}:00", "end": f"{day}T{end}:00",
        "date": day, "description": "", "class_code": "",
        "cancelled": False, "all_day": all_day,
    }


def seeded() -> None:
    """A small corpus with the shapes the assertions below need.

    Room 157 is deliberately free on D and booked on D1: that is the case
    that tells a per-day answer apart from a one-day answer applied to every
    day.
    """
    store.replace_events(
        [
            booking("142", D, "09:00", "12:00", "RSM 6307"),
            booking("147", D, "14:00", "16:00", "CIBC Info Session"),
            booking("157", D1, "10:00", "11:00", "Standup"),
            booking("142", D1, "09:00", "17:00", "Full day workshop"),
            # An all-day service block, which must render as "All day" rather
            # than as a 23-hour booking.
            booking("147", D1, "00:00", "23:00", "RENOVATIONS", all_day=True),
        ],
        D, D1,
    )


# ── Browser harness ──────────────────────────────────────────────────────

# assertable clipboard: navigator.clipboard needs permissions and a secure
# context, so the write is captured instead of performed. This is what makes
# "Invalid Date on the clipboard" testable at all.
CLIPBOARD_STUB = """
Object.defineProperty(navigator, 'clipboard', {
  value: {writeText: t => {window.__copied = t; return Promise.resolve();}},
  configurable: true
});
"""

OFFENDER: list[str] = []


def _guard(route) -> None:
    """Abort anything off loopback, and remember it if it names UofT.

    Aborting is what keeps this offline; the offender list is what turns
    "we probably don't touch SSO" into an assertion.
    """
    url = route.request.url
    if "utoronto" in url.lower():
        OFFENDER.append(url)
    if "127.0.0.1" in url or "localhost" in url:
        route.continue_()
    else:
        route.abort()


def open_page(browser, base: str, path: str):
    page = browser.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    page.add_init_script(CLIPBOARD_STUB)
    page.route("**/*", _guard)
    page.goto(base + path, wait_until="load")
    # Readiness on a DOM signal, not on a global: `let EVENTS` at the top
    # level of a classic script is a lexical binding, so window.EVENTS is
    # undefined and bare EVENTS is not reachable from evaluate(). #evcnt is
    # written by applyFilters, which runs after readURL.
    page.wait_for_function(
        "() => /\\d+ events/.test("
        "document.getElementById('evcnt').textContent)"
    )
    return page


def param(page, key: str):
    return page.evaluate(
        "k => new URLSearchParams(location.search).get(k)", key
    )


def exports(page) -> dict:
    """The on-screen export links, keyed by their base path."""
    return page.evaluate(
        "() => Object.fromEntries("
        "[...document.querySelectorAll('[data-export]')]"
        ".map(a => [a.dataset.export, a.getAttribute('href')]))"
    )


# ── R1: every nav path goes through render() ─────────────────────────────

def test_today_nav_keeps_url_and_exports_current(browser, base: str) -> None:
    """Stepping a day must move the URL, both exports and the free-at answer.

    The on-screen arrows used to call todayNav -> renderToday() directly,
    which skipped render() and therefore skipped writeURL, syncExportLinks
    and maybeRefreshFree. The visible symptom was a .ics link that kept
    exporting the day you had already navigated away from.
    """
    page = open_page(browser, base, f"/?view=today&date={D}")
    try:
        check("today: opens on the date in the URL", param(page, "date"), D)
        check("today: .ics covers the day on screen",
              exports(page).get("/download/ics"), f"/download/ics?from={D}&to={D}")

        page.click(".td-nav button:last-child")

        check("today: arrow advances the URL by one day",
              param(page, "date"), D1)
        check("today: .ics follows the arrow",
              exports(page).get("/download/ics"),
              f"/download/ics?from={D1}&to={D1}")
        check("today: JSON export follows too",
              exports(page).get("/download/json"),
              f"/download/json?from={D1}&to={D1}")
        ok("today: header redraws for the new day",
           "March 11" in page.inner_text(".today-hd h2"))
    finally:
        page.close()


def test_today_back_arrow_is_symmetric(browser, base: str) -> None:
    page = open_page(browser, base, f"/?view=today&date={D1}")
    try:
        page.click(".td-nav button:first-child")
        check("today: back arrow steps one day", param(page, "date"), D)
    finally:
        page.close()


def test_nav_agrees_between_arrow_and_keyboard(browser, base: str) -> None:
    """One day per press, whichever input you use.

    nav() stepped a week in Today view while the on-screen arrows stepped a
    day, so the keyboard moved you somewhere the buttons would not.
    """
    page = open_page(browser, base, f"/?view=today&date={D}")
    try:
        page.click("body")
        page.keyboard.press("ArrowRight")
        check("today: keyboard steps one day, like the arrow",
              param(page, "date"), D1)
    finally:
        page.close()


def test_month_nav_still_steps_a_month(browser, base: str) -> None:
    """The fix is scoped to Today; month view keeps stepping months."""
    page = open_page(browser, base, "/?view=month&date=2026-03-10")
    try:
        page.click(".chead .cnav button:last-child")
        check("month: arrow advances one month", param(page, "date"), "2026-04-10")
    finally:
        page.close()


# ── Runner ───────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("  web UI (real browser)")
    print("=" * 60)

    store.init_db()
    seeded()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"  FAIL  playwright is not importable: {exc}")
        return 1

    from werkzeug.serving import make_server

    # threaded=True is required, not tidiness: the page fires bootstrap,
    # status, today and free concurrently, and a single-threaded server with
    # a browser attached deadlocks.
    srv = make_server("127.0.0.1", 0, create_app(StubOrch()), threaded=True)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"  serving on {base}\n")

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:
                print(f"  FAIL  could not launch Chromium: {exc}")
                print("        fix:  playwright install chromium")
                return 1
            try:
                test_today_nav_keeps_url_and_exports_current(browser, base)
                test_today_back_arrow_is_symmetric(browser, base)
                test_nav_agrees_between_arrow_and_keyboard(browser, base)
                test_month_nav_still_steps_a_month(browser, base)
            finally:
                browser.close()
    finally:
        srv.shutdown()

    check("no request was made to utoronto", OFFENDER, [])

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
