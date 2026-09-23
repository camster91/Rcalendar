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
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the data directory at a scratch location *before* app.config is
# imported, so this never touches real scraped data, the live profile, or
# session.bin.
_tmp = tempfile.mkdtemp(prefix="lsm-web-")
os.environ["LSM_DATA_DIR"] = _tmp

from app import store  # noqa: E402
from app.rooms import floor_for  # noqa: E402
from app.server import create_app  # noqa: E402

PASS, FAIL = 0, 0

# Fixed dates, so no assertion depends on the day the suite is run. A Tuesday
# and the Wednesday after it.
D = "2026-03-10"
D1 = "2026-03-11"
# Outside March's grid entirely, so it never enters a free-at answer.
D2 = "2026-04-20"
# The Sunday of D's week, and the Sunday after it. Both ends of one week, which
# is where the week view's window is decided, and the day the window used to
# get wrong in both directions.
SOW = "2026-03-08"
NEXT_SOW = "2026-03-15"

PAGE_TIMEOUT_MS = 20000

# A booking title that is trying to be code. Booking titles are free text —
# anyone who books a Rotman room sets one, and this app renders them into every
# view — so this is the string a hostile, or merely careless, booking puts in
# front of every user of the calendar.
#
# Written as those literal characters rather than as the quote they spell,
# because that is the whole attack. The copy button used to interpolate
# JSON.stringify(ev) into an onclick attribute with only the double quote
# escaped, and the HTML tokenizer decodes character references inside an
# attribute *before* the result is compiled as script. So `&quot;` arrives in
# the handler as a real quote and closes the JSON string; what follows closes
# the object and the copyCard call, runs, and then comments out the remainder
# of the original expression so the handler still parses. `esc()` and
# JSON.stringify are both no help against it, which is why the payload does not
# travel in markup at all any more.
HOSTILE = "&quot;}),window.__pwned=1//"


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
    """Stands in for the Playwright worker, so no session is ever probed.

    `backfill` is settable because the history-fill toast fires on the edge of
    that field, and an edge needs two different answers to exist at all.
    `session` is settable for the same reason: the list page's badge is bound
    to it, and a badge that is bound to a fact has to be shown moving when the
    fact moves. `busy`/`busy_action`/`progress` are settable for the first-run
    card, whose whole subject is what the worker reports it is doing.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.backfill: dict | None = None
        self.session = "ok"
        self.session_message = ""
        self.busy = False
        self.busy_action = ""
        self.progress = ""
        # The worker owns the scrape message — the scheduler sets this
        # itself when a command fails — while the run row only supplies
        # the status. A stub that reported neither would test nothing.
        self.last_scrape_message = ""

    def status(self) -> dict:
        return {
            "session": self.session, "session_message": self.session_message,
            "busy": self.busy, "busy_action": self.busy_action,
            "progress": self.progress,
            "last_scrape": None,
            "last_scrape_message": self.last_scrape_message,
            "backfill": self.backfill,
        }

    def is_busy(self) -> bool:
        return False

    def request_logout(self) -> None:
        """The endpoint queues the work; the worker owns the browser profile.

        Recorded rather than performed: a real logout drives Chromium against
        LSM, which no test here may do.
        """
        self.calls.append("logout")


# The single orchestrator the server is built with, kept so a test can change
# what /api/status reports between two polls.
ORCH = StubOrch()


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

    The Auditorium is the other shape the corpus needs, and it carries two of
    them. Its name matches none of app.rooms.floor_for's patterns and it is
    not in ROOM_INFO, so it comes back with no floor at all -- the only way to
    reach the branch where the floor filter has nothing to place a room on. Its
    title is HOSTILE, because a booking title is free text that anyone who
    books a Rotman room can set, and the copy button used to compile it as
    script. Both need a booking that no count assertion depends on, which is
    why they share one: it sits outside March's grid on purpose, so it cannot
    enter a free-at answer and move those counts.

    Room 127's two bookings are the ends of D's week -- the Sunday morning and
    the Sunday after, as an all-day block. Both are needed, because the week
    view's window was wrong at both edges for the same reason, and a fix to one
    edge would leave the other broken. They are in 127 rather than a room the
    other assertions already count heavily, and they are timed so that 127 is
    *not* free at the free-at tests' 10:30 -- a room that reads as free there
    would change those counts for a booking that has nothing to do with them.
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
            # The first day of D's week, in the morning.
            booking("127", SOW, "09:00", "12:00", "Sunday morning session"),
            # The first day of the *next* week, all day, so it reaches the
            # week's all-day strip -- which is where a booking from the wrong
            # week is visible.
            booking("127", NEXT_SOW, "00:00", "23:00", "Sunday service block",
                    all_day=True),
            booking("Auditorium", D2, "09:00", "12:00", HOSTILE),
        ],
        SOW, D2,
    )


def seeded_presets() -> None:
    """Presets of the shape a hand-edited file can hold.

    load_presets type-checks the outer blob but not the values inside it, so
    these are exactly what a user file can deliver: a scalar where a list
    belongs, a comma string where an array belongs, and plain nonsense. All
    three used to reach the state variables uncoerced.
    """
    store.save_preset("Ground Floor", {"floors": "Ground Floor"})
    store.save_preset("Two rooms", {"rooms": "142,147"})
    store.save_preset("Junk", {"seats": "abc", "mins": "nonsense",
                               "free": "afternoon", "panopto": "yes"})


def seeded_groups() -> None:
    """Two groups with a deliberately awkward name.

    The list page hardcoded its group buttons, so a group that exists only in
    the config had no button at all. The apostrophe is the other half: the
    buttons used to interpolate the name into an onclick attribute, where a
    quote in the name breaks out of the string.
    """
    store.save_groups({"North": ["142"], "Dean's Suite": ["157"]})


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


def events_shown(page) -> int:
    """"5 events" -> 5. Written by applyFilters, so it tracks the filter pass."""
    return int(page.inner_text("#evcnt").split()[0])


def chip_labels(page) -> list:
    """The Active filters row, as the labels a person reads.

    The chip's own text is its first child; the ✕ is a real <button> inside it
    and would otherwise be glued onto the label by inner_text. Reading the text
    node rather than the rendered string is what makes this stable — the ✕ is
    styled by the page and could change character, but the label is the data.
    """
    return page.eval_on_selector_all(
        "#active .tag", "els => els.map(e => e.childNodes[0].textContent)")


def open_presets(page) -> None:
    """Open the filter panel, which is what loads the preset tags."""
    page.evaluate("() => openFilters()")
    page.wait_for_selector("#presetTags .tag")


def apply_preset(page, name: str):
    """Click a preset by name and return the toast in the same turn.

    Clicking and reading in one evaluate() closes the race with the toast's own
    2s timeout, and dispatching the click on the element still exercises the
    handler renderPresets wired -- which is the part that was passing the wrong
    object.
    """
    return page.evaluate(
        """(name) => {
             const t = [...document.querySelectorAll('#presetTags .tag')]
               .find(el => el.textContent.startsWith(name));
             if (!t) return null;
             t.click();
             return document.getElementById('toast').textContent;
           }""",
        name,
    )


# ── The shared module ────────────────────────────────────────────────────

def test_shared_helpers_are_served(browser, base: str) -> None:
    """The extraction is only real if the file the pages load is actually served.

    create_app is built with static_folder=None, so there is no /static and
    /filters.js is a route of its own. A 404 there would not degrade the page:
    every helper it defines would be undefined, and a page that throws while
    reading the URL comes up blank rather than partially working.

    The other half of this -- that a page must not re-declare what the module
    already declares -- needs no assertion of its own, because the collision is
    a parse-time error and every test above would fail on a page that never
    boots.
    """
    page = browser.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    page.route("**/*", _guard)
    try:
        resp = page.goto(base + "/filters.js")
        check("filters.js: served", resp.status, 200)
        ok("filters.js: is javascript",
           "javascript" in (resp.headers.get("content-type") or ""))
        ok("filters.js: carries the shared helpers",
           "function parseFilters" in resp.text()
           and "function mergeEvts" in resp.text())
    finally:
        page.close()

    page = open_page(browser, base, f"/?view=month&date={D}")
    try:
        missing = page.evaluate(
            """() => ['rcol','fmtT','fmtDur','fmtDay','esc','mergeEvts',
                      'parseFilters','PAL','DAYS','MONTHS']
                 .filter(n => typeof window[n] === 'undefined')"""
        )
        # PAL/DAYS/MONTHS are consts in the module's own scope, so they are
        # deliberately NOT window properties -- checking them would be the wrong
        # test. The functions are what the page calls.
        missing = [n for n in missing if n not in ("PAL", "DAYS", "MONTHS")]
        check("filters.js: the calendar page sees the shared functions",
              missing, [])
    finally:
        page.close()


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


# ── R3: a saved filter cannot mangle the UI ──────────────────────────────

def test_preset_names_itself_in_the_toast(browser, base: str) -> None:
    """renderPresets passed p.filters into applyPreset, which read .name off it.

    Filters have no name, so every application announced Applied "undefined".
    """
    page = open_page(browser, base, "/?view=month&date=2026-03-10")
    try:
        open_presets(page)
        text = apply_preset(page, "Two rooms")
        ok("preset: toast names the preset", "Two rooms" in (text or ""))
        ok("preset: toast is not 'undefined'", "undefined" not in (text or ""))
    finally:
        page.close()


def test_scalar_floors_does_not_blank_the_calendar(browser, base: str) -> None:
    """"Ground Floor" became a set of individual characters.

    new Set("Ground Floor") is {'G','r','o','u','n','d',' ','F','l',...}, which
    matches no room's floor, so the calendar emptied with nothing to explain it.

    Read correctly it selects the bookings in Ground Floor rooms -- 142 has
    two, 147 has two, 157 has one, 127 has two -- and leaves out the
    Auditorium, which has no recorded floor. That exclusion is the intended
    strict reading, not a regression: see test_no_floor_is_selectable for the
    other half of it.
    """
    page = open_page(browser, base, "/?view=month&date=2026-03-10")
    try:
        check("preset: the corpus is the eight seeded bookings",
              events_shown(page), 8)
        open_presets(page)
        apply_preset(page, "Ground Floor")
        check("preset: scalar floors keeps exactly the rooms that match",
              events_shown(page), 7)
    finally:
        page.close()


# ── R5: a room with no floor can be selected ─────────────────────────────

def test_no_floor_is_selectable(browser, base: str) -> None:
    """The floor filter hid unfloored rooms and offered no way to ask for them.

    app.rooms.floor_for returns "" for a room whose name matches none of its
    patterns, and the predicate mapped that to "", while floorsPresent() skipped
    falsy floors -- so such a room dropped out under any active floor filter
    with no chip that could bring it back. A state the user could neither see
    nor enter.

    The filter stays a partition: unlike capacity, which is a threshold and
    rightly lenient about an unknown, "not on the floor you picked" is the
    honest reading for a room whose floor is unrecorded. So the fix makes the
    unknown selectable rather than making it pass.
    """
    page = open_page(browser, base, "/?view=month&date=2026-03-10")
    try:
        page.evaluate("() => openFilters()")
        page.wait_for_selector("#filterBody button")
        labels = page.evaluate(
            "() => [...document.querySelectorAll('#filterBody button')]"
            ".map(b => b.textContent)"
        )
        if "No floor" not in labels:
            # Reported and returned rather than falling through to the click
            # below, which would throw on undefined and take every later test
            # down with it.
            ok("floor: a No floor chip is offered", False)
            return

        page.evaluate(
            """() => [...document.querySelectorAll('#filterBody button')]
                 .find(b => b.textContent === 'No floor').click()"""
        )
        check("floor: selecting it shows exactly the unfloored bookings",
              events_shown(page), 1)
        # The sentinel is a label rather than an empty string precisely so this
        # survives: an empty member of a comma-joined list is dropped on the way
        # back in, and the chip would silently unselect itself on reload.
        check("floor: and the URL carries it",
              param(page, "floors"), "(no floor)")
    finally:
        page.close()


def test_no_floor_survives_a_reload(browser, base: str) -> None:
    """The chip has to come back selected, not just be written to the URL."""
    page = open_page(browser, base,
                     "/?view=month&date=2026-03-10&floors=(no floor)")
    try:
        page.evaluate("() => openFilters()")
        page.wait_for_selector("#filterBody button")
        pressed = page.evaluate(
            """() => [...document.querySelectorAll('#filterBody button')]
                 .filter(b => b.getAttribute('aria-pressed') === 'true')
                 .map(b => b.textContent)"""
        )
        ok("floor: the chip is selected again on reload",
           "No floor" in pressed)
        check("floor: and the filter is still applied",
              events_shown(page), 1)
    finally:
        page.close()


def test_string_rooms_selects_those_rooms(browser, base: str) -> None:
    """rooms:"142,147" is a string, and strings have no .filter.

    That threw inside applyPreset, leaving the state half-applied with only a
    console error to show for it.
    """
    page = open_page(browser, base, "/?view=month&date=2026-03-10")
    try:
        open_presets(page)
        apply_preset(page, "Two rooms")
        # 142 has two bookings, 147 has two, and 157's is excluded.
        check("preset: string rooms selects exactly those rooms",
              events_shown(page), 4)
    finally:
        page.close()


def test_nonsense_values_apply_as_no_opinion(browser, base: str) -> None:
    """A junk value must not silently switch a filter ON.

    panopto:"yes" is not 1, so it is off; free:"afternoon" is not a clock time,
    so it is off rather than sent to the server as one.
    """
    page = open_page(browser, base, "/?view=month&date=2026-03-10")
    try:
        total = events_shown(page)
        open_presets(page)
        apply_preset(page, "Junk")
        check("preset: nonsense values filter nothing", events_shown(page), total)
        check("preset: nonsense free-at stays off", param(page, "free"), None)
        check("preset: nonsense panopto stays off", param(page, "panopto"), None)
    finally:
        page.close()


# ── R2: free-at is a per-day answer ──────────────────────────────────────

def wait_for_free(page) -> None:
    """Open the panel, which is what builds #freeNote, then wait for an answer.

    The note only exists once the panel body has been built, so a cold link
    carrying ?free= has no element to write to until then -- which is why the
    page looks it up on each write rather than capturing it.
    """
    page.evaluate("() => openFilters()")
    page.wait_for_function(
        "() => /free from|Could not check/.test("
        "(document.getElementById('freeNote')||{}).textContent || '')"
    )


def test_free_at_filters_each_booking_by_its_own_day(browser, base: str) -> None:
    """The filter asked about one day and applied the answer to every day.

    At 10:30 for 60 min: room 147 is free on the 10th and booked all day on the
    11th, while 157 is free on the 10th and booked on the 11th. One day's
    answer applied across the month has to get one of them wrong, so the count
    below cannot be reached by accident.

    Per-day, exactly one booking survives: 147's 14:00 slot on the 10th. Every
    other booking either sits on the 11th, or is a room that is booked at that
    time on its own day.
    """
    page = open_page(browser, base,
                     "/?view=month&date=2026-03-10&free=10:30&mins=60")
    try:
        wait_for_free(page)
        check("free-at: month view filters each booking by its own day",
              events_shown(page), 1)
    finally:
        page.close()


def test_free_at_is_per_day_for_a_single_day_window(browser, base: str) -> None:
    """The same question over a one-day window gives the same answer.

    #evcnt counts every booking that passes the filters, not only the ones the
    current view draws, so the two views report the same number here. What
    differs is the window the answer was fetched for -- one day against the
    month grid's 35 -- so this pins that the answer is per-day at both sizes,
    and that a window of one day is not a special case that skips the lookup.
    """
    page = open_page(browser, base,
                     "/?view=today&date=2026-03-10&free=10:30&mins=60")
    try:
        wait_for_free(page)
        check("free-at: a one-day window filters per day too",
              events_shown(page), 1)
    finally:
        page.close()


def test_free_at_clears_the_rooms_booked_that_day(browser, base: str) -> None:
    """Room 142 is booked 09:00-12:00 on the 10th, so it is not free at 10:30.

    A guard against the opposite failure: a predicate that let everything
    through would also pass the counts above if the corpus were smaller.
    """
    page = open_page(browser, base,
                     "/?view=today&date=2026-03-10&free=10:30&mins=60")
    try:
        wait_for_free(page)
        rooms = page.evaluate(
            "() => [...document.querySelectorAll('.evcard .eroom')]"
            ".map(el => el.textContent)"
        )
        ok("free-at: the booked room is gone", not any("142" in r for r in rooms))
        ok("free-at: the free room is still there", any("147" in r for r in rooms))
    finally:
        page.close()


def test_free_at_note_describes_the_span(browser, base: str) -> None:
    """The note said "on <one date>" while answering for a whole month.

    With a per-day answer the note cannot name a single day, and saying so is
    the difference between a filter that explains itself and one that does not.
    """
    page = open_page(browser, base,
                     "/?view=month&date=2026-03-10&free=10:30&mins=60")
    try:
        wait_for_free(page)
        note = page.text_content("#freeNote") or ""
        ok("free-at: the month note describes the span", "every day shown" in note)
        ok("free-at: and does not claim one date", "2026-03-10" not in note)
    finally:
        page.close()


# ── list.html ────────────────────────────────────────────────────────────
# The secondary page was a stale fork of the calendar that predated several
# fixes, which is why it is tested at all: nothing loaded it, so nothing
# noticed. These cover the defects that fork had.

def test_list_has_a_url_and_the_links_carry_it(browser, base: str) -> None:
    """list.html had no writeURL at all.

    The URL never changed as you filtered, so it could not be copied or
    bookmarked, and both export links and the tab across to the calendar were
    fixed hrefs — a .ics download ignored every filter you had set.
    """
    page = open_page(browser, base, "/list")
    try:
        check("list: opens with an empty query", param(page, "rooms"), None)
        page.select_option("#roomFilter", "142")
        check("list: selecting a room writes it to the URL",
              param(page, "rooms"), "142")
        check("list: the .ics link carries the filter",
              exports(page).get("/download/ics"), "/download/ics?rooms=142")
        check("list: and so does the tab back to the calendar",
              page.eval_on_selector("#tab-cal", "el => el.getAttribute('href')"),
              "/?rooms=142")
    finally:
        page.close()


def test_list_badge_reads_the_session(browser, base: str) -> None:
    """list.html asserted "● Live" as a string literal, for as long as it was open.

    The file never called /api/status at all — a grep for it returned only that
    one badge line. So the secondary page claimed a live session while the
    calendar's badge, looking at the same session, correctly read "Session
    expired" — and the bookings underneath it had stopped updating. That is the
    same class as the three fixes in 5070386: a screen describing a state that
    is no longer there, on the one page with no test covering its header.
    """
    page = open_page(browser, base, "/list")
    try:
        def badge() -> str:
            return page.inner_text("#sessBadge")

        def colour() -> str:
            return page.eval_on_selector("#sessBadge", "el => el.className")

        def poll() -> None:
            """Drive the poll rather than waiting out its 60s interval.

            The interval is the calendar's cadence, and a test that slept for
            it would assert nothing the interval itself needed.
            """
            page.evaluate("async () => { await loadStatus(); }")

        # The stub worker is signed in, so the page must say so — in the
        # calendar's words, or the two pages describe one session differently.
        poll()
        check("list: a live session is reported, in the calendar's words",
              badge(), "● Session active")
        ok("list: ...in the live colour", "bg-gn" in colour())

        ORCH.session = "expired"
        poll()
        check("list: an expired session stops claiming live",
              badge(), "● Session expired")
        ok("list: ...and turns red rather than staying green", "bg-rd" in colour())

        ORCH.session = "unknown"
        ORCH.session_message = "No browser profile"
        poll()
        check("list: an unknown session says what it does know",
              badge(), "● No browser profile")
        ok("list: ...in the warning colour", "bg-og" in colour())

        # The markup itself, which no assertion above can pin: the behaviour
        # tests all pass if the page simply never writes a wrong thing, and a
        # literal "Live" badge is a claim the page makes by not speaking. So
        # read what the server actually sends — parsed, and read off the badge
        # element, because a search for the string through the raw file would
        # find the comment that explains this very change.
        served = page.evaluate(
            """async () => {
                 const html = await (await fetch('/list')).text();
                 const doc = new DOMParser().parseFromString(html, 'text/html');
                 const el = doc.getElementById('sessBadge');
                 return el === null ? null : el.textContent;
               }"""
        )
        check("list: the served badge exists to be written through",
              served is not None, True)
        ok("list: the served page no longer hardcodes a live badge",
           "Live" not in (served or ""))
    finally:
        ORCH.session = "ok"
        ORCH.session_message = ""
        page.close()

    # A status call that fails must not leave the last good reading standing.
    # A badge that said "active" once and then went quiet is the original
    # defect in slower motion, so the failure has to be visible as failure.
    # Both routes are registered here rather than through open_page because the
    # abort has to be in place before the first poll; Playwright matches the
    # most recently added route first, so the specific one wins over the guard.
    dead = browser.new_page()
    dead.set_default_timeout(PAGE_TIMEOUT_MS)
    dead.route("**/*", _guard)
    dead.route("**/api/status", lambda route: route.abort())
    try:
        dead.goto(base + "/list", wait_until="load")
        dead.wait_for_selector("#sessBadge")
        # Which of the two neutral states is showing depends on whether the
        # rejected fetch has settled yet, so this asserts the property that
        # holds either way: the page does not claim a session it could not read.
        text = dead.inner_text("#sessBadge")
        cls = dead.eval_on_selector("#sessBadge", "el => el.className")
        ok("list: a failed status call does not claim a live session",
           "active" not in text)
        ok("list: ...and does not show the live colour", "bg-gn" not in cls)
    finally:
        dead.close()


def test_list_group_filter_reaches_the_download(browser, base: str) -> None:
    """The one filter on the list page the download could not see.

    The list page keeps rooms and groups as two separate filters — one
    mechanism each, which is what stopped them ANDing to nothing — so unlike
    the calendar it carries the group *name* in its links instead of expanding
    it into rooms. Nothing turned a name into rooms, so a .ics taken while
    filtered to a group held every booking in the database. The link and what
    it returns are asserted together, because from the page those two look
    identical when the parameter is right and the server ignores it.
    """
    page = open_page(browser, base, "/list")
    try:
        page.evaluate("() => toggleGroup('North')")
        check("list: the export link names the group",
              exports(page).get("/download/ics"), "/download/ics?groups=North")

        # The set the page is showing, against the set the link returns. Counts
        # rather than a room list, because "the download holds what is on
        # screen" is the property, not "the download holds room 142".
        shown = events_shown(page)
        got = page.evaluate(
            """async () => {
                 const a = document.querySelector('[data-export="/download/json"]');
                 const d = await (await fetch(a.getAttribute('href'))).json();
                 return d.events.length;
               }"""
        )
        check("list: the download holds the bookings the page is showing",
              got, shown)
        ok("list: ...which is the group's rooms, not every booking",
           shown < 8)
    finally:
        page.close()


def test_sign_out_is_offered_only_with_a_session(browser, base: str) -> None:
    """POST /api/logout existed, and nothing called it — no page, no tray item.

    So the app held a live UofT session with no way for the user to drop it.
    Wiring it up means answering two questions the UI has to get right: it is
    offered only while there is a session to end, and it asks before ending one,
    because the session is the thing the whole app runs on and getting it back
    costs a UTORid login and a Duo approval.
    """
    page = open_page(browser, base, "/")
    dialogs: list[str] = []

    def dismiss(dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    def accept(dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    try:
        page.on("dialog", dismiss)

        # The inline style, not the computed one: it is the property loadStatus
        # writes, so this reads the code's own decision rather than whatever the
        # renderer made of it.
        def shown(sel: str) -> str:
            return page.eval_on_selector(sel, "el => el.style.display")

        check("sign-out is offered while the session is live",
              shown("#logoutLink"), "block")
        check("...and sign-in is not, since there is nothing to sign in to",
              shown("#loginBtn"), "none")

        # Dismissing the confirm must not touch the endpoint. This is the safety
        # property: a stray click must not be able to end the app's session.
        ORCH.calls.clear()
        page.click("#logoutLink")
        check("dismissing the confirm asks nothing of the app", ORCH.calls, [])
        ok("...and the question says what signing out would cost",
           bool(dialogs) and "Duo" in dialogs[-1] and "UTORid" in dialogs[-1])

        # Accepting it does, and the request lands. Playwright calls every
        # registered dialog listener, so the dismissing one is removed first or
        # it would close the second dialog before the accepting one saw it.
        page.remove_listener("dialog", dismiss)
        page.on("dialog", accept)
        ORCH.calls.clear()
        page.click("#logoutLink")
        deadline = time.time() + 5
        while time.time() < deadline and not ORCH.calls:
            time.sleep(0.1)
        check("accepting it reaches the endpoint", ORCH.calls, ["logout"])

        # With the session gone there is nothing to sign out of, so the two
        # controls swap: one is always the action that can actually be taken.
        ORCH.session = "expired"
        page.evaluate("async () => { await loadStatus(); }")
        check("sign-out is withdrawn once the session is gone",
              shown("#logoutLink"), "none")
        check("...and sign-in takes its place", shown("#loginBtn"), "block")
    finally:
        ORCH.session = "ok"
        ORCH.calls.clear()
        page.close()


def test_list_select_and_tags_are_one_filter(browser, base: str) -> None:
    """They were two filters that ANDed, so combining them selected nothing.

    The <select> ANDed against the OR'd room tags. Picking room 142 in the
    dropdown and room 157 from the search results asked for bookings in 142 and
    in 157 — which is no booking at all. There is one selection now, and both
    controls write it.
    """
    page = open_page(browser, base, "/list")
    try:
        check("list: the corpus is the eight seeded bookings",
              events_shown(page), 8)
        page.select_option("#roomFilter", "142")
        check("list: the dropdown narrows to that room", events_shown(page), 2)

        page.evaluate("() => addTag('room','157')")
        check("list: picking another room moves the selection, not ANDs it",
              events_shown(page), 1)
        check("list: the dropdown follows the tag",
              page.eval_on_selector("#roomFilter", "el => el.value"), "157")
    finally:
        page.close()


def test_list_groups_come_from_the_config(browser, base: str) -> None:
    """The group buttons were hardcoded, so a configured group had none.

    Built from GROUPS as DOM nodes with listeners. The apostrophe in the seeded
    group name is the reason for that: the old buttons interpolated the name
    into an onclick attribute, where a quote in it ends the attribute.
    """
    page = open_page(browser, base, "/list")
    try:
        labels = page.evaluate(
            "() => [...document.querySelectorAll('#gbar .gb')]"
            ".map(b => b.textContent)"
        )
        ok("list: every configured group has a button",
           "North" in labels and "Dean's Suite" in labels)
        ok("list: plus the All button", "All" in labels)

        page.evaluate(
            """() => [...document.querySelectorAll('#gbar .gb')]
                 .find(b => b.textContent === 'North').click()"""
        )
        check("list: a group button filters to its rooms",
              events_shown(page), 2)
    finally:
        page.close()


def test_list_renders_an_all_day_block_as_all_day(browser, base: str) -> None:
    """Its cardHTML had no all-day branch, unlike the calendar's.

    Without it a service block printed "12:00 a.m. → 11:00 p.m. (23h)", which
    reads as a real 23-hour booking.
    """
    page = open_page(browser, base, "/list")
    try:
        when = page.evaluate(
            """() => {
                 const c = [...document.querySelectorAll('.evcard')]
                   .find(el => el.textContent.includes('RENOVATIONS'));
                 return c ? c.querySelector('.etime').textContent.trim() : null;
               }"""
        )
        check("list: an all-day block says All day", when, "All day")
    finally:
        page.close()


def test_list_copies_a_real_date(browser, base: str) -> None:
    """Its fmtDay had no full-timestamp guard, so the clipboard got "Invalid Date".

    ev.start is a full ISO timestamp, and appending T12:00:00 to one produces
    "Invalid Date" — which is what copyCard was putting on the clipboard. The
    guard lives in the shared fmtDay now.
    """
    page = open_page(browser, base, "/list")
    try:
        page.evaluate(
            """() => copyCard({room:'142', title:'RSM 6307', description:'',
                 start:'2026-03-10T09:00:00', end:'2026-03-10T12:00:00'})"""
        )
        copied = page.evaluate("() => window.__copied") or ""
        ok("list: the copied card is not 'Invalid Date'", "Invalid" not in copied)
        ok("list: and names the day", "Mar 10" in copied)
    finally:
        page.close()


def test_a_hostile_title_is_not_code(browser, base: str) -> None:
    """A booking title is data, and it has to stay data on the copy button.

    The button carried its payload as JSON.stringify(ev) inside an onclick
    attribute, with only the double quote escaped. HOSTILE documents why that
    is not enough: the markup decoder turns `&quot;` back into a quote before
    the handler is compiled, so the title closes the JSON string and the rest
    of it runs — in this page's origin, which on a loopback app with no
    authentication is the entire control plane.

    The assertion is on the effect rather than on the markup, and it is a real
    click on a real rendered card, because that is the only version of this
    that proves the payload could not run: a test that read the attribute back
    would pass against the broken code too.
    """
    # Both pages, because both built the same attribute. On the calendar the
    # card is reached by navigating to the day it is booked on, so this
    # exercises the whole render path; on /list every booking is in range.
    for path in (f"/?view=today&date={D2}", "/list"):
        page = open_page(browser, base, path)
        try:
            card = page.evaluate(
                f"""() => {{
                     const c = [...document.querySelectorAll('.evcard')]
                       .find(el => el.textContent.includes('__pwned'));
                     return c ? true : false;
                   }}"""
            )
            ok(f"hostile: the card renders on {path}", card)

            page.evaluate(
                """() => {
                     const c = [...document.querySelectorAll('.evcard')]
                       .find(el => el.textContent.includes('__pwned'));
                     c.querySelector('.ecopy').click();
                   }"""
            )
            check(f"hostile: the title never ran on {path}",
                  page.evaluate("() => window.__pwned"), None)
            copied = page.evaluate("() => window.__copied") or ""
            ok(f"hostile: and the title reaches the clipboard verbatim ({path})",
               HOSTILE in copied)
        finally:
            page.close()


def test_week_view_owns_all_seven_of_its_days(browser, base: str) -> None:
    """The week ran from Sunday noon to the next Sunday noon.

    The window was `new Date(curDate)` with the date rolled back to Sunday, and
    that keeps curDate's *time of day* — which the URL sets to noon, so the URL's
    date is unambiguous. But inWeek compares instants, so the week did not
    contain its own Sunday morning: a Sunday booking before noon fell outside
    its own week. An all-day row starts at midnight, so a Sunday service block
    left the all-day strip entirely, and the strip is where a booking from the
    wrong week is visible.

    The far bound had the mirror fault — it ran to the next Sunday at noon, so
    that Sunday's morning was attributed to the week before it. Both edges are
    asserted, because a fix to one leaves the other wrong.
    """
    # D1 is a Wednesday, whose week begins on SOW. 09:00 is inside the grid's
    # 07:00-22:00 span, so a booking that is in the week renders a bar.
    page = open_page(browser, base, f"/?view=week&date={D1}")
    try:
        bars = page.evaluate(
            f"""(d) => document.querySelectorAll(
                     '.wkc[data-date="' + d + '"] .wkev').length""", SOW
        )
        check("week: the Sunday of the week carries its morning booking",
              bars, 1)

        strip = page.evaluate(
            "() => {const r=document.querySelector('.wkadrow');"
            "return r ? r.textContent : '';}"
        )
        ok("week: the week's own all-day block is in the strip",
           "RENOVATIONS" in strip)
        ok("week: and the next week's is not",
           "Sunday service block" not in strip)
    finally:
        page.close()

    # Navigating onto the next Sunday must then show that block, or the
    # assertion above is satisfied by dropping the booking everywhere.
    page = open_page(browser, base, f"/?view=week&date={NEXT_SOW}")
    try:
        strip = page.evaluate(
            "() => {const r=document.querySelector('.wkadrow');"
            "return r ? r.textContent : '';}"
        )
        ok("week: the next Sunday's block is in its own week",
           "Sunday service block" in strip)
    finally:
        page.close()


def test_nav_steps_the_view_you_are_looking_at(browser, base: str) -> None:
    """One press moves one unit of the current view, and lands on a real month.

    Two faults in one line. setMonth alone overflows: 31 January plus one month
    is 31 February, which JS rolls forward to 3 March, so stepping forward from
    the 31st skipped February on screen. And the rule named only Month and
    Today, so Week fell to the else branch and its arrows stepped a whole month
    — three weeks of bookings skipped per press.
    """
    def heading() -> str:
        return page.evaluate(
            "() => document.querySelector('#mainArea .chead h2').textContent"
        )

    # Month view from the 31st: the step must be February, not March.
    page = open_page(browser, base, "/?view=month&date=2026-01-31")
    try:
        check("nav: the month view starts in January", heading(), "January 2026")
        page.keyboard.press("ArrowRight")
        check("nav: one month forward from the 31st is February",
              heading(), "February 2026")
        page.keyboard.press("ArrowLeft")
        check("nav: and back again is January", heading(), "January 2026")
    finally:
        page.close()

    # Week view: one press is one week, and it lands on the next Sunday.
    page = open_page(browser, base, f"/?view=week&date={D1}")
    try:
        ok("nav: the week view starts on the week's Sunday",
           "March 8" in heading())
        page.keyboard.press("ArrowRight")
        ok("nav: one press forward is one week", "March 15" in heading())
        ok("nav: ...and ends the following Saturday", "March 21" in heading())
        page.keyboard.press("ArrowLeft")
        ok("nav: and back again is the week before",
           "March 8" in heading() and "March 14" in heading())
    finally:
        page.close()


def test_list_reads_the_calendars_link(browser, base: str) -> None:
    """A link from the calendar lands on the same bookings, and on the same count.

    The calendar's tab now carries rooms/groups/q, and this page reads them
    through the same parseFilters and applies them with the same AND-across /
    OR-within rule, so the two cannot disagree about what a filter means. That
    rule was the other half of the select-vs-tags defect: this page OR'd
    everything together, so a room and a search term widened each other instead
    of narrowing.
    """
    page = open_page(browser, base, "/list?rooms=142,147")
    try:
        # Two rooms is either, so this is 142's two bookings plus 147's two.
        check("list: two rooms select either", events_shown(page), 4)
    finally:
        page.close()

    page = open_page(browser, base, "/list?rooms=142,147&q=rsm")
    try:
        # The search text is a filter, so it narrows the rooms rather than
        # widening them: only 142's "RSM 6307" matches.
        check("list: the search text narrows the rooms",
              events_shown(page), 1)
        check("list: the search text is in the box",
              page.eval_on_selector("#search", "el => el.value"), "rsm")
    finally:
        page.close()

    # The same URL on the calendar must give the same answer, or a copied link
    # means two different things depending on which page opens it.
    page = open_page(browser, base, "/?rooms=142,147&q=rsm")
    try:
        check("calendar: the same link gives the same answer",
              events_shown(page), 1)
    finally:
        page.close()


# ── Runner ───────────────────────────────────────────────────────────────

# Listed rather than called one by one so the run can report a crash as a
# failure. A test that throws would otherwise end the run, skip every test
# after it and print no summary -- a suite that looks truncated, not failed.
# ── The chips, the group names, and the history-fill toast ───────────────

def group_names(page) -> str:
    """The working copy's group names, as one comparable string."""
    return page.evaluate("() => Object.keys(GEDIT).sort().join('|')")


def rename_group(page, old: str, new: str):
    """Type a name into a group's field the way a person does.

    An `input` per change and a `blur` at the end, because that is the event
    sequence the field really sees. A test that only blurred would pass against
    the live-commit version too, by never firing the handler that did the
    damage.
    """
    return page.evaluate(
        """([old, newName]) => {
             const inp = [...document.querySelectorAll('.fgrp-hd input')]
               .find(i => i.value === old);
             if (!inp) return null;
             inp.value = newName;
             inp.dispatchEvent(new Event('input', {bubbles: true}));
             inp.dispatchEvent(new Event('blur', {bubbles: true}));
             return document.getElementById('toast').textContent;
           }""",
        [old, new],
    )


def test_chips_do_not_outlive_the_filters_they_name(browser, base: str) -> None:
    """A chip names the filter in force now, whoever put that filter there.

    The row used to be a history: addTag appended a chip, and readURL and
    applyPreset replaced the whole filter without clearing it, so the row could
    name a filter that no longer existed. removeTag then acted on the live
    state, so removing a stale room chip widened a two-room preset to every
    room in the building — which reads as the preset not working rather than
    as a stale chip.

    The row is derived from the state now, so a stale chip cannot exist. What
    is asserted is that it *follows the filter across both paths*, which is the
    stronger property and the one that would have caught the bug: "no chips"
    passes just as well when the row has stopped rendering altogether.
    """
    page = open_page(browser, base, f"/?view=month&date={D}")
    try:
        page.evaluate("() => addTag('room', '142')")
        check("chips: adding a room shows one chip", chip_labels(page), ["Room 142"])
        check("chips: ...and the badge counts it",
              page.inner_text("#advN"), "· 1")
        check("chips: ...and the filter followed it", events_shown(page), 2)

        open_presets(page)
        apply_preset(page, "Two rooms")

        check("chips: the preset's rooms are the filter",
              page.evaluate("() => [...activeRooms].sort().join(',')"),
              "142,147")
        check("chips: ...and its bookings are shown", events_shown(page), 4)
        # The room chip is not left standing next to the preset's rooms: the
        # row is rebuilt from the preset, so what is on screen names what is on.
        check("chips: the row names the filter the preset installed",
              chip_labels(page), ["2 rooms"])
    finally:
        page.close()


def test_a_renamed_group_does_not_eat_its_neighbour(browser, base: str) -> None:
    """Group names are the working copy's keys, so a rename is an assignment.

    Typing a name that already exists does not create a second group — it
    assigns the renamed group's rooms over the existing one and deletes the old
    key, throwing the other group's rooms away with nothing said. The live
    commit is what made it reachable: the write happened as soon as the field
    read a name that existed, so renaming a group to "Dean's Suite" destroyed
    Dean's Suite the moment the field spelled it out. Refusing the collision
    instead does not work either, because the name is committed before it is
    finished and the field can then never be typed past "Dean".

    Committing on blur is what makes a finished name distinguishable from a
    half-typed one, which is what lets the collision be named and the field put
    back. The last case is the control: a guard that refused every rename would
    pass everything above.
    """
    page = open_page(browser, base, f"/?view=month&date={D}")
    try:
        open_presets(page)
        check("rename: both seeded groups are in the editor",
              group_names(page), "Dean's Suite|North")

        text = rename_group(page, "North", "Dean's Suite")
        ok("rename: the collision is named, not silent",
           "already exists" in (text or ""))
        check("rename: nothing was merged or lost",
              group_names(page), "Dean's Suite|North")
        check("rename: North keeps its own rooms",
              page.evaluate("() => (GEDIT['North'] || []).join(',')"), "142")
        check("rename: Dean's Suite keeps its own",
              page.evaluate("""() => (GEDIT["Dean's Suite"] || []).join(',')"""),
              "157")
        check("rename: the field shows the name the group still has",
              page.evaluate(
                  "() => [...document.querySelectorAll('.fgrp-hd input')]"
                  ".map(i => i.value).sort().join('|')"),
              "Dean's Suite|North")

        # The other way the working copy could stop being a set of names.
        text = rename_group(page, "North", "   ")
        ok("rename: a blank name is refused", "needs a name" in (text or ""))
        check("rename: ...and nothing is renamed to nothing",
              group_names(page), "Dean's Suite|North")

        # The control: a guard that refused every rename would pass all of the
        # above and break the feature.
        rename_group(page, "North", "North Wing")
        check("rename: a free name is taken",
              group_names(page), "Dean's Suite|North Wing")
        check("rename: ...and the rooms move with it",
              page.evaluate("() => (GEDIT['North Wing'] || []).join(',')"),
              "142")
    finally:
        page.close()


def test_history_fill_toast_reports_the_fill_not_the_edge(browser, base: str) -> None:
    """The toast fired on the falling edge of `running`, which is not success.

    A backfill stops running when it errors, and when its reach was too short
    to settle the fill. Announcing "History filled" there is a claim the reader
    cannot check: the months that were never fetched look like quiet months, so
    the lie is invisible in the calendar the toast is about. The claim now comes
    from backfill_done — the same fact the sidebar below reports from.

    Both directions are asserted, because a toast that always said "failed"
    would be as wrong as one that always said "filled".
    """
    page = open_page(browser, base, f"/?view=month&date={D}")
    try:
        check("history: no backfill has landed yet",
              store.backfill_done(), False)

        def poll(running: bool) -> str:
            """Set what the worker reports, poll, and read the toast.

            The toast's text is read rather than its visibility: the status
            poll also runs on a 60s interval, and a toast either of them
            raised carries the same message, so reading the text rather than
            counting firings keeps the assertion about what was said.
            """
            ORCH.backfill = {"running": running}
            return page.evaluate(
                """async () => { await loadStatus();
                     return document.getElementById('toast').textContent; }"""
            )

        # A run starts, then stops with nothing to show for it.
        poll(True)
        text = poll(False)
        ok("history: the stop is reported, not celebrated",
           (text or "").startswith("⚠️ History fill"))
        ok("history: ...and it does not claim the history is filled",
           "✅" not in (text or ""))

        # The control: once the fill really has landed, that same edge is a
        # success. backfill_done reads the run rows, so one is written here —
        # and removed again rather than left behind for the tests after this.
        floor = store._first_of_month_months_ago(
            store.BACKFILL_MONTHS, datetime.now().isoformat()
        )
        run_id = store.start_run("backfill")
        store.finish_run(run_id, "ok", events_count=5,
                         date_from=floor.strftime("%d/%m/%Y"),
                         date_to=datetime.now().strftime("%d/%m/%Y"))
        try:
            check("history: a completed fill is recorded",
                  store.backfill_done(), True)
            poll(True)
            text = poll(False)
            ok("history: a finished fill is announced",
               "✅ History filled" in (text or ""))
        finally:
            store._conn().execute("DELETE FROM scrape_runs WHERE id = ?",
                                  (run_id,))
            store._conn().commit()
            check("history: the run row is cleaned up",
                  store.backfill_done(), False)

        # The shape the fix is about: a fill that ran, reached the floor,
        # stored a page of every month, and could not download the report. It
        # is still owed, so the sidebar must not call it filled — and the
        # months it read hold real bookings, so nothing else on screen marks
        # the fill as a fraction of one. The pending line has to carry the
        # run's own note or the reader has no way to tell why it came back.
        ORCH.backfill = {"running": False}
        run_id = store.start_run("backfill")
        store.finish_run(run_id, "partial", events_count=5,
                         date_from=floor.strftime("%d/%m/%Y"),
                         date_to=datetime.now().strftime("%d/%m/%Y"),
                         message="5 bookings over 11 months "
                                 "(11 month(s) only partly read)")
        try:
            check("history: a partly-read fill does not settle the gate",
                  store.backfill_done(), False)
            line = page.evaluate(
                """async () => { await loadStatus();
                     return document.getElementById('histInfo').textContent; }"""
            )
            ok("history: the sidebar does not call a partial fill filled",
               "Filled" not in (line or ""))
            ok("history: ...it says the fill is still pending",
               "History fill pending" in (line or ""))
            ok("history: ...and says why, in the run's own words",
               "only partly read" in (line or ""))
        finally:
            store._conn().execute("DELETE FROM scrape_runs WHERE id = ?",
                                  (run_id,))
            store._conn().commit()
    finally:
        ORCH.backfill = None
        page.close()


def test_a_fresh_install_says_what_it_is_doing(browser, base: str) -> None:
    """An empty calendar must not look the same broken as it does loading.

    On a fresh install the store is empty and the month grid renders as a
    bare grid of days — indistinguishable from an app that failed to load
    anything, which is exactly the ambiguity a first-run reader hits: is it
    working, or is it broken? The main area now carries a card while the
    store is empty, answering with one of the only honest answers there are
    — still loading, fetching (with the worker's live progress line),
    waiting for a sign-in the app cannot do for itself, or the fetch failed.

    Every state is asserted on the DOM, never on computed style: the moving
    dot is a CSS animation and headless Chromium freezes those (see the
    frozen-transitions note), so motion is asserted by the dot's *presence*
    and by the progress line being text. And the card must vanish the
    moment the store holds anything — a card that outlived the data would be
    a new way to look broken.
    """
    page = open_page(browser, base, f"/?view=month&date={D}")
    try:
        # The fresh-install state: an empty store, by the same direct route
        # the corpus restore uses, so the page sees what a first run sees.
        conn = sqlite3.connect(store.DB_PATH)
        try:
            conn.execute("DELETE FROM events")
            conn.commit()
        finally:
            conn.close()
        page.reload(wait_until="load")
        page.wait_for_function(
            "() => /\\d+ events/.test("
            "document.getElementById('evcnt').textContent)"
        )
        check("fresh: the store is empty on this page",
              events_shown(page), 0)

        # No session: the card says why there is nothing and offers the one
        # action that changes it — the sidebar button exists, but a first-run
        # reader is looking at the calendar, not the Status block.
        ORCH.session = "expired"
        page.evaluate("async () => { await loadStatus(); }")
        ok("fresh: an empty store with no session shows the card, not a bare grid",
           page.locator("#bootCard").count() == 1)
        ok("fresh: ...it says what a sign-in gets, not just that one is needed",
           "year of bookings" in page.inner_text("#bootCard"))
        ok("fresh: ...and carries the sign-in button itself",
           "Sign in to LSM" in page.inner_text("#bootCard"))

        # A filter change re-renders the main area through applyFilters —
        # the one path that could redraw the bare grid while the store is
        # still empty. The card has to survive it: a calendar that looks
        # empty-and-broken again the moment a first-run reader touches
        # anything is the ambiguity this card exists to remove. Applying a
        # preset is the direct way through applyFilters, with no chip-click
        # behaviour to depend on.
        open_presets(page)
        apply_preset(page, "Ground Floor")
        ok("fresh: the card survives a filter change on an empty store",
           page.locator("#bootCard").count() == 1)

        # Busy: the card reports the worker, with its progress where the eye
        # lands. This is the state the complaint was about — a first run
        # takes minutes, and nothing else on screen moves.
        ORCH.session = "ok"
        ORCH.busy = True
        ORCH.busy_action = "Backfilling history (first run)"
        ORCH.progress = "Backfill 3/11: reading 01/09/2025"
        page.evaluate("async () => { await loadStatus(); }")
        card_text = page.inner_text("#bootCard")
        ok("fresh: a busy worker names its work on the card",
           "Backfilling history (first run)" in card_text)
        ok("fresh: ...with the live progress line, which is what moves",
           "Backfill 3/11" in card_text)
        ok("fresh: ...and the motion marker is a class, never a computed style",
           page.locator("#bootCard .bdot").count() == 1)

        # Failed: the last scrape's error is said on the card, not left to
        # the small sidebar text. The status comes from the store's run
        # row, the words from the worker that failed (see the stub), so one
        # of each is staged — and the row removed again rather than left
        # behind for the tests after this.
        ORCH.busy = False
        ORCH.last_scrape_message = "Failed: session expired during report"
        run_id = store.start_run("scrape")
        store.finish_run(run_id, "error", events_count=0,
                         message="Failed: session expired during report")
        try:
            page.evaluate("async () => { await loadStatus(); }")
            card_text = page.inner_text("#bootCard")
            ok("fresh: a failed fetch is admitted, not rendered as quiet",
               "Couldn't fetch bookings" in card_text)
            ok("fresh: ...in the run's own words",
               "session expired during report" in card_text)
        finally:
            conn = sqlite3.connect(store.DB_PATH)
            try:
                conn.execute("DELETE FROM scrape_runs WHERE id = ?",
                             (run_id,))
                conn.commit()
            finally:
                conn.close()

        # The control, without which none of the above means anything: real
        # data makes the card leave and the grid return. seeded() restores
        # the corpus, so the tests after this see the eight bookings again.
        # The navigation is a fresh goto rather than a reload, because the
        # preset above left floors= in this page's URL — a reload would
        # keep that filter and the eight below would never all show.
        seeded()
        page.goto(base + f"/?view=month&date={D}", wait_until="load")
        page.wait_for_function(
            "() => /\\d+ events/.test("
            "document.getElementById('evcnt').textContent)"
        )
        ok("fresh: the card is gone once there are bookings",
           page.locator("#bootCard").count() == 0)
        check("fresh: ...and the grid is what replaced it",
              events_shown(page), 8)
    finally:
        ORCH.session = "ok"
        ORCH.busy = False
        ORCH.busy_action = ""
        ORCH.progress = ""
        ORCH.last_scrape_message = ""
        # The corpus is the eight bookings every other test counts on, and
        # this test emptied it. Restore it even on a mid-test failure —
        # an assertion that throws would otherwise leave every test after
        # this one reading a store with nothing in it.
        if not store.get_events():
            seeded()
        page.close()


# ── Accessibility ────────────────────────────────────────────────────────
#
# Two properties, both false somewhere in this UI when they were first
# checked, and both readable off the DOM rather than by eye. There is no
# scanner vendored in for this on purpose: a bundled axe-core would widen the
# static surface of a process holding a live LSM session, and it would report
# against generic rule names instead of against what actually broke here.

# The accessible name, as far as this app needs it. An input or a select is
# named by aria-label, a real <label>, or a title -- deliberately never by its
# own text, which for a select is just its options, and deliberately never by
# its placeholder, which is what the unlabelled search box was leaning on and
# which disappears the moment anything is typed. Everything else is named by
# its text first, then the same two fallbacks.
#
# Text only counts as a name if it contains a letter or a digit. That is the
# whole point of this check: the defects it was written for are bare glyphs --
# a triangle, a refresh arrow, a gear -- and a first version that accepted any
# non-empty text content passed against an unlabelled button, which is a test
# that cannot fail. "◀" is not a name; "Today" and "⏰ Time" are.
#
# Returns the count it examined alongside the ones that have no name, so a
# caller can tell "every control is named" from "there were no controls".
NAMELESS_CONTROLS = """
() => {
  const has = s => !!(s && s.trim());
  const hasWord = s => /[\\p{L}\\p{N}]/u.test(s || '');
  const named = el => {
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
      if (has(el.getAttribute('aria-label'))) return true;
      if (el.labels && el.labels.length && hasWord(el.labels[0].textContent)) return true;
      return has(el.getAttribute('title'));
    }
    if (hasWord(el.textContent)) return true;
    if (has(el.getAttribute('aria-label'))) return true;
    return has(el.getAttribute('title'));
  };
  const nameless = [];
  let examined = 0;
  for (const el of document.querySelectorAll(
      'button, a[href], select, input, textarea, [role="button"]')) {
    // Anything not rendered is skipped, which is what keeps a closed overlay
    // out of the result rather than reporting its controls as unnamed.
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') continue;
    examined++;
    if (named(el)) continue;
    nameless.push(el.tagName.toLowerCase()
      + (el.id ? '#' + el.id : '')
      + (typeof el.className === 'string' && el.className.trim()
          ? '.' + el.className.trim().split(/\\s+/)[0] : '')
      + ' ' + JSON.stringify((el.textContent || '').slice(0, 24)));
  }
  return { examined, nameless };
}
"""


def test_every_control_has_an_accessible_name(browser, base: str) -> None:
    """Four icon-only controls had no name, and list.html had none anywhere.

    The nav arrows in all three calendar renderers are bare triangles, so
    nothing announced them and they were indistinguishable from the "Today"
    button beside them; the scrape button was a glyph named only by a title.
    On list.html the search box leaned on its placeholder and the room select
    had nothing at all, while the calendar labels both of its own controls.

    Every view is visited because the arrows are built per renderer -- the
    defect existed three times and one page load would only have found one.
    """
    for where in ("/?view=month", "/?view=week", "/?view=today",
                  "/?view=changes", "/list"):
        page = open_page(browser, base, where)
        try:
            result = page.evaluate(NAMELESS_CONTROLS)
        finally:
            page.close()
        ok(f"{where}: controls were actually examined",
           result["examined"] > 0)
        check(f"{where}: every control has a name",
              result["nameless"], [])

    # The filter panel is a second page's worth of controls behind one button.
    page = open_page(browser, base, "/")
    try:
        page.evaluate("() => openFilters()")
        page.wait_for_selector("#filterBody", state="visible")
        result = page.evaluate(NAMELESS_CONTROLS)
    finally:
        page.close()
    ok("the filter panel: controls were actually examined",
       result["examined"] > 5)
    check("the filter panel: every control has a name",
          result["nameless"], [])


def test_toggle_state_is_not_colour_alone(browser, base: str) -> None:
    """A pressed state kept only in a CSS class says nothing to a screen reader.

    list.html's sort buttons and its generated group buttons both showed the
    selection with .on and nothing else, so "sorted by room" and "the Classroom
    group is on" were invisible to anything not looking at the colour. The
    calendar's chips already carried aria-pressed; these never did.
    """
    page = open_page(browser, base, "/list")
    try:
        check("list: Time reads as pressed on load",
              page.eval_on_selector("#s-time", "el => el.getAttribute('aria-pressed')"),
              "true")
        page.click("#s-room")
        check("list: Room reads as pressed after being chosen",
              page.eval_on_selector("#s-room", "el => el.getAttribute('aria-pressed')"),
              "true")
        check("list: and Time reads as released",
              page.eval_on_selector("#s-time", "el => el.getAttribute('aria-pressed')"),
              "false")
        check("list: exactly one sort is pressed",
              page.evaluate(
                  "() => [...document.querySelectorAll('.sort-btn')]"
                  ".filter(b => b.getAttribute('aria-pressed') === 'true').length"),
              1)

        group = page.evaluate(
            "() => [...document.querySelectorAll('#gbar .gb')]"
            ".find(b => b.dataset.g).dataset.g")
        page.click(f'#gbar .gb[data-g="{group}"]')
        check(f"list: the {group} group button reads as pressed",
              page.eval_on_selector(f'#gbar .gb[data-g="{group}"]',
                                    "el => el.getAttribute('aria-pressed')"),
              "true")

        # Removing a filter was a span with a click handler: the tag could be
        # added from the keyboard and only removed with a mouse.
        page.select_option("#roomFilter", "142")
        page.wait_for_selector("#tags .tag-x", state="visible")
        check("list: remove-filter is a real button, so Tab reaches it",
              page.eval_on_selector("#tags .tag-x", "el => el.tagName"),
              "BUTTON")
        check("list: and it says what it will remove",
              page.eval_on_selector("#tags .tag-x",
                                    "el => el.getAttribute('aria-label')"),
              "Remove filter: Room 142")
    finally:
        page.close()


def test_the_focus_ring_is_not_removed_without_replacement(browser, base: str) -> None:
    """outline:none is only safe with something visible in its place.

    Both pages remove the outline from their search box and replace it with a
    border and a glow. list.html removed it from the room select too and put
    nothing back, so the one control there you reach with Tab and then change
    with the arrow keys was the one that never showed where the focus was.

    Asserted as a *difference* — the chrome blurred against the chrome focused
    — because the first version of this test read the focused box-shadow and
    asked whether it was "none", and that cannot fail here: both controls carry
    a resting `box-shadow: var(--sh)` for depth. Deleting the room select's
    focus rule outright still left the suite at 160 passed, because the
    assertion was reading the card shadow and calling it a focus ring. The
    delta has no such escape: the page sets `outline:none`, so the browser's
    own focus outline is suppressed and the only thing that can make the two
    readings differ is the page's own `:focus` rule.
    """
    CHROME = ("el => { const s = getComputedStyle(el);"
              " return [s.outlineStyle, s.boxShadow, s.borderColor].join(' | '); }")

    page = open_page(browser, base, "/list")
    try:
        for where, sel, what in (("/", "#q", "the search box"),
                                 ("/list", "#search", "the search box"),
                                 ("/list", "#roomFilter", "the room select")):
            p = page if where == "/list" else open_page(browser, base, where)
            try:
                p.eval_on_selector(sel, "el => el.blur()")
                blurred = p.eval_on_selector(sel, CHROME)
                p.focus(sel)
                focused = p.eval_on_selector(sel, CHROME)
            finally:
                if p is not page:
                    p.close()
            ok(f"{where} {sel}: focusing {what} changes something visible",
               focused != blurred)

        # The structural half of the same claim, and the half that catches the
        # next control rather than these three: every selector in the page's
        # own stylesheet that removes the outline must have a :focus rule
        # putting something back. The computed check above only covers the
        # controls this test happens to name.
        for where in ("/", "/list"):
            p = open_page(browser, base, where)
            try:
                pairs = p.evaluate(
                    "() => { const stripped = [], ringed = [];"
                    " for (const sheet of document.styleSheets) {"
                    "  if (sheet.href) continue;"
                    "  let rules; try { rules = sheet.cssRules; } catch (e) { continue; }"
                    "  for (const rule of rules) {"
                    "   if (!rule.selectorText || !rule.style) continue;"
                    "   const o = rule.style.outline || rule.style.outlineStyle || '';"
                    "   if (/none|^0/.test(o)) stripped.push(rule.selectorText);"
                    "   if (rule.selectorText.includes(':focus')"
                    "       && rule.style.boxShadow"
                    "       && rule.style.boxShadow !== 'none')"
                    "     ringed.push(rule.selectorText);"
                    "  } } return { stripped, ringed }; }")
            finally:
                p.close()
            stripped, ringed = pairs["stripped"], pairs["ringed"]
            ok(f"{where}: the stylesheet removes an outline somewhere to check",
               len(stripped) > 0)
            missing = [s for s in stripped
                       if not any(s.split(':')[0].strip() in r for r in ringed)]
            check(f"{where}: every outline:none has a :focus ring beside it",
                  missing, [])
    finally:
        page.close()

    # And the ring rule itself is present on both pages, so tabbing looks
    # the same in both rather than the browser's default in one.
    for where in ("/", "/list"):
        p = open_page(browser, base, where)
        try:
            style = p.evaluate(
                # Only same-origin sheets are readable: reading cssRules
                # off the Google Fonts <link> throws a SecurityError, and
                # that sheet is aborted by the route guard anyway. The rule
                # being looked for is in the page's own inline <style>,
                # whose href is null.
                "() => { for (const sheet of document.styleSheets) {"
                "  if (sheet.href) continue;"
                "  let rules; try { rules = sheet.cssRules; } catch (e) { continue; }"
                "  for (const rule of rules) {"
                "    if (rule.selectorText === ':focus-visible') return true;"
                "  } } return false; }")
        finally:
            p.close()
        ok(f"{where}: has a :focus-visible rule", style)


def test_search_suggestions_are_reachable_by_keyboard(browser, base: str) -> None:
    """The list a keyboard user is left holding after they type.

    Typing in the search box is what opens this dropdown, and every suggestion
    in it was a div carrying an onmousedown -- so the box invited a keyboard in
    and then offered it nothing it could act on. Nothing in the name check above
    would have caught that: a div is not a control, so it was never examined.

    Asserted by doing it rather than by reading the markup, which is the only
    version that proves reachability: Tab out of the box, check where the focus
    landed, press Enter, check the tag that appears.

    The chip it leaves behind is the same defect one step later, and it stayed
    hidden for the same reason -- the calendar removed a tag with a span
    carrying an onclick, and no assertion had ever put a tag on screen first, so
    it was never looked at. Having added one, this checks it can be taken off
    again by something that is not a mouse.
    """
    for path, box, chips in (("/", "#q", "#active"), ("/list", "#search", "#tags")):
        page = open_page(browser, base, path)
        try:
            page.click(box)
            page.fill(box, "14")
            page.wait_for_selector("#sdrop.show .sdrop-item")

            n = page.eval_on_selector_all(
                "#sdrop .sdrop-item",
                "els => els.filter(e => e.offsetParent !== null).length")
            ok(f"{path}: the dropdown has suggestions to reach", n > 0)

            page.keyboard.press("Tab")
            landed = page.evaluate(
                "() => { const a = document.activeElement;"
                " return a ? (a.className || '') : ''; }")
            ok(f"{path}: Tab from the search box lands on a suggestion",
               landed.startswith("sdrop-item"))

            page.keyboard.press("Enter")
            page.wait_for_selector(f"{chips} .tag")
            tag = page.inner_text(f"{chips} .tag")
            ok(f"{path}: Enter adds the suggestion the focus was on",
               "14" in tag)

            # Reachability, not styling: a button is in the tab order and is
            # announced, whatever it looks like. Then the wiring, so this
            # cannot pass on a control that is focusable and does nothing.
            x = page.eval_on_selector(
                f"{chips} .tag .tag-x",
                "el => ({ tag: el.tagName, label: el.getAttribute('aria-label')"
                " || '', tabbable: el.tabIndex >= 0 })")
            ok(f"{path}: the remove control is a real button", x["tag"] == "BUTTON")
            ok(f"{path}: ...that a keyboard can land on", x["tabbable"])
            ok(f"{path}: ...and it says what it will remove", bool(x["label"]))

            # The remove control clears the filter its own chip names. Asserted
            # by label rather than by "the row is now empty": choosing a
            # suggestion adds that room, and the search term stays on as a
            # filter of its own beside it, so the row can legitimately hold two
            # chips here.
            #
            # Messages in this file stay ASCII: the suite prints them to a
            # cp1252 console, and the page's own ✕ cannot be encoded there.
            before = page.eval_on_selector(
                f"{chips} .tag", "el => el.childNodes[0].textContent")
            page.click(f"{chips} .tag .tag-x")
            page.wait_for_function(
                "([sel, label]) => "
                "![...document.querySelectorAll(sel + ' .tag')]"
                ".some(e => e.childNodes[0].textContent === label)",
                arg=[chips, before])
            ok(f"{path}: the remove control cleared the filter its chip named",
               before not in chip_labels(page))
        finally:
            page.close()


def test_returning_to_changes_rebuilds_the_panel(browser, base: str) -> None:
    """The Changes view has to come back after you leave it.

    renderChanges memoises the fetched feed under a key made of the filters, so
    returning to the view with nothing changed skips the refetch. The memo was
    also being read as a promise about the *screen*: every other renderer
    replaces mainArea wholesale, so Month (or Week, or Today) destroys
    #chgList while CHANGES and CHANGE_KEY survive it. Coming back then took
    the memo path, painted into an element that no longer existed, and returned
    early -- bringing the Changes view up blank.

    It failed on the *second* visit every time the filters had not changed,
    which is why one page load would never have found it: the first visit has
    no memo to hit. So the trip here is out and back, and the assertion is on
    the panel and its rows rather than on the fetch having happened.
    """
    print("\nchanges view")
    D = "2026-04-15"
    page = open_page(browser, base, f"/?view=changes&date={D}")
    try:
        # First visit: no memo yet, so this is the path that always worked.
        page.wait_for_selector("#chgList .chg")
        first = page.eval_on_selector_all("#chgList .chg", "els => els.length")
        ok("changes: the first visit lists changes", first > 0)

        # Away and back, with the filters untouched so the memo is hit.
        page.click("#v-month")
        page.wait_for_selector("#mainArea .cgrid")
        check("changes: the panel is gone once we leave",
              page.eval_on_selector_all("#chgList", "els => els.length"), 0)

        page.click("#v-changes")
        page.wait_for_selector("#chgList")
        rows = page.eval_on_selector_all("#chgList .chg", "els => els.length")
        check("changes: the second visit lists the same changes", rows, first)
        cnt = (page.inner_text("#chgCnt") or "").strip()
        ok("changes: and the count is filled in, not left at 'loading…'",
           cnt and cnt != "loading…" and "+" in cnt)
    finally:
        page.close()


def test_free_now_is_grouped_timestamped_and_filters_in_place(browser, base: str) -> None:
    """The Free-right-now drawer: what it measured, and when.

    Measured against the running app rather than guessed: 58 of 91 rooms free,
    49 of them with a booking later that day, 9 free all day. The panel
    rendered all of that as one unlabelled line of text -- "127 11:15" in a 62px
    box -- with nothing saying which number was the room and which the
    clock, nothing saying when the answer had been measured, and 58 rooms
    arriving as one undifferentiated run.

    Four properties, each of which the old panel failed:

      * the answer is timestamped, so a list fetched at boot is not still
        labelled "Right Now" after a morning in a background tab;
      * the rooms are grouped by the thing that makes them different offers --
        free all day vs free until a time -- and each heading counts its own
        rows;
      * the rows are real buttons, so the list is reachable by keyboard;
      * choosing a room narrows the filter to it without throwing the rest of
        the filter away, and without changing the view. The old handler did
        both, from a panel the surrounding markup calls read-only.
    """
    # A room free now and booked later today, so the list has both groups to
    # show; the corpus otherwise has no bookings on today's date at all, which
    # would leave every room in one group. 23:58 is the only window where this
    # is not true, and by then there is no day left to ask about.
    today = datetime.now().strftime("%Y-%m-%d")
    store.replace_events(
        store.get_events() + [booking("142", today, "23:58", "23:59", "Late slot")],
        SOW, D2,
    )
    try:
        page = open_page(browser, base, f"/?view=month&date={D}")
        try:
            # The list answers from the drawer now, not the sidebar, and a
            # closed drawer's contents are written but not visible -- so the
            # toolbar button comes first.
            page.click("#freeBtn")
            page.wait_for_selector("#freeDrawer.open")
            page.wait_for_selector("#freeList .fr")

            as_of = (page.inner_text("#freeAs") or "").strip()
            ok("free: the panel says when the answer was measured",
               as_of.startswith("as of ")
               and len(as_of) > len("as of ") + 3
               and as_of.split()[2].count(":") == 1)

            # Read the group structure as rendered, rather than re-deriving it
            # from the corpus: what is asserted is that the headings and the
            # rows agree with each other.
            structure = page.evaluate(
                """() => {
                     const out = [];
                     let cur = null;
                     for (const el of document.getElementById('freeList').children) {
                       if (el.classList.contains('fgp')) {
                         cur = {label: el.textContent, rows: 0, buttons: 0};
                         out.push(cur);
                       } else if (el.classList.contains('fr') && cur) {
                         cur.rows++;
                         if (el.tagName === 'BUTTON') cur.buttons++;
                       }
                     }
                     return out;
                   }""")
            check("free: the free rooms are grouped, not one run of them",
                  [g["label"].split(" (")[0] for g in structure],
                  ["Free all day", "Free until"])
            for g in structure:
                label, _, count = g["label"].rpartition(" (")
                check(f"free: '{label}' counts the rows under it",
                      int(count.rstrip(")")), g["rows"])
                check(f"free: every row under '{label}' is a real button",
                      g["buttons"], g["rows"])

            named = page.eval_on_selector_all(
                "#freeList .fr",
                "els => els.map(e => e.getAttribute('aria-label') || '')")
            ok("free: each row says which room, and until when",
               named and all("free until " in n or "free all day" in n for n in named))

            # The heading's count is the strong one and the groups are the
            # detail, so the two are shown to agree. textContent, not
            # inner_text: the section title is small-caps in CSS, and reading
            # it through the stylesheet would make this assert "4 FREE ALL DAY"
            # -- the rendering, rather than the number.
            check("free: the section heading counts the all-day rooms",
                  page.eval_on_selector("#freeN", "el => el.textContent").strip(),
                  f"{structure[0]['rows']} free all day")

            page.fill("#q", "CIBC")
            page.wait_for_selector("#active .tag")
            room = page.eval_on_selector("#freeList .fr", "el => el.dataset.room")
            page.click("#freeList .fr")
            check("free: choosing a room narrows the filter to that room",
                  page.evaluate("() => [...activeRooms].join(',')"), room)
            check("free: ...without throwing away the rest of the filter",
                  page.evaluate("() => searchQ"), "cibc")
            check("free: ...and without moving you off the view you were on",
                  page.evaluate("() => viewMode"), "month")
        finally:
            page.close()
    finally:
        # The seeded booking is for today, and today is not one of the fixture's
        # fixed dates, so it is restored rather than left behind: a store that
        # differs after this test is a different corpus for whatever runs next.
        seeded()
        # ...but seeded() cannot remove it, which is why it is also deleted
        # here: replace_events deletes only inside its window, and no
        # reconcile over the fixture's fixed dates ever reaches a row dated
        # today. Left behind, the store holds nine bookings where every
        # corpus count downstream says eight -- a trap for whichever test
        # is appended or reordered ahead of this one.
        late = booking("142", today, "23:58", "23:59", "Late slot")
        conn = sqlite3.connect(store.DB_PATH)
        try:
            conn.execute("DELETE FROM events WHERE uid = ?",
                         (store.event_uid(late),))
            conn.commit()
        finally:
            conn.close()
        check("free: the corpus is the eight seeded bookings again",
              len(store.get_events()), 8)


def test_quick_filters_drive_the_free_drawer(browser, base: str) -> None:
    """The toolbar's Free-now drawer: quick filters, and a list that obeys them.

    The free list used to sit in the sidebar and answer for all 91 rooms at
    once, so no filter could reach it -- "is a classroom free right now" was
    not a question it could answer, and a list that ignored the chips two
    sections above it was answering a question nobody asked. The drawer that
    replaces it carries one chip per floor plus the Classroom and Events
    groups, and both the chips and the list hold the *same* filter state the
    calendar does -- not a private copy, which is how a second control surface
    drifts from the first.

    Measured properties, each of which a chip-less or unfiltered drawer would
    fail:

      * one toolbar button, and no free list left in the sidebar: two lists
        with different answers is the clutter;
      * the chips are advFloors and the groups themselves -- the URL, the
        Active filters row and the sidebar's group button all move with a
        chip click;
      * the list answers with those filters: every row is a room the
        calendar would show;
      * the button's count is the number the drawer lists, so the button
        cannot promise rooms the filters then hide;
      * Escape closes it, and the button says which state it is in.
    """
    # Classroom and Events are not in the seeded group set (it is North and
    # Dean's Suite), and the kind chips are built from the groups the data
    # knows -- so they are exercised against groups really named Classroom
    # and Events, and the seeded set is restored for the same reason every
    # other fixture change here is. Room 127 is the Classroom member: it is on
    # the ground floor, its two corpus bookings are both on other days, and
    # so nothing below depends on the clock.
    store.save_groups({
        "Classroom": ["127"], "Events": ["100", "2057"],
        "North": ["142"], "Dean's Suite": ["157"],
    })
    try:
        page = open_page(browser, base, f"/?view=month&date={D}")
        try:
            ok("drawer: closed on load, and the sidebar carries no free list "
               "of its own",
               page.evaluate("() => !document.getElementById('freeDrawer')"
                             ".classList.contains('open')"
                             " && !document.querySelector('.side .freelist')"))

            page.click("#freeBtn")
            page.wait_for_selector("#freeDrawer.open")
            check("drawer: the button says it is open",
                  page.eval_on_selector("#freeBtn",
                                       "el => el.getAttribute('aria-expanded')"),
                  "true")

            # The chips are read as rendered and compared with the page's own
            # floor list rather than a hardcoded expectation: the chip set
            # must follow the data, and a copy of the floors in this test
            # would agree with a stale chip set in the page.
            check("drawer: one chip per floor the data knows",
                  page.evaluate("() => [...document.querySelectorAll('#qfloor .gb')]"
                                ".map(b => b.textContent)"),
                  page.evaluate("() => floorsPresent()"
                                ".map(f => f === NO_FLOOR ? 'No floor' : f)"))
            check("drawer: the kind chips are the classroom and event groups",
                  page.evaluate("() => [...document.querySelectorAll('#qgroup .gb')]"
                                ".map(b => b.textContent)"),
                  ["Classroom", "Events"])
            ok("drawer: every chip's pressed state is said out loud",
               page.evaluate("() => [...document.querySelectorAll('#qfloor .gb, #qgroup .gb')]"
                             ".every(b => b.hasAttribute('aria-pressed'))"))

            # A floor chip, for the ground floor.
            ground = floor_for("127")
            page.click(f'#qfloor .gb:text-is("{ground}")')
            check("drawer: the floor chip reads as pressed",
                  page.eval_on_selector(f'#qfloor .gb:text-is("{ground}")',
                                       "el => el.getAttribute('aria-pressed')"),
                  "true")
            check("drawer: ...and is the same filter the page holds",
                  page.evaluate("f => advFloors.size === 1 && advFloors.has(f)",
                                ground),
                  True)
            ok("drawer: ...and the URL says it too",
               param(page, "floors") is not None)
            ok("drawer: ...and the sidebar's Active filters row agrees",
               ground in chip_labels(page))

            # The list, compared against the page's state recomputed from
            # first principles (floorOf over ROOMS) rather than through
            # roomAllowed -- so a drawer that silently dropped the filter
            # fails here instead of agreeing with itself. The wait is because
            # loadFree is a fetch: the rows cannot be read the instant the
            # chip goes down.
            page.wait_for_function(
                "() => [...document.querySelectorAll('#freeList .fr')]"
                ".every(e => floorOf(e.dataset.room) === [...advFloors][0])")
            check("drawer: the list answers with the current filters",
                  page.evaluate("() => [...document.querySelectorAll('#freeList .fr')]"
                                ".map(e => e.dataset.room).sort()"),
                  page.evaluate("() => ROOMS.filter(r => floorOf(r) === [...advFloors][0])"
                                ".sort()"))

            # A kind chip, on top of the floor.
            page.click('#qgroup .gb:text-is("Classroom")')
            page.wait_for_function(
                "() => [...document.querySelectorAll('#freeList .fr')]"
                ".every(e => GROUPS.Classroom.includes(e.dataset.room))")
            ok("drawer: the Classroom chip and the sidebar's group button "
               "are one state",
               page.evaluate("() => activeGroups.has('Classroom')"
                             " && document.querySelector('#gbar [data-g=\"Classroom\"]')"
                             ".classList.contains('on')"))
            want = page.evaluate(
                "() => ROOMS.filter(r => GROUPS.Classroom.includes(r)"
                " && floorOf(r) === [...advFloors][0]).sort()")
            check("drawer: floor and kind narrow the list together",
                  page.evaluate("() => [...document.querySelectorAll('#freeList .fr')]"
                                ".map(e => e.dataset.room).sort()"),
                  want)
            check("drawer: the toolbar button's count is what the drawer lists",
                  int(page.eval_on_selector("#freeBtnN", "el => el.textContent")),
                  len(want))

            page.keyboard.press("Escape")
            ok("drawer: Escape closes it",
               page.evaluate("() => !document.getElementById('freeDrawer')"
                             ".classList.contains('open')"))
            check("drawer: ...and the button says so",
                  page.eval_on_selector("#freeBtn",
                                       "el => el.getAttribute('aria-expanded')"),
                  "false")

            # Reopening rebuilds the chips from the live state, so a filter
            # changed somewhere else cannot leave the drawer showing the
            # filters as they were.
            page.click("#freeBtn")
            check("drawer: reopening shows the filters as they are",
                  page.eval_on_selector('#qgroup .gb:text-is("Classroom")',
                                       "el => el.getAttribute('aria-pressed')"),
                  "true")
        finally:
            page.close()
    finally:
        seeded_groups()


TESTS = [
    test_shared_helpers_are_served,
    test_today_nav_keeps_url_and_exports_current,
    test_today_back_arrow_is_symmetric,
    test_nav_agrees_between_arrow_and_keyboard,
    test_month_nav_still_steps_a_month,
    test_preset_names_itself_in_the_toast,
    test_scalar_floors_does_not_blank_the_calendar,
    test_string_rooms_selects_those_rooms,
    test_no_floor_is_selectable,
    test_no_floor_survives_a_reload,
    test_nonsense_values_apply_as_no_opinion,
    test_free_at_filters_each_booking_by_its_own_day,
    test_free_at_is_per_day_for_a_single_day_window,
    test_free_at_clears_the_rooms_booked_that_day,
    test_free_at_note_describes_the_span,
    test_list_has_a_url_and_the_links_carry_it,
    test_list_group_filter_reaches_the_download,
    test_list_badge_reads_the_session,
    test_sign_out_is_offered_only_with_a_session,
    test_list_select_and_tags_are_one_filter,
    test_list_groups_come_from_the_config,
    test_list_renders_an_all_day_block_as_all_day,
    test_list_copies_a_real_date,
    test_a_hostile_title_is_not_code,
    test_week_view_owns_all_seven_of_its_days,
    test_nav_steps_the_view_you_are_looking_at,
    test_list_reads_the_calendars_link,
    test_chips_do_not_outlive_the_filters_they_name,
    test_a_renamed_group_does_not_eat_its_neighbour,
    test_history_fill_toast_reports_the_fill_not_the_edge,
    test_a_fresh_install_says_what_it_is_doing,
    test_every_control_has_an_accessible_name,
    test_toggle_state_is_not_colour_alone,
    test_the_focus_ring_is_not_removed_without_replacement,
    test_search_suggestions_are_reachable_by_keyboard,
    test_returning_to_changes_rebuilds_the_panel,
    test_free_now_is_grouped_timestamped_and_filters_in_place,
    test_quick_filters_drive_the_free_drawer,
]


def run(test, browser, base: str) -> None:
    """One test, with a crash reported as a failure rather than ending the run."""
    global FAIL
    try:
        test(browser, base)
    except Exception as exc:
        FAIL += 1
        msg = f"{type(exc).__name__}: {exc}".encode("ascii", "replace").decode()
        print(f"  FAIL  {test.__name__} raised\n          {msg}")


def main() -> int:
    print("=" * 60)
    print("  web UI (real browser)")
    print("=" * 60)

    store.init_db()
    seeded()
    seeded_presets()
    seeded_groups()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"  FAIL  playwright is not importable: {exc}")
        return 1

    from werkzeug.serving import make_server

    # threaded=True is required, not tidiness: the page fires bootstrap,
    # status, today and free concurrently, and a single-threaded server with
    # a browser attached deadlocks.
    srv = make_server("127.0.0.1", 0, create_app(ORCH), threaded=True)
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
                for test in TESTS:
                    run(test, browser, base)
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
