"""
Retention, backfill and the change feed.

Covers the three claims the feature rests on: that re-scraping identical data
is silent (so the feed means something), that a booking which vanishes leaves a
readable trace, and that a year of history survives the prune that follows it.

    python tests/test_changes.py
"""

from __future__ import annotations

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
from app.config import BACKFILL_MONTHS, KEEP_DAYS  # noqa: E402
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


def test_runs_and_retention() -> None:
    print("\nruns and retention")
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=99)
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


def _clear_backfills() -> None:
    """Drop every backfill run so the gate can be exercised from scratch."""
    import sqlite3

    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("DELETE FROM scrape_runs WHERE trigger = 'backfill'")


def test_backfill_gate() -> None:
    print("\nthe one-time history fill is owed until a clean run lands")
    _clear_backfills()
    check("no backfill at all -> still owed", store.backfill_done(), False)

    # A run that skipped a month finishes 'error'. That is what keeps the fill
    # resumable instead of writing off the months it never fetched.
    rid = store.start_run("backfill")
    store.finish_run(rid, "error", events_count=1200,
                     message="1200 bookings over 11 months; 3 month(s) skipped")
    check("a partial run leaves it owed", store.backfill_done(), False)

    rid = store.start_run("backfill")
    store.finish_run(rid, "auth_required", events_count=400,
                     message="Session expired after 4/11 months")
    check("an interrupted run leaves it owed", store.backfill_done(), False)

    # A reach with no windows finishes 'ok' having fetched nothing. Counting
    # that would retire the fill for good without a single booking stored.
    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=0)
    check("an empty run does not settle it", store.backfill_done(), False)

    rid = store.start_run("backfill")
    store.finish_run(rid, "ok", events_count=24016,
                     message="24016 bookings over 11 months")
    check("a clean run settles it", store.backfill_done(), True)

    sid = store.start_run("schedule")
    store.finish_run(sid, "ok", events_count=6520)
    check("a later scrape does not unsettle it", store.backfill_done(), True)


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
    store.finish_run(rid, "ok", events_count=24016)
    orch._queue_backfill_if_owed()
    check("settled -> nothing queued", orch._q.empty(), True)


def test_backfill_progress_label() -> None:
    print("\na backfill's progress names the month, not just the window")
    from types import SimpleNamespace

    import app.scheduler as sch  # noqa: PLC0415
    from app.scheduler import Orchestrator  # noqa: PLC0415

    windows = backfill_windows(2, 1)
    seen: list[dict] = []

    class FakeResult:
        status = "ok"
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


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — retention and change feed")
    print("=" * 60)

    store.init_db()
    test_change_detection()
    test_narrow_rescrape_adds_nothing()
    test_edited_booking_is_a_remove_and_an_add()
    test_backfill_logs_no_changes()
    test_empty_report_does_not_wipe()
    test_runs_and_retention()
    test_prune()
    test_backfill_windows()
    test_api()
    # Last: these two rewrite scrape_runs, and test_api reads it.
    test_backfill_gate()
    test_auto_queue()
    test_backfill_progress_label()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
