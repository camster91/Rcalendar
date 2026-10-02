"""
Retention, backfill and the change feed.

Covers the three claims the feature rests on: that re-scraping identical data
is silent (so the feed means something), that a booking which vanishes leaves a
readable trace, and that a year of history survives the prune that follows it.

    python tests/test_changes.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the data directory at a scratch location *before* app.config is
# imported, so this never touches real scraped data or the live profile.
import os  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="lsm-changes-")
os.environ["LSM_DATA_DIR"] = _tmp

from app import store  # noqa: E402
from app.config import BACKFILL_MONTHS, KEEP_DAYS, SCRAPE_MONTHS_BACK  # noqa: E402
from app.scrape import backfill_windows  # noqa: E402
from app.server import create_app  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        # Values here include dd/mm/yyyy → dd/mm/yyyy windows, and the console
        # on this machine is cp1252, where the arrow is unprintable. A failed
        # assertion should report the mismatch, not die printing it.
        g = f"{got!r}".encode("ascii", "replace").decode()
        w = f"{want!r}".encode("ascii", "replace").decode()
        print(f"  FAIL  {label}\n          got:  {g}\n          want: {w}")


def ok(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


# ── Fixtures ─────────────────────────────────────────────────────────────

def ev(title: str, room: str, day: int, hour: int = 9, hours: int = 2) -> dict:
    """A booking `day` days from today, at `hour` o'clock."""
    when = datetime.combine(date.today() + timedelta(days=day), dtime(hour, 0))
    return {
        "title": title, "room": room,
        "start": when.isoformat(),
        "end": (when + timedelta(hours=hours)).isoformat(),
        "description": "Lecture", "class_code": "A", "cancelled": False,
    }


def win(a: int, b: int) -> tuple[str, str]:
    """A dd/mm/yyyy window from `a` to `b` days from today."""
    fmt = "%d/%m/%Y"
    return ((date.today() + timedelta(days=a)).strftime(fmt),
            (date.today() + timedelta(days=b)).strftime(fmt))


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


# ── Tests ────────────────────────────────────────────────────────────────

def test_change_detection() -> None:
    print("\nchange detection")
    w_from, w_to = win(-60, -10)
    evs = [ev("Alpha Lecture", "142", -50),
           ev("Beta Session", "147", -45),
           ev("Gamma Talk", "368", -40)]

    n = store.replace_events(evs, w_from, w_to, run_id=1, trigger="manual")
    check("rows written", n, 3)
    rows = store.get_changes(limit=500)
    check("a first scrape logs every booking as an addition", len(rows), 3)
    check("all of them are additions", {r["kind"] for r in rows}, {"added"})
    check("the addition carries the booking",
          sorted(r["title"] for r in rows),
          ["Alpha Lecture", "Beta Session", "Gamma Talk"])

    # Idempotence — the guarantee the whole feed rests on. Re-scraping an
    # unchanged window must be completely silent, or the feed is noise.
    store.replace_events(evs, w_from, w_to, run_id=2, trigger="schedule")
    check("an identical re-scrape logs nothing", len(store.get_changes(limit=500)), 3)

    # A booking the report no longer lists is a removal — and its feed row has
    # to describe it, because the events row it came from is gone.
    store.replace_events(evs[:1], w_from, w_to, run_id=3)
    removed = store.get_changes(kind="removed", limit=500)
    check("the two vanished bookings are logged", len(removed), 2)
    check("a removal keeps its title",
          sorted(r["title"] for r in removed),
          ["Beta Session", "Gamma Talk"])
    check("a removal keeps its room",
          sorted(r["room"] for r in removed), ["147", "368"])
    check("a removal keeps its time", all(r["start"] for r in removed), True)
    check("the removed booking is gone from events",
          len(store.get_events(room="147")), 0)


def test_all_day_changes_keep_the_flag() -> None:
    print("\nan all-day change row carries all_day")
    w_from, w_to = win(-60, -10)
    day = date.today() - timedelta(days=35)
    block = {
        "title": "003/RENOVATIONS", "room": "L1045",
        "start": datetime.combine(day, dtime(0, 0)).isoformat(),
        "end": datetime.combine(day, dtime(23, 0)).isoformat(),
        "description": "Service block", "class_code": "", "cancelled": False,
        "all_day": True,
    }
    keeper = ev("Iota Keeper", "L1045", -35, hour=13)
    store.replace_events([block, keeper], w_from, w_to, run_id=60)

    added = {r["title"]: r for r in store.get_changes(kind="added", limit=500)
             if r["room"] == "L1045"}
    check("both additions are logged", sorted(added), ["003/RENOVATIONS",
                                                       "Iota Keeper"])
    # Without this the feed renders 00:00 → 23:00 as a real 23-hour booking,
    # which is the same display bug the card view already carries a fix for.
    check("the service block is marked all-day",
          added["003/RENOVATIONS"]["all_day"], True)
    check("the ordinary booking is not", added["Iota Keeper"]["all_day"], False)

    # The removal side reads a snapshot of `events`, which has the column — but
    # only if the reconcile SELECT actually fetches it. Leaving it out raises
    # IndexError on the first removal of any scrape.
    store.replace_events([keeper], w_from, w_to, run_id=61)
    gone = [r for r in store.get_changes(kind="removed", limit=500)
            if r["room"] == "L1045"]
    check("the block's removal is logged", len(gone), 1)
    check("and it is still marked all-day", gone[0]["all_day"], True)


def test_narrow_rescrape_adds_nothing() -> None:
    print("\na narrow re-scrape of wider data adds nothing")
    w_from, w_to = win(-120, -60)
    evs = [ev("Delta Wide A", "L1010", -110),
           ev("Delta Wide B", "L1010", -90),
           ev("Delta Wide C", "L1010", -70)]
    store.replace_events(evs, w_from, w_to, run_id=10)
    base = len(store.get_changes(limit=2000))

    # Re-scrape only the middle booking's month. Everything reported is
    # already stored — worked out against the whole table rather than the
    # window, this would log a phantom addition.
    narrow_from, narrow_to = win(-95, -85)
    store.replace_events([evs[1]], narrow_from, narrow_to, run_id=11)
    check("no additions from a narrow window",
          len(store.get_changes(limit=2000)), base)
    check("rows outside the narrow window survive",
          len(store.get_events(room="L1010")), 3)


def test_edited_booking_is_a_remove_and_an_add() -> None:
    print("\nan edited booking is a removal plus an addition")
    w_from, w_to = win(-60, -10)
    store.replace_events([ev("Epsilon Seminar", "L1020", -30, hour=13)],
                         w_from, w_to, run_id=20)
    # Same title, same room, an hour later. The report carries no booking id,
    # so this is indistinguishable from a cancel-and-rebook, and that is what
    # the feed reports. Nothing here infers a "move".
    store.replace_events([ev("Epsilon Seminar", "L1020", -30, hour=15)],
                         w_from, w_to, run_id=21)
    newest = store.get_changes(limit=2)
    check("the edit produced one addition and one removal",
          sorted(r["kind"] for r in newest), ["added", "removed"])
    check("both rows describe the same booking",
          {(r["title"], r["room"]) for r in newest},
          {("Epsilon Seminar", "L1020")})


def test_backfill_logs_no_changes() -> None:
    print("\na backfill stores rows but logs no changes")
    w_from, w_to = win(-200, -150)
    evs = [ev("Zeta Backfill A", "L1025", -190),
           ev("Zeta Backfill B", "L1025", -170)]
    before = len(store.get_changes(include_backfill=True, limit=2000))

    n = store.replace_events(evs, w_from, w_to, run_id=30,
                             trigger="backfill", record_changes=False)
    check("rows are still stored", n, 2)
    check("the stored rows read back", len(store.get_events(room="L1025")), 2)
    check("nothing reached the feed",
          len(store.get_changes(include_backfill=True, limit=2000)), before)


def test_partial_report_adds_but_never_deletes() -> None:
    """A report that is only part of the window may not delete what it omits.

    The empty-report guard above catches the case where a failed render shows
    *nothing*. This is the case it cannot see: a render that shows some of the
    window and not the rest. `replace_events` was written to treat every
    non-empty report as the whole report, so a scrape that read three bookings
    out of two hundred deleted the other hundred and ninety-seven and filed
    them as cancellations — bookings that were never cancelled, gone from the
    calendar, with a change feed that reads as if they had been.

    That the scrape can produce such a report is not hypothetical:
    `scrape()` falls back to reading the rendered results table when the
    Download link yields no file, and the rendered table is one page of an
    interactive report. So the caller marks the result incomplete, and the
    incomplete case is what this asserts — adds land, deletes do not.
    """
    print("\na partial report adds but never deletes")
    w_from, w_to = win(-45, -5)

    store.replace_events([ev("Iota Guard A", "L1035", -40),
                          ev("Iota Guard B", "L1035", -35),
                          ev("Iota Guard C", "L1035", -30)],
                         w_from, w_to, run_id=50)
    check("three stored", len(store.get_events(room="L1035")), 3)

    # One booking re-reported, the other two simply missing from the page.
    # Complete would mean two deletions; incomplete must mean none.
    n = store.replace_events([ev("Iota Guard A", "L1035", -40)],
                             w_from, w_to, run_id=51, complete=False)
    check("the report is still stored", n, 1)
    check("the bookings it did not show survive",
          len(store.get_events(room="L1035")), 3)
    check("no phantom removals reached the feed",
          len([r for r in store.get_changes(kind="removed", limit=2000)
               if r["room"] == "L1035"]), 0)

    # And the same report *with* completeness still deletes, so the guard is
    # what is doing the work rather than the fixture never being able to fail.
    store.replace_events([ev("Iota Guard A", "L1035", -40)],
                         w_from, w_to, run_id=52, complete=True)
    check("a complete report still reconciles",
          len(store.get_events(room="L1035")), 1)
    check("...and logs what it removed",
          len([r for r in store.get_changes(kind="removed", limit=2000)
               if r["room"] == "L1035"]), 2)

    # The additions an incomplete report carries are real evidence and must
    # still land: a booking that has appeared has appeared, whatever else the
    # page failed to show.
    n = store.replace_events([ev("Iota Guard D", "L1035", -20)],
                             w_from, w_to, run_id=53, complete=False)
    check("a booking the partial report showed is stored", n, 1)
    ok("...and is readable", any(e["title"] == "Iota Guard D"
                                 for e in store.get_events(room="L1035")))
    check("...and the one it omitted still survives",
          len(store.get_events(room="L1035")), 2)

    # The default stays complete. Every caller that has not thought about it
    # keeps the reconcile it had, so this cannot quietly disarm the deletions
    # cancellations depend on.
    store.replace_events([ev("Iota Guard A", "L1035", -40)],
                         w_from, w_to, run_id=54)
    check("omitting the flag reconciles as before",
          len(store.get_events(room="L1035")), 1)


def test_empty_report_does_not_wipe() -> None:
    print("\nan empty report does not empty a window")
    w_from, w_to = win(-60, -10)
    store.replace_events([ev("Eta Guard A", "L1030", -55),
                          ev("Eta Guard B", "L1030", -52)],
                         w_from, w_to, run_id=40)
    check("two stored", len(store.get_events(room="L1030")), 2)

    # A report that renders as "no data found" is a failed render far more
    # often than it is a whole window of genuine cancellations.
    check("nothing written", store.replace_events([], w_from, w_to, run_id=41), 0)
    check("the bookings survive", len(store.get_events(room="L1030")), 2)
    check("no phantom removal was logged",
          len([r for r in store.get_changes(kind="removed", limit=2000)
               if r["room"] == "L1030"]), 0)


def test_explicit_cancellation_is_removed() -> None:
    """A row the report marks cancelled is evidence, not absence.

    The scheduler drops cancelled rows before replace_events, so without the
    `cancelled` uids the store could not tell "cancelled" from "not seen" —
    and both guards above exist for "not seen". A partial report or a window
    where every row was cancelled left the cancelled booking live.
    """
    print("\nan explicit cancellation is removed whatever the guards say")
    w_from, w_to = win(-45, -5)
    a, b, c = (ev("Mu Cancel A", "L1040", -40), ev("Mu Cancel B", "L1040", -35),
               ev("Mu Cancel C", "L1040", -30))
    store.replace_events([a, b, c], w_from, w_to, run_id=70)

    def removed() -> list[str]:
        return sorted(r["title"] for r in store.get_changes(
            kind="removed", room="L1040", limit=500))

    def live() -> list[str]:
        return sorted(e["title"] for e in store.get_events(room="L1040"))

    # Partial report: A seen, B marked cancelled, C simply missing.
    store.replace_events([a], w_from, w_to, run_id=71, complete=False,
                         cancelled=[store.event_uid(b)])
    check("a partial report removes the booking it marks cancelled",
          live(), ["Mu Cancel A", "Mu Cancel C"])
    check("...and logs it, but not the one it merely omitted",
          removed(), ["Mu Cancel B"])

    # Every row in the window cancelled: the empty-report guard still holds
    # for the rows not named, but the named ones go.
    store.replace_events([], w_from, w_to, run_id=72,
                         cancelled=[store.event_uid(a)])
    check("an all-cancelled report removes what it names",
          live(), ["Mu Cancel C"])
    check("...and logs it", removed(), ["Mu Cancel A", "Mu Cancel B"])

    # A uid both reported live and named cancelled stays: live wins.
    store.replace_events([c], w_from, w_to, run_id=73,
                         cancelled=[store.event_uid(c)])
    check("a booking also reported live is kept", live(), ["Mu Cancel C"])
    check("...and nothing new is logged", removed(),
          ["Mu Cancel A", "Mu Cancel B"])


def test_window_moving_forward_is_not_a_flood() -> None:
    """A month the window has just reached holds first observations, not adds.

    On the 1st of every month the daily window gains a month, and every
    booking in it used to be logged as "added" — ~2,000 rows burying the
    real feed. Only what the previous good scrape already covered can change.
    """
    print("\nthe window moving forward a month logs no flood")
    y = date.today().year + 1

    def at(m: int, d: int, title: str, yr: int = y) -> dict:
        when = datetime(yr, m, d, 10, 0)
        return {"title": title, "room": "L1050", "start": when.isoformat(),
                "end": (when + timedelta(hours=1)).isoformat(),
                "description": "", "class_code": "A", "cancelled": False}

    def feed(rid: int, kind: str | None = None) -> list[str]:
        # By room too: earlier fixtures hard-code small run_ids.
        return sorted(r["title"] for r in store.get_changes(
            run_id=rid, kind=kind, room="L1050", limit=500))

    sep_dec = [at(9, 10, "Kappa Sep"), at(10, 5, "Kappa Oct"),
               at(12, 1, "Kappa Dec")]
    r1 = store.start_run("schedule")
    store.replace_events(sep_dec, f"01/09/{y}", f"31/12/{y}",
                         run_id=r1, trigger="schedule")
    store.finish_run(r1, "ok", 3, f"01/09/{y}", f"31/12/{y}")

    jan = [at(1, 12, "Kappa Jan A", y + 1), at(1, 20, "Kappa Jan B", y + 1)]
    r2 = store.start_run("schedule")
    store.replace_events(sep_dec + jan + [at(10, 20, "Kappa Oct New")],
                         f"01/09/{y}", f"31/01/{y + 1}",
                         run_id=r2, trigger="schedule")
    store.finish_run(r2, "ok", 6, f"01/09/{y}", f"31/01/{y + 1}")
    check("only the booking inside the old reach is an addition",
          feed(r2), ["Kappa Oct New"])
    check("the newly visible month is still stored",
          len([e for e in store.get_events(room="L1050")
               if e["title"].startswith("Kappa Jan")]), 2)

    # Once a scrape has covered January, January can change like any month.
    r3 = store.start_run("schedule")
    store.replace_events(sep_dec + jan[:1] + [at(10, 20, "Kappa Oct New"),
                                               at(1, 25, "Kappa Jan C", y + 1)],
                         f"01/09/{y}", f"31/01/{y + 1}",
                         run_id=r3, trigger="schedule")
    check("a later add in the covered month is logged", feed(r3, "added"),
          ["Kappa Jan C"])
    check("...and the removal beside it is unchanged", feed(r3, "removed"),
          ["Kappa Jan B"])

    # Leave scrape_runs as it was: later tests read the last run, and an
    # earlier date_to would gate their additions.
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("DELETE FROM scrape_runs WHERE id IN (?,?,?)",
                     (r1, r2, r3))


def test_stats_are_recomputed_only_when_the_events_change() -> None:
    """stats() answers every status poll, so it caches — but never stale."""
    print("\nstats cache")

    import sqlite3
    import threading

    first = store.stats()
    computed: list[int] = []
    real_compute = store._compute_stats

    def counting(conn):
        computed.append(1)
        return real_compute(conn)

    store._compute_stats = counting
    try:
        again = store.stats()
        check("an unchanged store answers the same", again, first)
        check("...without scanning the events table again", len(computed), 0)

        # The server answers each poll on a fresh thread, and the cache used
        # to be per thread, so it never hit. Two threads, one computation.
        store.set_kv("stats_probe", 1)       # a write: the next read recounts
        answers: list[dict] = []
        threads = [threading.Thread(target=lambda: answers.append(store.stats()))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("two threads after a write share one computation",
              len(computed), 1)
        check("...and agree", answers[0] == answers[1] == first, True)

        # A write through this connection.
        store.replace_events(
            [{"title": "Stats probe", "room": "STATS1",
              "start": "2026-05-04T09:00:00", "end": "2026-05-04T10:00:00",
              "description": "", "class_code": "A", "cancelled": False}],
            "04/05/2026", "04/05/2026", run_id=None, record_changes=False)
        check("a write through the store is seen",
              store.stats()["total_events"], first["total_events"] + 1)

        # A write through another connection, as the worker thread's or a
        # raw sqlite3 handle's would be.
        other = sqlite3.connect(store.DB_PATH)
        try:
            other.execute("DELETE FROM events WHERE room = 'STATS1'")
            other.commit()
        finally:
            other.close()
        check("a write through another connection is seen",
              store.stats()["total_events"], first["total_events"])

        # And a write from another *thread* through the store, as the
        # scheduler's would be.
        t = threading.Thread(target=lambda: store.replace_events(
            [{"title": "Stats probe 2", "room": "STATS2",
              "start": "2026-05-05T09:00:00", "end": "2026-05-05T10:00:00",
              "description": "", "class_code": "A", "cancelled": False}],
            "05/05/2026", "05/05/2026", run_id=None, record_changes=False))
        t.start()
        t.join()
        check("a write from another thread is seen",
              store.stats()["total_events"], first["total_events"] + 1)
        with sqlite3.connect(store.DB_PATH) as other:
            other.execute("DELETE FROM events WHERE room = 'STATS2'")
        other.close()
    finally:
        store._compute_stats = real_compute


def test_presets_and_groups() -> None:
    print("\npresets and groups")
    import threading

    store.set_kv(store.PRESETS_KEY, [])
    store.save_preset("Lab", {"rooms": "142"})
    names = [p["name"] for p in store.save_preset("lab", {"rooms": "147"})]
    check("a preset name differing only in case replaces the old one",
          names, ["lab"])
    check("...with the new filters",
          store.load_presets()[0]["filters"], {"rooms": "147"})

    # Concurrent saves: each one is a read-modify-write of the whole list, so
    # without the lock two racing saves each read the old list and the later
    # write drops the other's preset.
    store.set_kv(store.PRESETS_KEY, [])
    barrier = threading.Barrier(8)

    def save(i: int) -> None:
        barrier.wait()
        store.save_preset(f"Race {i}", {"seats": i})

    threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("eight concurrent preset saves all land",
          sorted(p["name"] for p in store.load_presets()),
          [f"Race {i}" for i in range(8)])
    store.set_kv(store.PRESETS_KEY, [])

    # Groups: concurrent saves must not collide on one temp file (which
    # raised, or renamed another save's half-written file into place).
    errors: list[BaseException] = []
    gbarrier = threading.Barrier(8)

    def save_groups(i: int) -> None:
        gbarrier.wait()
        try:
            store.save_groups({f"G{i}": [str(100 + i)]})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=save_groups, args=(i,))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("concurrent group saves do not fail", errors, [])
    loaded = store.load_groups()
    ok("...and the file holds one whole save",
       len(loaded) == 1 and next(iter(loaded)).startswith("G"))
    check("no temp files are left behind",
          [p.name for p in store.GROUPS_PATH.parent.iterdir()
           if p.name.endswith(".tmp")], [])
    store.GROUPS_PATH.unlink()

def test_runs_and_retention() -> None:
    print("\nruns and retention")
    rid = store.start_run("backfill")
    # With the reach it really reaches, because test_api asserts against
    # backfill_done() and the gate now reads date_from.
    store.finish_run(rid, "ok", events_count=99, date_from=_full_reach())
    # A backfill is a run but not a scrape: if it counted as the last run, the
    # startup scrape would think it had just run and skip itself.
    check("a backfill is not the last scrape", store.last_run(), None)
    check("last_backfill finds it",
          (store.last_backfill() or {}).get("events_count"), 99)

    sid = store.start_run("manual")
    store.finish_run(sid, "ok", events_count=7)
    check("the scrape is the last run",
          (store.last_run() or {}).get("events_count"), 7)

    # The margin that stops a backfill deleting the first month it fetched:
    # the oldest row a BACKFILL_MONTHS backfill can write is 396 days old.
    check("retention covers the backfill",
          BACKFILL_MONTHS * 31 + 31 < KEEP_DAYS, True)


def test_prune() -> None:
    print("\nprune")
    # 396 days is the oldest a 12-month backfill can write, so it must survive.
    store.replace_events([ev("Theta Old", "1065", -396),
                          ev("Theta Ancient", "1065", -(KEEP_DAYS + 5))],
                         *win(-500, -300), run_id=50)
    check("both stored", len(store.get_events(room="1065")), 2)

    counts = store.prune()
    check("prune reports both counts", sorted(counts), ["changes", "events"])
    check("the ancient booking is gone",
          [e["title"] for e in store.get_events(room="1065")], ["Theta Old"])

    # The feed ages out on the booking's own date, so it keeps the change
    # about the booking we still hold and drops the one we do not.
    titles = [r["title"] for r in store.get_changes(limit=2000)]
    check("the feed keeps the change about the surviving booking",
          "Theta Old" in titles, True)
    check("the feed drops the change about the pruned booking",
          "Theta Ancient" in titles, False)

    # The horizon is a local date on both sides now. The fixture's days come
    # from date.today(), but the cutoff used to come from SQLite's date('now')
    # -- UTC, so for part of every Toronto evening the two clocks disagreed
    # and the fate of the boundary row depended on the hour the suite ran.
    # Both edges are pinned: the row exactly on the horizon stays (the
    # cutoff is exclusive), the row one day past it goes.
    store.replace_events([ev("Theta Boundary", "1065", -KEEP_DAYS),
                          ev("Theta Over", "1065", -(KEEP_DAYS + 1))],
                         *win(-(KEEP_DAYS + 2), -(KEEP_DAYS - 1)),
                         run_id=51, record_changes=False)
    store.prune()
    titles = [e["title"] for e in store.get_events(room="1065")]
    check("a booking exactly on the horizon survives -- the cutoff is exclusive",
          "Theta Boundary" in titles, True)
    check("a booking one day past the horizon goes",
          "Theta Over" not in titles, True)


def test_backfill_windows() -> None:
    print("\nbackfill windows")
    for months in (6, 12):
        ws = backfill_windows(months, 1)
        check(f"{months} months yields {months - 1} windows", len(ws), months - 1)
        check("oldest first", ws[0][0].startswith("01/"), True)
        check("every window ends on the last of a month",
              all(w[1].split("/")[0] in ("28", "29", "30", "31") for w in ws), True)
        check("windows are contiguous months",
              _contiguous(ws), True)
        # The daily scrape owns the month after the last one, so the backfill
        # must not reach into it.
        last_end = ws[-1][1]
        check("stops short of the daily window",
          last_end != date.today().strftime("%d/%m/%Y"), True)


def _contiguous(ws: list[tuple[str, str]]) -> bool:
    """Each window should start the day after the previous one ended."""
    for (_, prev_end), (next_start, _) in zip(ws, ws[1:]):
        e = datetime.strptime(prev_end, "%d/%m/%Y").date()
        s = datetime.strptime(next_start, "%d/%m/%Y").date()
        if (s - e).days != 1:
            return False
    return True


def test_api() -> None:
    print("\napi")
    store.init_db()          # a second boot must not disturb the feed
    orch = StubOrch()
    client = create_app(orch).test_client()

    r = client.get("/api/changes")
    check("changes status", r.status_code, 200)
    body = r.get_json()
    check("the feed lists rows", body["count"] > 0, True)
    check("the counts add up", body["added"] + body["removed"], body["count"])

    body = client.get("/api/changes?kind=removed").get_json()
    check("kind filter narrows",
          {c["kind"] for c in body["changes"]}, {"removed"})

    body = client.get("/api/changes?kind=added&limit=1").get_json()
    check("limit is honoured", body["count"], 1)
    check("truncation is reported", body["truncated"], True)

    body = client.get("/api/changes?kind=nonsense").get_json()
    check("a junk kind widens rather than empties", body["count"] > 0, True)

    # The history fill is the app's own first-run job, so it has no route and
    # no button — nothing should be able to ask for it a second time.
    check("no backfill trigger route", client.post("/api/backfill").status_code, 404)
    check("no backfill state route", client.get("/api/backfill").status_code, 404)

    body = client.get("/api/status").get_json()
    check("status reports the last backfill", body["last_backfill"]["events"], 99)
    check("status keeps the scrape separate", body["last_scrape_status"], "ok")
    check("status says the fill has landed", body["backfill_done"], True)

    # Rows come back ORDER BY start_iso ascending, so a limit below the row
    # count would keep the oldest bookings and drop the upcoming ones.
    check("no read truncation",
          len(store.get_events()), store.stats()["total_events"])

    # A run that finished clean but stepped over an empty month carries that
    # as a parenthetical. The sidebar shows the note without reprinting the
    # booking count it has already given, so it has to come out of the string —
    # the run row has no column for it.
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=23000, date_from=_full_reach(),
                     message="23000 bookings over 11 months (2 month(s) empty)")
    bf = client.get("/api/status").get_json()["last_backfill"]
    check("the note is split out for the sidebar",
          bf["note"], "2 month(s) empty")
    check("the run still reports its bookings", bf["events"], 23000)

    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=24016, date_from=_full_reach(),
                     message="24016 bookings over 11 months")
    bf = client.get("/api/status").get_json()["last_backfill"]
    check("a clean run has no note", bf["note"], "")


def _clear_backfills() -> None:
    """Drop every backfill run so the gate can be exercised from scratch."""
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("DELETE FROM scrape_runs WHERE trigger = 'backfill'")


def _reach_for(months: int) -> str:
    """The oldest window a `months`-month backfill covers, dd/mm/yyyy.

    Taken from the window builder rather than recomputed, so the gate is
    checked against what a backfill actually asks for — including the short
    reaches, which is where the gate can go wrong.
    """
    return backfill_windows(months, SCRAPE_MONTHS_BACK)[0][0]


def _full_reach() -> str:
    """The oldest window a full backfill covers, dd/mm/yyyy."""
    return _reach_for(BACKFILL_MONTHS)


def _full_reach_at(days_ago: int) -> str:
    """The reach a full backfill covered if it ran `days_ago` days back.

    Mirrors store._first_of_month_months_ago on purpose: backfill_windows()
    always works from today, so a run dated in the past cannot use it.
    """
    when = date.today() - timedelta(days=days_ago)
    m = when.month - 1 - BACKFILL_MONTHS
    return date(when.year + m // 12, m % 12 + 1, 1).strftime("%d/%m/%Y")


def test_backfill_gate() -> None:
    print("\nthe one-time history fill is owed until a clean run lands")
    _clear_backfills()
    check("no backfill at all -> still owed", store.backfill_done(), False)

    # A run that skipped a month finishes 'error'. That is what keeps the fill
    # resumable instead of writing off the months it never fetched.
    rid = store.start_run("backfill")
    store.finish_run(rid, "error", events_count=1200, date_from=_full_reach(),
                     message="1200 bookings over 11 months; 3 month(s) skipped")
    check("a partial run leaves it owed", store.backfill_done(), False)

    rid = store.start_run("backfill")
    store.finish_run(rid, "auth_required", events_count=400,
                     date_from=_full_reach(),
                     message="Session expired after 4/11 months")
    check("an interrupted run leaves it owed", store.backfill_done(), False)

    # A run that reached the floor and stored thousands of bookings, but read
    # them off the rendered page because the report would not download. It is
    # deliberately NOT filed as `error`: nothing failed and nothing was
    # destroyed — `replace_events` will not delete for a report it cannot
    # establish as whole. What it is not is the history: each month holds its
    # first page, and those months look exactly like quiet ones, so nothing
    # else in the app can say so. Only the status distinguishes it, which is
    # why the status has to be its own word rather than `ok`.
    rid = store.start_run("backfill")
    store.finish_run(rid, "partial", events_count=2400, date_from=_full_reach(),
                     message="2400 bookings over 11 months "
                             "(11 month(s) only partly read)")
    check("a fill read off the page leaves it owed",
          store.backfill_done(), False)

    # A reach with no windows finishes 'ok' having fetched nothing. Counting
    # that would retire the fill for good without a single booking stored.
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=0, date_from=_full_reach())
    check("an empty run does not settle it", store.backfill_done(), False)

    # The reach check. `--backfill --months 6` is documented as a repair
    # command, and its run is genuinely clean — it just never went back past
    # six months. Without this the fill retires and the six oldest months are
    # never fetched, by the app or by anything else.
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=9000, date_from=_reach_for(6),
                     message="9000 bookings over 5 months")
    check("a six-month repair run does not settle it", store.backfill_done(), False)

    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=24016, date_from=_full_reach(),
                     message="24016 bookings over 11 months")
    check("a full-reach run settles it", store.backfill_done(), True)

    # A later short run must not *un*-settle it. This is why the floor is
    # measured against each run's own started_at instead of against today: a
    # today-relative floor drifts forward, so this good fill would expire on
    # its own and refetch eleven months to no purpose.
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=9000, date_from=_reach_for(6))
    check("a later short run does not unsettle it", store.backfill_done(), True)

    # ...and the no-drift property itself, which the case above cannot show:
    # it is the *old* run that has to keep qualifying as time passes.
    _clear_backfills()
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=24016,
                     date_from=_full_reach_at(200))
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("UPDATE scrape_runs SET started_at = ? WHERE id = ?",
                     ((date.today() - timedelta(days=200)).isoformat(), rid))
    check("a fill that reached the floor keeps reaching it 200 days on",
          store.backfill_done(), True)

    _clear_backfills()
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=24016, date_from=_full_reach())
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("UPDATE scrape_runs SET started_at = ? WHERE id = ?",
                     ((date.today() - timedelta(days=200)).isoformat(), rid))
    check("a reach from today does not reach the floor 200 days ago",
          store.backfill_done(), False)

    # A scrape is a different job and must never settle the fill on its own.
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=24016, date_from=_full_reach())
    sid = store.start_run("schedule")
    store.finish_run(sid, "ok", events_count=6520)
    check("a later scrape does not unsettle a good fill",
          store.backfill_done(), True)


def test_auto_queue() -> None:
    print("\nthe fill is queued by the app, and only while it is owed")
    from app.scheduler import CMD_BACKFILL, Orchestrator  # noqa: PLC0415

    orch = Orchestrator()
    _clear_backfills()
    orch._queue_backfill_if_owed()
    queued = []
    while not orch._q.empty():
        queued.append(orch._q.get_nowait())
    check("owed -> queued once", [c for c, _ in queued], [CMD_BACKFILL])
    check("...and marked as the app's own", queued[0][1].get("auto"), True)

    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=24016, date_from=_full_reach())
    orch._queue_backfill_if_owed()
    check("settled -> nothing queued", orch._q.empty(), True)

    # The other half of the same rule, and the one the review found: a clean
    # six-month repair run must not read as a finished fill, or the app stops
    # asking for the months it still does not have.
    _clear_backfills()
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=9000, date_from=_reach_for(6))
    orch._queue_backfill_if_owed()
    queued = []
    while not orch._q.empty():
        queued.append(orch._q.get_nowait())
    check("a short fill is still owed -> queued again",
          [c for c, _ in queued], [CMD_BACKFILL])


def test_backfill_progress_label() -> None:
    print("\na backfill's progress names the month, not just the window")
    from types import SimpleNamespace

    import app.scheduler as sch  # noqa: PLC0415
    from app.scheduler import Orchestrator  # noqa: PLC0415

    windows = backfill_windows(2, 1)
    seen: list[dict] = []

    class FakeResult:
        status = "ok"
        complete = True
        events: list = []
        rooms: list = []
        date_from, date_to = windows[0]

    real_scrape, real_probe = sch.scrape, sch.session.probe
    # No browser and no LSM — the question is only which message the sidebar
    # ends up showing, and the live path costs eleven report runs to ask.
    sch.scrape = lambda **kw: (seen.append(kw), FakeResult())[1]
    sch.session.probe = lambda **kw: SimpleNamespace(
        ok=True, state="ok", message="", session_id=None)
    try:
        orch = Orchestrator()
        # Watch the progress as it is set: _do_backfill clears it in its
        # finally, so the value after the call says nothing about the run.
        shown: list[str] = []
        real_set = orch._set
        orch._set = lambda **kw: (shown.append(kw["progress"]) if "progress" in kw
                                  else None, real_set(**kw))[1]
        orch._do_backfill(months=2)
    finally:
        sch.scrape, sch.session.probe = real_scrape, real_probe

    # scrape() reports "Scraping 01/02/2026 → 28/02/2026". Handing that to the
    # sidebar rewrites the month counter into the same dates minus the position
    # in the run, so the label the backfill set must be left standing.
    label = f"Backfill 1/{len(windows)}: {windows[0][0]} → {windows[0][1]}"
    check("scrape is not given a status callback", "on_status" in seen[0], False)
    check("the month counter is what the sidebar is shown", label in shown, True)
    check("no scrape message leaks into the sidebar",
          [p for p in shown if p.startswith("Scraping")], [])
    check("the month label is the last thing shown",
          [p for p in shown if p][-1], label)
    check("progress is cleared when the run ends",
          orch.status()["progress"], "")


def test_backfill_with_no_windows() -> None:
    print("\na reach with no windows fails loudly instead of crashing")
    from app.main import run_backfill  # noqa: PLC0415
    from app.scheduler import Orchestrator  # noqa: PLC0415

    # months=1 yields no windows at all: backfill_windows() is
    # range(months, 1, -1). This used to index windows[0] at the finish_run
    # call and raise IndexError, which the except then swallowed into a run
    # that reported success — on the one command a person runs when the fill
    # has already gone wrong. The early return sits before session.probe, so
    # this needs no browser and no network.
    orch = Orchestrator()
    orch._do_backfill(months=1)
    bf = orch.status()["backfill"]
    check("the run is counted as a failure", bf["failed"], 1)
    check("it stored nothing", bf["stored"], 0)
    check("it says what happened", bool(bf["message"]), True)

    row = store.last_backfill() or {}
    check("the run row is an error", row.get("status"), "error")
    check("the run row names the reach asked for",
          "months=1" in (row.get("message") or ""), True)

    check("the CLI exits non-zero", run_backfill(1) != 0, True)


def test_changes_limit_is_clamped() -> None:
    print("\nthe change feed's limit is clamped at both ends")
    store.init_db()
    client = create_app(StubOrch()).test_client()

    # More rows than the cap, so the clamp is observable at all. Without it a
    # negative limit reached SQLite as `LIMIT -1`, which it reads as
    # *unlimited* — so the endpoint returned the whole table while `truncated`
    # compared the row count against -1 and reported false.
    today_iso = date.today().isoformat()
    now = datetime.now().isoformat()
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO changes (run_id, trigger, kind, uid, title, room, "
            "  start_iso, end_iso, date, description, class_code, all_day, "
            "  detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(None, "manual", "added", f"burst{i}", f"Burst {i}", "1099",
              None, None, today_iso, "", "", 0, now) for i in range(2100)],
        )
    check("the fixture really did exceed the cap",
          len(store.get_changes(limit=5000)) > 2000, True)

    body = client.get("/api/changes?limit=-1").get_json()
    check("a negative limit falls back to the default, not the whole table",
          body["count"], 200)
    check("...and reports the truncation", body["truncated"], True)
    check("an enormous limit is capped too",
          client.get("/api/changes?limit=999999").get_json()["count"], 2000)
    check("an ordinary limit is left alone",
          client.get("/api/changes?limit=5").get_json()["count"], 5)


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — retention and change feed")
    print("=" * 60)

    store.init_db()
    test_change_detection()
    test_all_day_changes_keep_the_flag()
    test_narrow_rescrape_adds_nothing()
    test_edited_booking_is_a_remove_and_an_add()
    test_backfill_logs_no_changes()
    test_partial_report_adds_but_never_deletes()
    test_empty_report_does_not_wipe()
    test_explicit_cancellation_is_removed()
    test_window_moving_forward_is_not_a_flood()
    test_stats_are_recomputed_only_when_the_events_change()
    test_presets_and_groups()
    test_runs_and_retention()
    test_prune()
    test_backfill_windows()
    test_api()
    # Last: these two rewrite scrape_runs, and test_api reads it.
    test_backfill_gate()
    test_auto_queue()
    test_backfill_progress_label()
    test_backfill_with_no_windows()
    # Last: it inserts a burst of rows, and test_api counts them.
    test_changes_limit_is_clamped()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
