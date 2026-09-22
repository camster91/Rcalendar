"""
Background-worker tests: the report window guard, the retry-storm guard, and
the worker's survival of a failing command.

    python tests/test_worker.py

All of these exist because the alternative to checking is silent. A report
that rendered a *narrower* window than we asked for is not merely missing
data — the window's bounds are the bounds of the reconcile delete, so it
erases real bookings and reports success. A daily scrape that is marked done
only on success re-launches a browser against LSM every tick while the
session is dead. And a worker thread that dies on an exception takes the
app's only scraper with it, without restarting anything and without saying
so. None of the three produces an error anyone would see, which is what makes
them worth a test rather than a comment.

Nothing here opens a browser or touches LSM. The page is faked, the worker's
scrape is stubbed, and app.config is pointed at a scratch data directory
before it is imported.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Before app.config is imported: it opens the log file on import, and a test
# run should not write into the real data/app.log.
os.environ["LSM_DATA_DIR"] = tempfile.mkdtemp(prefix="lsm-worker-")

from app import scheduler, scrape  # noqa: E402
from app.config import log  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")


def ok(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


# ── The report window ────────────────────────────────────────────────────

APEX_URL = "https://lsm.utoronto.ca/ords/f?p=143:51:17828197829093:::::"


class PastTheDates(Exception):
    """Raised when a scrape gets past the date check.

    Used as a sentinel rather than making the fake page complete a whole
    scrape: the question a tolerant readback has to answer is only whether the
    date gate opened, and a fake that pretended to run the report would be
    asserting far more than it knows.
    """


class FakePage:
    """Enough of a Playwright page for scrape() to reach the date check."""

    def __init__(self, readback: list[str]) -> None:
        self._readback = list(readback)
        self._date_calls = 0

    @property
    def url(self) -> str:
        return APEX_URL

    def set_default_timeout(self, ms: int) -> None:
        pass

    def on(self, event: str, handler) -> None:
        pass

    def goto(self, url: str, **kwargs) -> None:
        pass

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def wait_for_function(self, script: str, **kwargs) -> None:
        """_wait_for_item; returning normally means the item exists."""

    def evaluate(self, script: str, arg=None):
        # _set_dates makes exactly two calls: the set, which passes the two
        # dates as a list, and the readback, which passes nothing. Anything
        # after them means the date guard opened.
        if isinstance(arg, list) and len(arg) == 2:
            self._date_calls += 1
            return None
        if self._date_calls == 1:
            self._date_calls += 1
            return list(self._readback)
        raise PastTheDates("reached the report")


class FakeCtx:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]

    def __enter__(self) -> "FakeCtx":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def with_fake_browser(page: FakePage, fn):
    """Run fn() with session.browser replaced, restoring it afterwards."""
    original = scrape.session.browser
    scrape.session.browser = lambda **kwargs: FakeCtx(page)
    try:
        return fn()
    finally:
        scrape.session.browser = original


def test_window_guard() -> None:
    print("\nreport window")

    asked = ("01/03/2026", "31/03/2026")

    # A date the report hands back in a different mask is the same date. The
    # readback is the item's display string and this code cannot know its
    # format, so an unfamiliar one must not be read as a disagreement.
    for readback in (["01/03/2026", "31/03/2026"],
                     ["01-Mar-2026", "31-Mar-2026"],
                     ["01-03-26", "31-03-26"],
                     ["2026-03-01", "2026-03-31"]):
        check(f"the same window reads back as the same ({readback[0]!r})",
              scrape._set_dates(FakePage(readback), *asked), None)

    # A date that cannot be read at all is no opinion. Failing here would stop
    # every scrape the day the report changes its format, which is worse than
    # the mismatch this is trying to catch.
    for readback in (["", "31/03/2026"], ["garbage", "31/03/2026"],
                     [None, "31/03/2026"]):
        check(f"an unreadable date abstains ({readback[0]!r})",
              scrape._set_dates(FakePage(readback), *asked), None)

    # A confidently different window is fatal. The bounds of the window are
    # the bounds of the reconcile delete, so running this would erase bookings.
    r = scrape._set_dates(FakePage(["01/03/2026", "07/03/2026"]), *asked)
    ok("a narrower window is refused", r is not None)
    ok("...and the message names both dates",
       r is not None and "2026-03-31" in r and "2026-03-07" in r)
    ok("...and says what would have happened",
       r is not None and "delete" in r)

    r = scrape._set_dates(FakePage(["01/02/2026", "31/03/2026"]), *asked)
    ok("a window wrong at the other end is refused too", r is not None)

    # The guard is wired into scrape(), not merely available to it: a mismatch
    # has to stop the run rather than be logged and stepped over.
    result = with_fake_browser(
        FakePage(["01/03/2026", "07/03/2026"]),
        lambda: scrape.scrape(date_from=asked[0], date_to=asked[1]),
    )
    check("scrape refuses a mismatched window", result.status, "error")
    check("...and reports nothing", result.events, [])
    ok("...and says why", "mismatch" in (result.message or ""))

    # And the gate does open when the window agrees, so the refusal above is
    # the check working rather than the scrape never starting.
    result = with_fake_browser(
        FakePage(["01-Mar-2026", "31-Mar-2026"]),
        lambda: scrape.scrape(date_from=asked[0], date_to=asked[1]),
    )
    ok("a matching window gets past the check",
       "reached the report" in (result.message or ""))

    check("the readback parser has no opinion about nothing",
          scrape._item_date(None), None)
    check("...and reads a plain date object",
          scrape._item_date(date(2026, 3, 1)), date(2026, 3, 1))


# ── The daily scrape's retry guard ───────────────────────────────────────

class FrozenDatetime(datetime):
    """A datetime whose now() is whatever the test says.

    A subclass, so an isinstance check anywhere still passes; the point is
    only that _tick and _due_for_daily cannot see the wall clock, because a
    suite that only passes after 06:00 is worse than no suite.
    """

    fixed = datetime(2026, 9, 21, 9, 0, 0)

    @classmethod
    def now(cls, tz=None) -> datetime:  # type: ignore[override]
        return cls.fixed


def test_no_retry_storm() -> None:
    print("\ndaily scrape")

    def due_at(when: str) -> bool:
        return scheduler.Orchestrator()._due_for_daily(
            datetime.fromisoformat(f"2026-09-21T{when}:00")
        )

    # SCRAPE_TIME is 06:00. Before it, nothing is owed.
    check("before the scrape time nothing is due", due_at("05:59"), False)
    check("at the scrape time it is due", due_at("06:00"), True)
    check("and after it, still due", due_at("11:00"), True)

    orch = scheduler.Orchestrator()
    check("a fresh worker owes a scrape", orch._due_for_daily(
        datetime.fromisoformat("2026-09-21T07:00:00")), True)
    orch._last_daily = date(2026, 9, 21)
    check("...and owes nothing once the day is marked",
          orch._due_for_daily(datetime.fromisoformat("2026-09-21T07:00:00")),
          False)
    orch._last_daily = date(2026, 9, 20)
    check("yesterday's mark does not cover today",
          orch._due_for_daily(datetime.fromisoformat("2026-09-21T07:00:00")),
          True)

    # The guard itself, driven through _tick. _last_daily is set *before* the
    # scrape, so a scrape that fails — a dead session, a bad render — does not
    # re-arm. Marking it only on success is the retry storm: every tick would
    # launch another browser against LSM, twenty seconds apart, forever.
    real_datetime = scheduler.datetime
    scheduler.datetime = FrozenDatetime
    try:
        orch = scheduler.Orchestrator()
        attempts: list[str] = []

        def failing_scrape(trigger: str = "manual", interactive: bool = False):
            attempts.append(trigger)
            # Deliberately does NOT set _last_daily, standing in for the
            # failure path at _do_scrape's tail.
            return scrape.ScrapeResult("auth_required", message="Session expired")

        orch._do_scrape = failing_scrape          # type: ignore[method-assign]
        orch._do_heartbeat = lambda: None         # type: ignore[method-assign]

        for _ in range(20):
            orch._tick()

        check("twenty ticks with a dead session scrape once",
              len(attempts), 1)
        check("...and the one attempt was the scheduled one", attempts, ["schedule"])

        # A manual retry still works, and it is the intended way to retry: the
        # guard suppresses the *automatic* storm, not the user's own button.
        orch._do_scrape(trigger="manual")
        check("a manual scrape still runs", len(attempts), 2)

        check("the day is marked attempted", orch._last_daily, date(2026, 9, 21))
    finally:
        scheduler.datetime = real_datetime

    # The heartbeat is a no-op until the first one has happened, so a fresh
    # worker cannot heartbeat on its way to its first scrape.
    orch = scheduler.Orchestrator()
    check("no heartbeat before the first one",
          orch._due_for_heartbeat(datetime(2026, 9, 21, 23, 0)), False)
    orch._last_heartbeat = datetime(2026, 9, 21, 6, 0)
    check("no heartbeat while the session is warm",
          orch._due_for_heartbeat(datetime(2026, 9, 21, 9, 0)), False)
    check("a heartbeat once the idle window is reached",
          orch._due_for_heartbeat(
              datetime(2026, 9, 21, 6, 0) + timedelta(hours=4)), True)


def test_logout_reports_what_the_probe_found() -> None:
    """The logout command has to reach the session layer, and be reported honestly.

    _do_logout is the only thing that decides between "Signed out" and "Still
    signed in", and getting it backwards is invisible: the sidebar would read
    "Session expired" over a session that still works, and the next scrape
    would run happily while the UI said it could not. So both directions are
    asserted, against a stubbed session.logout — nothing here opens a browser.
    """
    print("\nlogout command")

    real = scheduler.session.logout
    orch = scheduler.Orchestrator()
    try:
        # The request the API makes has to be the command the loop dispatches,
        # or the route enqueues something nothing answers.
        orch.request_logout()
        check("the request carries the command the loop dispatches",
              orch._q.get_nowait(), (scheduler.CMD_LOGOUT, {}))

        def logout_returning(state: str, message: str = ""):
            return lambda: scheduler.session.SessionState(state, message=message)

        scheduler.session.logout = logout_returning("expired", "wants a login")
        orch._do_logout()
        st = orch.status()
        check("a cleared session is reported as the probe found it",
              st["session"], "expired")
        check("...and the worker is not left busy", st["busy"], False)

        # The direction that matters: the cookies survived, so the session is
        # still there. Claiming a sign-out here is the defect.
        scheduler.session.logout = logout_returning("ok")
        orch._do_logout()
        st = orch.status()
        check("a logout that did not take is reported as still live",
              st["session"], "ok")
        ok("...and says so, rather than leaving the probe's blank message",
           "still signed in" in st["session_message"].lower())
        check("...and the worker is not left busy either", st["busy"], False)
    finally:
        scheduler.session.logout = real


def test_a_failed_command_does_not_kill_the_worker() -> None:
    """The worker thread is the app's only scraper, and nothing restarts it.

    _run had no guard around its dispatch, and only _do_scrape and _do_backfill
    catch anything: _bootstrap_session, _do_probe, _do_heartbeat and _tick are
    try/finally with no except, so an exception out of any of them -- a
    Playwright launch failure inside the probe, an OSError out of the
    heartbeat, a raising store read -- ended _run for the life of the process.
    Nothing would ever be scraped again, nothing would say so, and the window
    would go on displaying the status it held when the thread died.

    Driven through the real loop rather than by calling the guard, because the
    guard's value is that it survives a real dispatch. The failing command is
    one whose handler raises, and the proof it did not end the loop is that the
    command queued behind it is still handled. The bootstrap is made to raise
    too, so the failure that skips the loop entirely is covered by the same
    test -- that is the one that would leave the thread dead before it ever
    polled for work.
    """
    print("\nworker loop")

    orch = scheduler.Orchestrator()
    seen: list[str] = []
    real = {
        "bootstrap": orch._bootstrap_session,
        "probe": orch._do_probe,
        "login": orch._do_login,
        "shutdown": scheduler.session.shutdown,
    }
    try:
        def exploding_bootstrap() -> None:
            raise RuntimeError("no browser for you")

        def exploding_probe() -> None:
            raise RuntimeError("probe blew up")

        def recording_login() -> None:
            seen.append("login")

        orch._bootstrap_session = exploding_bootstrap
        orch._do_probe = exploding_probe
        orch._do_login = recording_login
        # Stopping must not touch a browser here either, and the STOP path has
        # its own guard to exercise.
        scheduler.session.shutdown = lambda: None

        orch.start()

        # Both are queued before either runs, so the login landing in `seen` is
        # proof the loop carried on after the probe raised -- not merely that
        # the thread had not been joined yet.
        orch.request_probe()
        orch.request_login()

        deadline = time.monotonic() + 10
        while "login" not in seen and time.monotonic() < deadline:
            time.sleep(0.02)

        check("a command that raises does not stop the next one being handled",
              seen, ["login"])
        ok("the worker thread is still alive after both failures",
           orch._thread is not None and orch._thread.is_alive())
        check("...and the failed command did not leave the UI claiming to be busy",
              orch.status()["busy"], False)
    finally:
        orch._bootstrap_session = real["bootstrap"]
        orch._do_probe = real["probe"]
        orch._do_login = real["login"]
        scheduler.session.shutdown = real["shutdown"]
        orch.stop()


def test_parse_time() -> None:
    """A SCRAPE_TIME nobody can read must not mean "scrape constantly"."""
    print("\nscrape time")

    from datetime import time as dtime

    check("a normal time", scheduler._parse_time("06:00"), dtime(6, 0))
    check("a time past noon", scheduler._parse_time("23:30"), dtime(23, 30))
    check("midnight", scheduler._parse_time("00:00"), dtime(0, 0))

    # The fallback is 06:00 rather than midnight on purpose: a typo in the
    # config should behave like the default it replaced, not like "the scrape
    # time has always passed", which would make the daily run fire at once.
    check("nonsense falls back to the default", scheduler._parse_time("oops"),
          dtime(6, 0))
    check("so does an empty string", scheduler._parse_time(""), dtime(6, 0))
    check("so does an hour out of range", scheduler._parse_time("25:00"),
          dtime(6, 0))
    check("so does a missing value", scheduler._parse_time(None), dtime(6, 0))


def test_exclusions() -> None:
    """An excluded room has to be excluded whatever the shuttle calls it.

    The set is compared against two differently-spelled things — the shuttle's
    own room names, which the patterns are matched against, and the events'
    `room`, which the parser has already normalised. Matching only the raw
    spelling made the answer depend on how the report happened to write the
    room, so '134A' was dropped and 'RT 134A' was stored.

    Which spelling the live shuttle uses is not knowable from here, and that
    is the point: the code does not get to choose, so both have to work.
    """
    print("\nroom exclusions")

    from app.parse import normalise_room
    from app.scheduler import _excluded_rooms

    # The same room, written every way the report might write it.
    for raw in ("134A", "RT 134A", "RT-134A", "ROTMAN 134A", "rotman-134a"):
        ex = _excluded_rooms([raw])
        ok(f"{raw!r} is excluded", raw in ex)
        check(f"...so the event's room {normalise_room(raw)!r} is too",
              normalise_room(raw) in ex, True)

    # And nothing else is swept up with it: the pattern is one room series,
    # not "starts with 134" and not "four characters".
    for raw in ("135A", "142", "134", "134AB", "1340", "Event North", ""):
        ex = _excluded_rooms([raw])
        check(f"{raw!r} is left alone", normalise_room(raw) in ex, False)

    # A room not mentioned in this report is not invented into the set — the
    # set is built from what was seen, which is why `seen` is an argument.
    check("no seen rooms, nothing patterned",
          _excluded_rooms([]), set())

    # A hand-written literal is honoured in either spelling, since whoever
    # wrote it may have copied the name out of the report.
    import app.config as config
    was = config.EXCLUDED_ROOMS
    try:
        config.EXCLUDED_ROOMS = {"RT 900"}
        ex = _excluded_rooms([])
        ok("a literal entry is excluded as written", "RT 900" in ex)
        ok("...and as the events spell it", "900" in ex)
    finally:
        config.EXCLUDED_ROOMS = was


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — worker tests")
    print(f"  data dir: {os.environ['LSM_DATA_DIR']}")
    print("=" * 60)

    test_window_guard()
    test_no_retry_storm()
    test_logout_reports_what_the_probe_found()
    test_a_failed_command_does_not_kill_the_worker()
    test_exclusions()
    test_parse_time()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    log.setLevel("CRITICAL")     # the abstain path logs a warning per case
    sys.exit(main())
