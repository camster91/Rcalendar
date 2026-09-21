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
# Outside March's grid entirely, so it never enters a free-at answer.
D2 = "2026-04-20"

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

    The Auditorium is the other shape the corpus needs, and it carries two of
    them. Its name matches none of app.rooms.floor_for's patterns and it is
    not in ROOM_INFO, so it comes back with no floor at all -- the only way to
    reach the branch where the floor filter has nothing to place a room on. Its
    title is HOSTILE, because a booking title is free text that anyone who
    books a Rotman room can set, and the copy button used to compile it as
    script. Both need a booking that no count assertion depends on, which is
    why they share one: it sits outside March's grid on purpose, so it cannot
    enter a free-at answer and move those counts.
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
            booking("Auditorium", D2, "09:00", "12:00", HOSTILE),
        ],
        D, D2,
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

    Read correctly it selects the five bookings in Ground Floor rooms -- 142 has
    two, 147 has two, 157 has one -- and leaves out the Auditorium, which has no
    recorded floor. That exclusion is the intended strict reading, not a
    regression: see test_no_floor_is_selectable for the other half of it.
    """
    page = open_page(browser, base, "/?view=month&date=2026-03-10")
    try:
        check("preset: the corpus is the six seeded bookings",
              events_shown(page), 6)
        open_presets(page)
        apply_preset(page, "Ground Floor")
        check("preset: scalar floors keeps exactly the rooms that match",
              events_shown(page), 5)
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


def test_list_select_and_tags_are_one_filter(browser, base: str) -> None:
    """They were two filters that ANDed, so combining them selected nothing.

    The <select> ANDed against the OR'd room tags. Picking room 142 in the
    dropdown and room 157 from the search results asked for bookings in 142 and
    in 157 — which is no booking at all. There is one selection now, and both
    controls write it.
    """
    page = open_page(browser, base, "/list")
    try:
        check("list: the corpus is the six seeded bookings",
              events_shown(page), 6)
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
    test_list_select_and_tags_are_one_filter,
    test_list_groups_come_from_the_config,
    test_list_renders_an_all_day_block_as_all_day,
    test_list_copies_a_real_date,
    test_a_hostile_title_is_not_code,
    test_list_reads_the_calendars_link,
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
