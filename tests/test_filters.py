"""
The filter features: free/busy, the endpoints behind the panel, and the
storage the panel writes to.

Three claims are worth testing and the rest follows from them.

  * There is exactly one answer to "is this room busy". /api/today answers both
    the point-in-time question (Free Right Now) and the window question ("free
    from 14:00 for 60 min") through app.avail, and the cross-check below is what
    fails if that ever forks into two implementations that drift.
  * Half-open intervals are the intended reading, at both ends of a window, and
    an all-day row occupies its whole date rather than reading as free.
  * A user-controlled group name can reach storage, so the write path validates
    and replaces atomically, and a preset round-trips.

    python tests/test_filters.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the data directory at a scratch location *before* app.config is
# imported, so this never touches real scraped data or the live profile.
import os  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="lsm-filters-")
os.environ["LSM_DATA_DIR"] = _tmp

from app import avail, store  # noqa: E402
from app.config import ROOM_GROUPS  # noqa: E402
from app.server import create_app  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        # This machine's console is cp1252 and the assertions below carry the
        # arrow and the multiplication sign. A failed assertion should report
        # the mismatch, not die printing it.
        g = f"{got!r}".encode("ascii", "replace").decode()
        w = f"{want!r}".encode("ascii", "replace").decode()
        print(f"  FAIL  {label}\n          got:  {g}\n          want: {w}")


class StubOrch:
    """Stands in for the Playwright worker."""

    def __init__(self) -> None:
        self.busy = False

    def status(self) -> dict:
        return {"session": "ok", "session_message": "", "busy": self.busy,
                "busy_action": "", "progress": "", "last_scrape": None,
                "last_scrape_message": "", "backfill": None}

    def is_busy(self) -> bool:
        return self.busy


# ── Fixtures ─────────────────────────────────────────────────────────────

def booking(start: str, end: str | None, **over) -> dict:
    """A booking from ISO strings, so the cases below read as intervals."""
    b = {
        "title": "T", "room": "142", "start": start, "end": end,
        "date": start[:10], "description": "", "class_code": "",
        "cancelled": False, "all_day": False,
    }
    b.update(over)
    return b


def iso(day: str, clock: str) -> str:
    return f"{day}T{clock}:00"


DAY = "2026-09-22"          # a Tuesday, and nothing here depends on the weekday
NEXT = "2026-09-23"


def seeded_day(day: str = DAY) -> None:
    """
    Four rooms, chosen so the cross-check has something to disagree about:
    142 is booked across noon, 147 is free on the day, 368 is booked but not at
    noon. The instant the cross-check probes is 12:30 — mid-booking for 142,
    mid-free for 147 and 368 — and deliberately not on any booking boundary.

    147 is booked on the *next* day rather than left out entirely, because
    /api/today seeds its room list from every room in the data and not from the
    day's bookings. A room with nothing anywhere would not be in the response
    at all, which is a different assertion from "this room is free today".

    200 is booked at 00:30 the next day for the window that runs past midnight:
    it is free for the whole of `day`, and asking for three hours from 23:00
    reaches its booking. It is dated inside DAY..NEXT, so the later re-seed of
    that window in test_search_wildcards_are_literal clears it too.
    """
    store.replace_events([
        booking(iso(day, "09:00"), iso(day, "13:00"), room="142",
                title="Morning Lecture"),
        booking(iso(day, "08:00"), iso(day, "09:00"), room="368",
                title="Early"),
        booking(iso(NEXT, "10:00"), iso(NEXT, "11:00"), room="147",
                title="Tomorrow Only"),
        booking(iso(NEXT, "00:30"), iso(NEXT, "02:00"), room="200",
                title="Overnight Setup"),
    ], day, NEXT, run_id=900, trigger="manual")


# ── 2.1 the predicate ────────────────────────────────────────────────────

def test_busy_at() -> None:
    print("\nbusy_at — the point-in-time question")
    day = DAY
    bs = [booking(iso(day, "09:00"), iso(day, "12:00"))]

    check("before the booking is free", avail.busy_at(bs, iso(day, "08:00")), None)
    # The first instant belongs to the booking: a room booked from 14:00 is not
    # free at 14:00.
    check("the first instant is booked",
          bool(avail.busy_at(bs, iso(day, "09:00"))), True)
    check("mid-booking is booked",
          bool(avail.busy_at(bs, iso(day, "10:30"))), True)
    check("the end instant is free",
          avail.busy_at(bs, iso(day, "12:00")), None)
    check("an empty room is free", avail.busy_at([], iso(day, "10:00")), None)


def test_busy_between() -> None:
    print("\nbusy_between — the window question")
    day = DAY

    # Half-open at both ends: touching at a boundary is not colliding.
    bs = [booking(iso(day, "09:00"), iso(day, "12:00"))]
    check("a window starting exactly at the end is free",
          avail.busy_between(bs, iso(day, "12:00"), iso(day, "13:00")), None)
    check("a window ending exactly at the start is free",
          avail.busy_between(bs, iso(day, "08:00"), iso(day, "09:00")), None)
    check("a window straddling the end is busy",
          bool(avail.busy_between(bs, iso(day, "11:30"), iso(day, "12:30"))), True)
    check("a window inside the booking is busy",
          bool(avail.busy_between(bs, iso(day, "10:00"), iso(day, "10:30"))), True)
    check("a window swallowing the booking is busy",
          bool(avail.busy_between(bs, iso(day, "07:00"), iso(day, "15:00"))), True)

    # Back to back books out the seam: 12:00 belongs to the second booking, so
    # there is no free instant between them. The half-open rule is about the
    # edges of a booking, not about inventing a gap where two meet.
    back = [booking(iso(day, "09:00"), iso(day, "12:00")),
            booking(iso(day, "12:00"), iso(day, "15:00"))]
    check("a window starting at the seam belongs to the later booking",
          bool(avail.busy_between(back, iso(day, "12:00"), iso(day, "12:30"))), True)
    check("a window ending at the seam belongs to the earlier one",
          bool(avail.busy_between(back, iso(day, "11:00"), iso(day, "12:00"))), True)
    check("but the day either side of them is free",
          avail.busy_between(back, iso(day, "15:00"), iso(day, "16:00")), None)

    # A booking with no recoverable end must not span to the end of time.
    broken = [booking(iso(day, "10:00"), None)]
    check("a booking with no end does not block a later window",
          avail.busy_between(broken, iso(day, "13:00"), iso(day, "14:00")), None)

    check("an empty day has every window free",
          avail.busy_between([], iso(day, "09:00"), iso(day, "17:00")), None)


def test_all_day() -> None:
    print("\nall-day rows")
    day = DAY
    # A service block renders as 00:00-23:00 and is flagged all_day.
    block = [booking(iso(day, "00:00"), iso(day, "23:00"), room="003",
                     title="RENOVATIONS", all_day=True)]
    # A slot-index booking whose time could not be recovered has no end at all,
    # and used to read as *free all day despite existing*.
    no_end = [booking(iso(day, "00:00"), None, room="205", all_day=True)]

    check("an all-day row blocks the middle of its date",
          bool(avail.busy_between(block, iso(day, "10:00"), iso(day, "11:00"))), True)
    check("an all-day row blocks the last window of its date",
          bool(avail.busy_between(block, iso(day, "22:00"), iso(day, "24:00"))), True)
    check("an all-day row is booked at any instant on its date",
          bool(avail.busy_at(block, iso(day, "07:15"))), True)
    check("an all-day row blocks nothing the next day",
          avail.busy_between(block, iso(NEXT, "09:00"), iso(NEXT, "10:00")), None)
    check("an all-day row with no end still blocks",
          bool(avail.busy_at(no_end, iso(day, "14:00"))), True)

    yesterday = "2026-09-21"
    check("the day before is untouched",
          avail.busy_between(block, iso(yesterday, "09:00"), iso(yesterday, "10:00")), None)


# ── 2.2 the endpoint ─────────────────────────────────────────────────────

def test_today_window_and_crosscheck() -> None:
    print("\n/api/today — window, and the cross-check that nothing forked")
    client = create_app(StubOrch()).test_client()

    probe = iso(DAY, "12:30")

    point = client.get(f"/api/today?date={DAY}&at=12:30").get_json()
    check("the instant is echoed back", point["at"], probe)
    check("no window means minutes is 0", point["minutes"], 0)
    free_now = {r["room"] for r in point["rooms"] if r["status"] == "free"}
    check("142 is booked at 12:30", "142" in free_now, False)
    check("147 is free at 12:30", "147" in free_now, True)
    check("368 is free at 12:30", "368" in free_now, True)

    # The cross-check. `for=1` is a one-minute window, which is not zero-length:
    # the overlap test is start < end and booking_start < end, so a window of a
    # single minute behaves like the instant for every booking that is not
    # *exactly* starting at the probe. Probing at 12:30 rather than on a
    # boundary is what makes the two forms comparable — for=0 would be a
    # zero-length window and would read a booking beginning at the queried
    # instant as free, which busy_at correctly calls booked.
    window = client.get(f"/api/today?date={DAY}&at=12:30&for=1").get_json()
    check("a window reports its length", window["minutes"], 1)
    free_win = {r["room"] for r in window["rooms"] if r["status"] == "free"}
    check("the window and the instant agree", free_win, free_now)
    check("they agree on the booked side too",
          {r["room"] for r in window["rooms"] if r["status"] == "booked"},
          {r["room"] for r in point["rooms"] if r["status"] == "booked"})

    # A window that outlasts the booking is not the same question.
    later = client.get(f"/api/today?date={DAY}&at=12:30&for=120").get_json()
    free_later = {r["room"] for r in later["rooms"] if r["status"] == "free"}
    check("142 is not free for the next two hours", "142" in free_later, False)
    check("147 still is", "147" in free_later, True)

    # Every active room, not just the ones with a booking — the emptiest rooms
    # are the ones you want, and they used to be missing entirely.
    check("a room with no bookings that day is present",
          "368" in {r["room"] for r in point["rooms"]}, True)
    check("the free count matches the statuses",
          point["free"], sum(1 for r in point["rooms"] if r["status"] == "free"))

    bad = client.get("/api/today?date=nonsense")
    check("a junk date is refused", bad.status_code, 400)
    check("minutes are capped at a day",
          client.get(f"/api/today?date={DAY}&at=09:00&for=99999").get_json()["minutes"],
          24 * 60)


def test_today_batch_is_per_day() -> None:
    """The month view's question: the same window, asked of many days at once.

    The defect this exists for: the free-at filter asked about the day on
    screen and then applied that one answer to every booking in the month, so a
    room booked all month read as free. The batch is per-day, and the
    cross-check below is what fails if it ever becomes a second implementation
    that drifts from the single-day form.
    """
    print("\n/api/today?dates= — a month of days in one call")
    client = create_app(StubOrch()).test_client()

    at = "10:30"
    batch = client.get(
        f"/api/today?dates={DAY},{NEXT}&at={at}&for=60"
    ).get_json()

    check("the batch reports its window length", batch["minutes"], 60)
    check("it answers for every day asked",
          sorted(batch["days"]), sorted([DAY, NEXT]))

    # The cross-check: the batch must be the single-day question asked
    # repeatedly, not a parallel implementation of it.
    for d in (DAY, NEXT):
        one = client.get(f"/api/today?date={d}&at={at}&for=60").get_json()
        check(f"free rooms on {d} match the single-day call",
              set(batch["days"][d]["free"]),
              {r["room"] for r in one["rooms"] if r["status"] == "free"})
        check(f"the booking count on {d} matches",
              batch["days"][d]["total_bookings"], one["total_bookings"])

    # And the point of the whole thing. 147 is booked on NEXT and free on DAY;
    # 142 is the other way round. One answer applied to both days would have to
    # get one of these wrong.
    check("147 is free on the first day", "147" in batch["days"][DAY]["free"], True)
    check("147 is booked on the next day", "147" in batch["days"][NEXT]["free"], False)
    check("142 is booked on the first day", "142" in batch["days"][DAY]["free"], False)
    check("142 is free on the next day", "142" in batch["days"][NEXT]["free"], True)
    check("a room free on both days is free on both",
          "368" in batch["days"][DAY]["free"]
          and "368" in batch["days"][NEXT]["free"], True)

    # One day at a time, for a caller that wants both forms to agree.
    single = client.get(f"/api/today?dates={DAY}&at={at}&for=60").get_json()
    check("a batch of one matches that day's entry",
          single["days"][DAY]["free"], batch["days"][DAY]["free"])

    # Rejections. A batch is the wrong place to guess at a date.
    check("a loose date is refused rather than guessed",
          client.get("/api/today?dates=2026-3-1&at=10:00").status_code, 400)
    check("a date carrying a time is refused",
          client.get("/api/today?dates=2026-03-01T10:00&at=10:00").status_code, 400)
    check("an empty list is refused",
          client.get("/api/today?dates=&at=10:00").status_code, 400)
    check("a junk date among good ones is refused",
          client.get(f"/api/today?dates={DAY},nonsense&at=10:00").status_code, 400)

    # The cap: a month cell is 42 days, so 62 covers every view and still
    # refuses unbounded work.
    base = date(2026, 3, 1)
    span = lambda n: ",".join((base + timedelta(days=i)).isoformat() for i in range(n))
    check("the cap itself is served",
          client.get(f"/api/today?dates={span(62)}&at=10:00").status_code, 200)
    check("one day over the cap is refused",
          client.get(f"/api/today?dates={span(63)}&at=10:00").status_code, 400)


def test_a_window_that_crosses_midnight() -> None:
    """A window opened late enough to reach tomorrow must see tomorrow's rows.

    "Free from 23:00 for 180 min" is a question about three hours that end at
    02:00 the next day, and the rows were filtered to the day the question was
    asked about. Room 200 is booked at 00:30 the next day and free for the whole
    of the day asked about, so it read as free for a window it was not free for.
    Of everything here that is the one wrong answer that sends someone to a room
    that is already taken.

    The predicate is left to decide, rather than the window being refused
    whenever it crosses midnight: an hour from 23:00 stops at midnight and is
    still free, which is the case that fails if the fix widens the *answer*
    instead of the rows the answer is computed from.
    """
    print("\n/api/today — a window that runs past midnight")
    client = create_app(StubOrch()).test_client()

    def free_rooms(payload: dict) -> set:
        return {r["room"] for r in payload["rooms"] if r["status"] == "free"}

    # The control. Nothing here depends on the window: at the instant it is
    # booked, 200 is booked.
    check("200 is booked at the instant its booking covers",
          "200" in free_rooms(
              client.get(f"/api/today?date={NEXT}&at=01:00").get_json()),
          False)

    crossed = client.get(f"/api/today?date={DAY}&at=23:00&for=180").get_json()
    check("the window is the one that crosses", crossed["minutes"], 180)
    check("a room booked just past midnight is not free for the window",
          "200" in free_rooms(crossed), False)
    # The widening is the rows, not a refusal: 147 is booked the next day at
    # 10:00, which three hours from 23:00 do not reach.
    check("a room booked later the next day is still free",
          "147" in free_rooms(crossed), True)
    # And the count stayed a count of the day the response names: DAY has two
    # bookings of its own, NEXT has two more that the predicate now sees.
    check("the day's booking count did not grow with the window",
          crossed["total_bookings"], 2)
    check("the payload's rows are still the day's",
          sorted({b["room"] for r in crossed["rooms"] for b in r["bookings"]}),
          ["142", "368"])
    # `next` is "next today" and not "next ever", for the same reason.
    check("a room with nothing left today is still free all day",
          next(r for r in crossed["rooms"] if r["room"] == "147")["next_at"],
          None)

    # An hour from 23:00 stops at midnight. The booking half an hour past it is
    # not in the window, so calling it booked would be a false refusal.
    to_midnight = client.get(f"/api/today?date={DAY}&at=23:00&for=60").get_json()
    check("a window that stops at midnight is unaffected",
          "200" in free_rooms(to_midnight), True)

    # The batch asks the same question of a month of days, and must not answer
    # it differently. This is what fails if only one of the two forms learns to
    # look past midnight.
    batch = client.get(
        f"/api/today?dates={DAY},{NEXT}&at=23:00&for=180"
    ).get_json()
    check("the batch agrees about the day asked",
          set(batch["days"][DAY]["free"]), free_rooms(crossed))
    check("the batch does not call it free either",
          "200" in batch["days"][DAY]["free"], False)
    check("the batch counts the day's own bookings",
          batch["days"][DAY]["total_bookings"], 2)

    # The narrowing has to widen the *read*, not just the day set: a batch
    # whose last day is the day the window opens on still needs the day the
    # window crosses into. The SQL upper bound is the last date plus one, and
    # without that one day 200's 00:30 booking was never read — the batch
    # called a taken room free, exactly the answer that sends someone to a
    # room that is already occupied.
    last_day = client.get(
        f"/api/today?dates={DAY}&at=23:00&for=180"
    ).get_json()
    check("a batch ending on the opening day still sees the day it crosses into",
          "200" in last_day["days"][DAY]["free"], False)
    check("...and that day's own count stays the day's",
          last_day["days"][DAY]["total_bookings"], 2)
    # NEXT's own rows are both outside this window — one at 00:30, one at 10:00
    # — so the day after is not dragged into the day before's answer.
    check("the next day's own answer is about the next day",
          set(batch["days"][NEXT]["free"]), {"142", "147", "200", "368"})


# ── 2.8 the feed ─────────────────────────────────────────────────────────

def test_changes_filters() -> None:
    print("\n/api/changes — q and rooms")
    client = create_app(StubOrch()).test_client()

    all_rows = client.get("/api/changes?limit=500").get_json()
    check("the feed has rows to narrow", all_rows["count"] > 0, True)

    one = client.get("/api/changes?limit=500&rooms=142").get_json()
    check("rooms narrows to that room",
          {c["room"] for c in one["changes"]}, {"142"})
    check("the count comes from the filtered set",
          one["count"], len(one["changes"]))

    two = client.get("/api/changes?limit=500&rooms=142,368").get_json()
    check("a CSV of rooms widens again",
          {c["room"] for c in two["changes"]}, {"142", "368"})

    # An absent `rooms` means no restriction. An *empty* one must too — the
    # same trap get_events documents, and the reason the client omits the
    # parameter rather than sending an empty string when nothing is filtered.
    check("an empty rooms is no restriction, not an empty feed",
          client.get("/api/changes?limit=500&rooms=").get_json()["count"],
          all_rows["count"])

    q = client.get("/api/changes?limit=500&q=Morning").get_json()
    check("q narrows by title",
          all(c["title"] == "Morning Lecture" for c in q["changes"]), True)
    check("q finds fewer than everything", q["count"] < all_rows["count"], True)

    none = client.get("/api/changes?limit=500&q=zzzznotpresent").get_json()
    check("a query matching nothing is empty, not everything", none["count"], 0)

    both = client.get("/api/changes?limit=500&rooms=368&q=Morning").get_json()
    check("q and rooms are ANDed, not ORed", both["count"], 0)


# ── 2.7 search ───────────────────────────────────────────────────────────

def test_search_wildcards_are_literal() -> None:
    """A `_` typed into the search box is a `_`, not "any one character".

    The pattern was built as f"%{q}%" with no escaping and no ESCAPE clause,
    so the two characters LIKE reserves as wildcards arrived as wildcards.
    Typing `_` -- which is exactly what a room code uses -- returned every
    booking in the window, and a search for L1060_A also matched L1060XA as
    though the underscore were not there. Both are silent failures: the
    result still looks like a result, just too many of them.

    Asserted at the store, because that is where the pattern is built, and
    through the route, because that is where `q` comes from.
    """
    print("\nsearch — LIKE wildcards are literal")

    # One call, so the state asserted below is the state this sets. The window
    # is DAY..NEXT, matching seeded_day's, because replace_events reconciles
    # the window it is given -- a narrower one here would delete the rest.
    store.replace_events([
        booking(iso(DAY, "09:00"), iso(DAY, "13:00"), room="142",
                title="Morning Lecture"),
        booking(iso(DAY, "08:00"), iso(DAY, "09:00"), room="368", title="Early"),
        booking(iso(NEXT, "10:00"), iso(NEXT, "11:00"), room="147",
                title="Tomorrow Only"),
        # The pair that makes the underscore visible: one room has one, the
        # other has a different character where the underscore is.
        booking(iso(DAY, "14:00"), iso(DAY, "15:00"), room="L1060_A",
                title="Underscore Room"),
        booking(iso(DAY, "15:00"), iso(DAY, "16:00"), room="L1060XA",
                title="Lookalike Room"),
    ], DAY, NEXT, run_id=901, trigger="manual")

    total = len(store.get_events(limit=500))
    check("there is data for an unescaped wildcard to over-match", total, 5)

    # The symptom as a user meets it. Unescaped, each of these matched all
    # five rows -- everything in the window. `_` must now match only the one
    # row that genuinely has an underscore in it, and `%` must match nothing,
    # because no booking here contains a percent sign. The count going 5 -> 1
    # is the whole fix; a count of 5 would mean the wildcard is still live.
    check("a bare '_' matches only the room that really has one",
          [e["room"] for e in store.get_events(q="_", limit=500)],
          ["L1060_A"])
    check("a bare '%' matches nothing, not everything",
          len(store.get_events(q="%", limit=500)), 0)

    check("an underscore matches the room that has one",
          [e["room"] for e in store.get_events(q="L1060_A", limit=500)],
          ["L1060_A"])
    check("...and not the room with the same shape but no underscore",
          [e["room"] for e in store.get_events(q="L1060XA", limit=500)],
          ["L1060XA"])

    # The backslash is the escape character now, so it has to be escaped too
    # -- and escaping it *first*. Left raw, `\L1060` would mean "the literal
    # character L, then 1060", which matches both rooms above rather than
    # neither. This is what catches the half-fix that adds ESCAPE to the SQL
    # but forgets the pattern's own backslash.
    check("a typed backslash cannot make the next character literal",
          len(store.get_events(q="\\L1060", limit=500)), 0)

    # Ordinary search is untouched, case-insensitivity included.
    check("plain search still finds its booking",
          [e["title"] for e in store.get_events(q="MORNING", limit=500)],
          ["Morning Lecture"])

    # The change feed builds the same pattern at a second call site, so
    # fixing only get_events would leave the changes view over-matching.
    check("the change feed escapes it too",
          {c["room"] for c in store.get_changes(q="_", limit=500)},
          {"L1060_A"})
    found = store.get_changes(q="Morning", limit=500)
    check("...and still finds by title there",
          [c["title"] for c in found], ["Morning Lecture"])

    client = create_app(StubOrch()).test_client()
    check("the changes route passes it through",
          {c["room"] for c in
           client.get("/api/changes?limit=500&q=_").get_json()["changes"]},
          {"L1060_A"})


# ── 2.6 groups ───────────────────────────────────────────────────────────

def test_groups() -> None:
    print("\ngroups — validation, atomicity, and the fallback copy")
    client = create_app(StubOrch()).test_client()

    for bad in ({}, [], "Classroom", {"": ["142"]}, {"North": "142"},
                {"North": [""]}, {"North": [7]}):
        check(f"rejected: {bad!r}"[:60],
              client.post("/api/room-groups", json=bad).status_code, 400)

    check("a valid map is accepted",
          client.post("/api/room-groups",
                      json={"Mine": ["142", "147"]}).status_code, 200)
    check("it comes back",
          client.get("/api/room-groups").get_json(), {"Mine": ["142", "147"]})

    # Replace, not merge — the file is the source of truth, and the panel
    # always submits the whole map because it renders from the effective set.
    client.post("/api/room-groups", json={"Other": ["368"]})
    check("a second save replaces rather than merges",
          client.get("/api/room-groups").get_json(), {"Other": ["368"]})

    # No .tmp sibling left behind, and the file is parseable JSON. DATA_DIR is
    # the scratch directory itself when LSM_DATA_DIR is set — there is no
    # nested data/ level on that path.
    d = Path(_tmp)
    leftovers = sorted(p.name for p in d.glob("room_groups.json*"))
    check("no temp file is left behind", leftovers, ["room_groups.json"])
    check("the file parses",
          json.loads((d / "room_groups.json").read_text(encoding="utf-8")),
          {"Other": ["368"]})

    # Put the defaults back, then check the fallback path hands out copies.
    (d / "room_groups.json").unlink()
    fallback = store.load_groups()
    check("the fallback is the built-in set",
          sorted(fallback), sorted(ROOM_GROUPS))
    fallback["North"].append("999")
    check("mutating the fallback does not edit app.config",
          "999" in ROOM_GROUPS["North"], False)
    check("Classroom is seeded with the union of North and South",
          sorted(ROOM_GROUPS["Classroom"]),
          sorted(set(ROOM_GROUPS["North"]) | set(ROOM_GROUPS["South"])))


# ── 2.7 presets ──────────────────────────────────────────────────────────

def test_presets() -> None:
    print("\n/api/presets")
    client = create_app(StubOrch()).test_client()

    check("none to start with", client.get("/api/presets").get_json()["presets"], [])

    f = {"rooms": ["142"], "seats": 20, "panopto": True, "free": "14:00", "mins": 60}
    body = client.post("/api/presets", json={"name": "Afternoon", "filters": f}).get_json()
    check("saving returns the whole list",
          [p["name"] for p in body["presets"]], ["Afternoon"])
    check("the filters survive the round trip",
          client.get("/api/presets").get_json()["presets"][0]["filters"], f)

    client.post("/api/presets", json={"name": "Quiet", "filters": {"seats": 50}})
    names = [p["name"] for p in client.get("/api/presets").get_json()["presets"]]
    check("a second preset is appended", names, ["Afternoon", "Quiet"])

    client.post("/api/presets", json={"name": "Quiet", "filters": {"seats": 10}})
    got = client.get("/api/presets").get_json()["presets"]
    check("the same name overwrites rather than duplicating",
          [p["name"] for p in got], ["Afternoon", "Quiet"])
    check("and it is the new filters that are kept", got[1]["filters"], {"seats": 10})

    check("an empty name is refused",
          client.post("/api/presets", json={"name": "  ", "filters": {}}).status_code, 400)
    check("a 61-character name is refused",
          client.post("/api/presets",
                      json={"name": "x" * 61, "filters": {}}).status_code, 400)
    check("a non-dict filters is refused",
          client.post("/api/presets",
                      json={"name": "ok", "filters": []}).status_code, 400)
    check("a non-object body is refused",
          client.post("/api/presets", json=[1, 2]).status_code, 400)

    check("delete needs a name",
          client.delete("/api/presets").status_code, 400)
    body = client.delete(f"/api/presets?name={client.get('/api/presets').get_json()['presets'][0]['name']}").get_json()
    check("delete removes exactly one", [p["name"] for p in body["presets"]], ["Quiet"])
    client.delete("/api/presets?name=Quiet")
    check("and the list can empty again",
          client.get("/api/presets").get_json()["presets"], [])

    # The cap is on one JSON blob in one kv row, so it has to hold.
    for i in range(store.PRESET_LIMIT + 5):
        store.save_preset(f"p{i}", {"seats": i})
    kept = store.load_presets()
    check("the cap holds", len(kept), store.PRESET_LIMIT)
    check("the newest is kept", kept[-1]["name"], f"p{store.PRESET_LIMIT + 4}")
    check("the oldest is dropped", kept[0]["name"], "p5")


def main() -> int:
    print("=" * 60)
    print("  filters")
    print("=" * 60)

    store.init_db()
    seeded_day()

    test_busy_at()
    test_busy_between()
    test_all_day()
    test_today_window_and_crosscheck()
    test_today_batch_is_per_day()
    test_a_window_that_crosses_midnight()
    test_changes_filters()
    test_search_wildcards_are_literal()
    test_groups()
    test_presets()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
