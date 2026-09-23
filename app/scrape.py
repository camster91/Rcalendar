"""
APEX report automation.

Drives the page-51 "Rotman Query" report and pulls its CSV export. Three
things about this page are non-obvious and are the reason this code looks
the way it does:

1. `P51_FR_DATE` / `P51_TO_DATE` are APEX date items. Setting the DOM
   value of the underlying input does not update APEX's model, so the
   report runs with whatever it had before. The only reliable way in is
   the client-side API: `apex.item('P51_FR_DATE').setValue(...)` — and
   the value is read back afterwards, because setValue does not promise
   the item *kept* what it was given. See `_set_dates` for why a window
   that quietly differs is worth refusing the run over.

2. `P51_ROOM` is an APEX *shuttle* widget — two <select>s and a
   "Move All" button — not a dropdown. We move everything across rather
   than enumerating rooms, so a newly added room is picked up for free.

3. The report is rendered server-side into the page. We grab the CSV via
   the Download link, and fall back to scraping the HTML results table if
   the download does not materialise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app import session
from app.config import (
    APEX_APP_ID, APEX_AUTH_URL, BROWSER_TIMEOUT_MS, NAVIGATE_TIMEOUT_MS,
    ROTMAN_PAGE_ID, log,
)
from app.parse import clean_title, parse_csv

APEX_SESSION_RE = re.compile(rf"f\?p={APEX_APP_ID}:\d+:(\d+)")


@dataclass
class ScrapeResult:
    status: str                     # ok | empty | auth_required | error
    events: list[dict[str, Any]] = field(default_factory=list)
    date_from: str = ""             # dd/mm/yyyy
    date_to: str = ""
    rooms: list[str] = field(default_factory=list)
    message: str = ""
    # False when the report was read off the rendered page rather than out of
    # the export. The distinction is load-bearing rather than informational:
    # the window's bounds are the bounds of the reconcile delete, so a report
    # that is a *page* of the results would delete every booking it did not
    # show. Only the export is the whole report. See _download_csv.
    complete: bool = True

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "empty")


def default_window(
    months_back: int = 1, months_ahead: int = 2
) -> tuple[str, str]:
    """Rolling window as (dd/mm/yyyy, dd/mm/yyyy) — the report's format."""
    today = date.today()
    start_m = today.month - months_back
    start_y = today.year + (start_m - 1) // 12
    start_m = (start_m - 1) % 12 + 1

    end_m = today.month + months_ahead
    end_y = today.year + (end_m - 1) // 12
    end_m = (end_m - 1) % 12 + 1

    first = date(start_y, start_m, 1)
    # Last day of the end month.
    if end_m == 12:
        last = date(end_y, 12, 31)
    else:
        last = date(end_y, end_m + 1, 1) - timedelta(days=1)

    return first.strftime("%d/%m/%Y"), last.strftime("%d/%m/%Y")


def month_start(months_back: int) -> date:
    """First day of the calendar month `months_back` months before this one.

    month_start(0) is the 1st of the current month; negatives go forward.
    """
    today = date.today()
    m = today.month - months_back
    y = today.year + (m - 1) // 12
    return date(y, (m - 1) % 12 + 1, 1)


def month_end(months_back: int) -> date:
    """Last day of the calendar month `months_back` months before this one."""
    start = month_start(months_back)
    nxt = date(start.year + (start.month // 12), start.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def backfill_windows(
    months_back: int = 12, daily_months_back: int = 1
) -> list[tuple[str, str]]:
    """
    Whole calendar months to fetch, as (dd/mm/yyyy, dd/mm/yyyy) pairs,
    oldest first.

    Stops short of the month the daily scrape's window starts in, so the
    backfill and the daily scrape never do the same month twice.

    One month per request rather than one long window, on purpose. The
    window's bounds are also the bounds of the reconcile delete, so a month
    APEX fails to render deletes that month and nothing else; a single
    twelve-month window would put the whole year behind one bad render. It
    also makes progress reportable and the run resumable — re-scraping a
    finished month finds no changes and writes nothing.
    """
    return [
        (month_start(k).strftime("%d/%m/%Y"), month_end(k).strftime("%d/%m/%Y"))
        for k in range(months_back, daily_months_back, -1)
    ]


def scrape(
    date_from: str | None = None,
    date_to: str | None = None,
    headless: bool | None = None,
    on_status=None,
) -> ScrapeResult:
    """
    Run one full scrape. Assumes a live session — a dead one surfaces as
    status='auth_required' rather than being retried here; the scheduler
    probes before calling, and sign-in is a separate, interactive action.
    """
    from app.config import HEADLESS_WHEN_POSSIBLE

    date_from = date_from or default_window()[0]
    date_to = date_to or default_window()[1]
    headless = HEADLESS_WHEN_POSSIBLE if headless is None else headless

    def say(msg: str) -> None:
        log.info(msg)
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    say(f"Scraping {date_from} → {date_to}")

    try:
        with session.browser(headless=headless, restore=True) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(BROWSER_TIMEOUT_MS)

            downloads: list[Any] = []
            page.on("download", lambda d: downloads.append(d))

            # ── Reach the app ──
            page.goto(APEX_AUTH_URL, wait_until="domcontentloaded",
                      timeout=NAVIGATE_TIMEOUT_MS)
            page.wait_for_timeout(1800)

            if session.is_login_url(page.url):
                return ScrapeResult("auth_required", message="Session expired")

            m = APEX_SESSION_RE.search(page.url)
            session_id = m.group(1) if m else None

            url = (
                f"https://lsm.utoronto.ca/ords/f?p={APEX_APP_ID}:"
                f"{ROTMAN_PAGE_ID}:{session_id}:::::"
                if session_id
                else f"https://lsm.utoronto.ca/ords/f?p={APEX_APP_ID}:{ROTMAN_PAGE_ID}"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
            page.wait_for_timeout(2500)

            if "LOGIN_DESKTOP" in page.url or session.is_login_url(page.url):
                return ScrapeResult("auth_required", message="Session expired")

            if not _wait_for_item(page, "P51_FR_DATE"):
                return ScrapeResult(
                    "error",
                    message="Report page did not load (P51_FR_DATE missing)",
                )

            # ── Dates ──
            # The window is checked before anything else runs, because a
            # report that rendered some *other* window would have its bounds
            # delete real bookings on reconcile. See _set_dates.
            mismatch = _set_dates(page, date_from, date_to)
            if mismatch:
                return ScrapeResult("error", message=mismatch,
                                    date_from=date_from, date_to=date_to)
            say(f"Dates set: {date_from} → {date_to}")

            # ── Rooms ──
            rooms, selection_ok = _select_all_rooms(page)
            say(f"Selected {len(rooms)} rooms")

            # ── Run ──
            # The click itself is checked, not assumed: a Generate that
            # never landed leaves the page holding the render it loaded
            # with — the previous scrape's window, persisted in APEX
            # session state — and every date on it can sit inside this
            # one. Only the page's per-render token can tell that shape
            # from a fresh render (see _generate_report).
            fresh = _generate_report(page)

            body = page.locator("body").inner_text()
            if "no data found" in body.lower():
                say("Report returned no data for this window")
                return ScrapeResult("empty", date_from=date_from,
                                    date_to=date_to, rooms=rooms,
                                    complete=fresh)

            # ── Export ──
            # `reasons` is the list of things the reconcile delete is
            # allowed to trust, each with the failure it guards against:
            # the export (the Download link hands over the report itself,
            # so its silence about a booking is evidence the booking is
            # gone; the rendered table is one page of an interactive
            # report, so its silence means nothing), the render (a click
            # that produced no render is no report at all, whatever its
            # dates look like), and the room selection (a report that ran
            # with only some rooms selected is not the calendar — its
            # silence about an unselected room would otherwise file that
            # room's bookings as cancellations). Any reason present means
            # the result is adds-only: adds land, deletes do not.
            reasons: list[str] = []
            csv_text = _download_csv(page, downloads)
            if not csv_text:
                say("CSV download unavailable — falling back to HTML table")
                csv_text = _html_table_to_csv(page)
                reasons.append("read from the rendered page, which is not "
                               "the whole report")
            if not fresh:
                reasons.append("no evidence the Generate click produced a "
                               "new render, so the page may still hold the "
                               "previous window")
            if not selection_ok:
                reasons.append("the room selection was incomplete, so the "
                               "export may cover only part of the calendar")
            complete = not reasons

            if not csv_text.strip():
                return ScrapeResult(
                    "error", date_from=date_from, date_to=date_to,
                    rooms=rooms, message="Report had data but no export could be read",
                )

            events = parse_csv(csv_text)
            for ev in events:
                ev["title"] = clean_title(ev["title"])

            # The second, independent check on the window, and the one the
            # date readback cannot make: not "did the page keep the dates"
            # but "did the report actually run them". See _outside_window.
            outside = _outside_window(events, date_from, date_to)
            if outside:
                log.error("export holds %d booking(s) outside %s → %s (e.g. %s)",
                          len(outside), date_from, date_to, outside[0])
                return ScrapeResult(
                    "error", date_from=date_from, date_to=date_to,
                    rooms=rooms,
                    message=(f"{len(outside)} booking(s) in the export fall "
                             f"outside {date_from} → {date_to}, e.g. "
                             f"{outside[0]} — the report did not run this "
                             f"window. Refusing to reconcile against it, "
                             f"because the window's bounds are the bounds of "
                             f"the reconcile delete."),
                )

            if reasons:
                say(f"Parsed {len(events)} bookings, but the result is not "
                    f"trusted to delete ({'; '.join(reasons)}), so nothing "
                    f"will be deleted")
            say(f"Parsed {len(events)} bookings")
            return ScrapeResult(
                "ok" if events else "empty",
                events=events, date_from=date_from, date_to=date_to,
                rooms=rooms, complete=complete,
            )

    except Exception as exc:
        log.exception("scrape failed")
        return ScrapeResult("error", message=str(exc),
                            date_from=date_from, date_to=date_to)


# ── Page helpers ─────────────────────────────────────────────────────────

def _wait_for_item(page: Any, item: str, timeout: int = 20_000) -> bool:
    """Wait until APEX has booted and the item exists in the model."""
    try:
        page.wait_for_function(
            f"() => window.apex && apex.item && apex.item('{item}')",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _set_dates(page: Any, date_from: str, date_to: str) -> str | None:
    """
    Set the report window, then check it is the window we asked for.

    Returns an error message if the report's own dates are confidently not the
    ones requested, and None otherwise.

    The check is not decoration, because the window's bounds are also the
    bounds of the reconcile delete: store.replace_events removes every stored
    booking inside [date_from, date_to] that the scrape did not report. So a
    report that quietly ran a *narrower* window than we asked for does not
    merely return less — it deletes real bookings off the calendar, and the
    scrape still reports success. Refusing is the only safe answer.

    setValue is documented above as the only reliable way into APEX's model,
    and it is; but "the model took it" and "the model kept it" are different
    claims. An item with a format mask or a min/max will reformat or clamp a
    value it does not like rather than raise, so the readback is the only
    evidence of what the report will actually run.

    A date that will not parse abstains rather than failing. The readback is
    the item's display string, and this code cannot know what mask the report
    uses — guessing wrong would turn every scrape into an error, which is a
    worse outcome than the mismatch it is trying to catch. So only a parsed
    disagreement is fatal, and an unparsed one is logged loudly and allowed
    through.
    """
    page.evaluate(
        """([f, t]) => {
            apex.item('P51_FR_DATE').setValue(f);
            apex.item('P51_TO_DATE').setValue(t);
        }""",
        [date_from, date_to],
    )
    page.wait_for_timeout(600)

    actual = page.evaluate(
        """() => [apex.item('P51_FR_DATE').getValue(),
                  apex.item('P51_TO_DATE').getValue()]"""
    )
    log.info("date items now %s → %s", actual[0], actual[1])

    for label, want_raw, got_raw in (("from", date_from, actual[0]),
                                     ("to", date_to, actual[1])):
        want, got = _item_date(want_raw), _item_date(got_raw)
        if want is None or got is None:
            log.warning("could not read back the %s date (%r → %r); "
                        "running the report unverified", label, want_raw, got_raw)
            continue
        if want != got:
            return (f"Report window mismatch: asked for {label} "
                    f"{want.isoformat()}, the page holds {got.isoformat()} "
                    f"({got_raw!r}). Refusing to run, because the window's "
                    f"bounds are the bounds of the reconcile delete.")
    return None


# The display formats a date item's readback might use. APEX hands back the
# item's own display string, so this is whatever mask the report was built
# with rather than anything chosen here — hence a list, and hence abstaining
# rather than failing when none of them match.
_READBACK_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d-%b-%Y", "%d %b %y", "%d-%b-%y",
    "%Y-%m-%d", "%d.%m.%Y",
)


def _item_date(raw: Any) -> date | None:
    """
    Read a date back out of an APEX item, or None if it cannot be read.

    None means "no opinion", which callers must not treat as "wrong": the
    cost of the two mistakes is not symmetric. Saying nothing loses a check;
    saying "mismatch" when the truth is merely an unfamiliar format stops
    every scrape.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    # A datetime string carries the date in its first ten characters or its
    # first token; either way the formats below do not match it, so try the
    # leading date before giving up.
    for candidate in (text, text.split("T")[0].split(" ")[0]):
        for fmt in _READBACK_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _select_all_rooms(page: Any) -> tuple[list[str], bool]:
    """
    Move every available room into the shuttle's selected side.
    Returns the resulting room set, and whether the selection provably
    covers all of it — the shuttle's left list is empty afterwards.
    """
    # Select everything in the left list first, so "Move All" has a target.
    page.evaluate(
        """() => {
            const left = document.getElementById('P51_ROOM_LEFT');
            if (!left) return;
            for (const o of left.options) o.selected = true;
        }"""
    )
    page.wait_for_timeout(300)

    move_all = page.locator(
        'button[title="Move All"], button[aria-label="Move All"], '
        'a[title="Move All"], .shuttleMoveAll'
    )
    clicked = False
    if move_all.count() > 0:
        try:
            move_all.first.click()
            page.wait_for_timeout(700)
            clicked = True
        except Exception:
            clicked = False

    selected = _shuttle_values(page, "P51_ROOM_RIGHT")

    if not clicked and not selected:
        # Direct API path.
        left = _shuttle_values(page, "P51_ROOM_LEFT")
        if left:
            log.info("using apex API to select %d rooms", len(left))
            page.evaluate(
                "(rooms) => apex.item('P51_ROOM').setValue(rooms)", left
            )
            page.wait_for_timeout(600)
            selected = _shuttle_values(page, "P51_ROOM_RIGHT")

    page.evaluate(
        """() => {
            const left = document.getElementById('P51_ROOM_LEFT');
            if (left) for (const o of left.options) o.selected = false;
        }"""
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # Everything the shuttle offered should now be on the selected side. APEX
    # keeps item values in session state, so a page that rendered with a
    # previous run's subset *starts* with rooms still on the left — and if
    # the Move All click above silently did nothing, they stay there. A union
    # alone would hide that (the caller would see a full-looking room list),
    # so the leftover left list is also the check: non-empty means the
    # selection is only a subset, and a subset report's silence about a room
    # is not evidence that room's bookings are gone.
    leftover = _shuttle_values(page, "P51_ROOM_LEFT")
    if leftover:
        log.error("room selection incomplete: %d room(s) still unselected "
                  "after Move All — the export may cover only part of the "
                  "calendar, so it will not be trusted to delete",
                  len(leftover))
    all_rooms = sorted(set(selected) | set(leftover))
    return all_rooms, not leftover


def _shuttle_values(page: Any, element_id: str) -> list[str]:
    try:
        return page.evaluate(
            """(id) => {
                const el = document.getElementById(id);
                return el ? Array.from(el.options).map(o => o.value) : [];
            }""",
            element_id,
        )
    except Exception:
        return []


def _page_submission_id(page: Any) -> str | None:
    """
    APEX's per-render page token, or None when it cannot be read.

    Every full page render carries a fresh `p_page_submission_id` — APEX
    22.1's submit-protection item. It is what turns "the Generate click
    produced a new render" from a guess into a comparison: a real submit
    re-renders the page and the token changes; a click that missed, or a
    page whose JS failed, leaves the token — and the report — exactly as
    the page loaded with them. Measured on the live page 2026-09-23
    (APEX 22.1.9): the token changed on a same-window re-generate whose
    content was identical, which is the one case a content hash cannot
    speak to, and the reason this token is the signal.
    """
    try:
        return page.evaluate(
            """() => {
                const el =
                    document.querySelector('input[name="p_page_submission_id"]');
                return el ? el.value : null;
            }"""
        )
    except Exception:
        return None


def _generate_report(page: Any) -> bool:
    """
    Click Generate, then report whether the click produced a render.

    True is positive evidence — the page's per-render token changed, so a
    submit genuinely re-rendered the page, and the report showing now is
    the one these items ran. False means no such evidence, and the page
    may still be holding the report it loaded with: APEX session state
    persists the previous scrape's window, so that stale report's rows sit
    inside the new window exactly when _outside_window cannot see them —
    narrower-but-inside is the shape the window checks cannot refuse, and
    only the render token can. The caller must treat False as "this
    report's silence is not evidence about anything".

    A token that cannot be read on either side is no opinion rather than a
    refusal: an APEX without the item, or a read that failed, would
    otherwise stop every scrape trusting nothing, the worse mistake of the
    two — the same asymmetry _item_date gives a date it cannot read.
    """
    before = _page_submission_id(page)

    button = page.locator('button:has-text("Generate Report")')
    if button.count() > 0:
        try:
            button.first.click()
        except Exception:
            page.evaluate("apex.page.submit({request:'GENERATE'});")
    else:
        page.evaluate("apex.page.submit({request:'GENERATE'});")

    try:
        page.wait_for_load_state("networkidle", timeout=BROWSER_TIMEOUT_MS)
    except Exception:
        pass
    page.wait_for_timeout(2500)

    after = _page_submission_id(page)
    if before is None or after is None:
        log.warning("could not read p_page_submission_id (%r → %r); cannot "
                    "verify that Generate rendered — trusting the click",
                    before, after)
        return True
    return before != after


def _download_csv(page: Any, downloads: list[Any]) -> str | None:
    link = page.locator('a:has-text("Download")')
    if link.count() == 0:
        return None

    try:
        with page.expect_download(timeout=BROWSER_TIMEOUT_MS) as dl:
            link.first.click()
        path = dl.value.path()
        if path:
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.info("download link did not yield a file: %s", exc)

    # Some APEX builds fire the download without expect_download catching it.
    if downloads:
        try:
            return downloads[-1].path().read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return None


def _outside_window(events: list[dict[str, Any]],
                    date_from: str, date_to: str) -> list[str]:
    """
    Export dates that fall outside the window that was asked for.

    `_set_dates` already reads the dates back and refuses a confident
    disagreement, and that check is about what the page will *run* — "the
    model took it" against "the model kept it". It is not evidence that the
    report ran again. `_generate_report` clicks Generate and then proves the
    click with the page's per-render token, but before that check existed a
    page that was never regenerated went undetected, still showing the
    report it loaded with: the *default* window, which is not the one that
    was just set on the items.

    That is the same class of danger the readback exists for, because the
    window's bounds are the bounds of the reconcile delete. A stale report
    read as this window's erases the window's real bookings, files them as
    cancellations, and reports success.

    So the export is made to prove its own window. Every row carries a date,
    and a report filtered to [from, to] cannot legitimately hold one outside
    it — so a row outside means the report is not the window, whatever the
    items say. A date that will not parse abstains, for the reason
    `_item_date` gives: stopping every scrape the day a format changes is the
    worse mistake of the two.

    What this catches is a stale report whose bounds *differ* — the untouched
    default, the previous window, a window a clamped date produced. What it
    cannot catch is a stale report whose bounds sit *inside* the requested
    one: every row is then a date this window could legitimately hold, and
    narrower-but-inside is exactly the shape APEX session state produces
    when a Generate click misses and the page keeps last scrape's render.
    That shape is the render token's to catch, not the dates': a Generate
    with no evidence of a re-render is refused by _generate_report, and a
    refused report is not reconciled whatever its dates say.
    """
    lo, hi = _item_date(date_from), _item_date(date_to)
    if lo is None or hi is None:
        return []
    out: list[str] = []
    for ev in events:
        day = str(ev.get("start") or "")[:10]
        try:
            when = date.fromisoformat(day)
        except ValueError:
            continue
        if when < lo or when > hi:
            out.append(day)
    return out


def _html_table_to_csv(page: Any) -> str:
    """Fallback: read the rendered report table off the page."""
    try:
        return page.evaluate(
            """() => {
                const tables = Array.from(document.querySelectorAll('table'));
                for (const t of tables) {
                    const rows = Array.from(t.querySelectorAll('tr'));
                    if (rows.length < 3) continue;
                    const text = rows.map(r => r.innerText).join(' ');
                    if (!/ACADEMIC|DEPARTMENT|Event\\/Course/i.test(text)) continue;
                    return rows.map(r =>
                        Array.from(r.querySelectorAll('th,td'))
                            .map(c => '"' + c.innerText.trim().replace(/"/g,'""') + '"')
                            .join(',')
                    ).join('\\n');
                }
                return '';
            }"""
        ) or ""
    except Exception as exc:
        log.warning("HTML table fallback failed: %s", exc)
        return ""
