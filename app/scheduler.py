"""
Background orchestrator — daily scrape, session heartbeat, manual commands.

Why a single worker thread
--------------------------
Playwright's sync API binds its driver to the thread that created it, and
Flask serves each request on its own thread. So nothing here is called
directly from a request handler: Flask, the tray menu and the clock all
enqueue a command, and this one thread is the only thing that ever talks
to Chromium. That also serialises access to the Chromium profile, which
only allows one process at a time.

Responsibilities
----------------
* Daily scrape at SCRAPE_TIME, plus a catch-up run if the app was closed
  over the scheduled time.
* Heartbeat every HEARTBEAT_HOURS to keep the Shibboleth session warm.
* Serve manual "Scrape now" / "Sign in" / "Check session" requests.
"""

from __future__ import annotations

import queue
import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Iterable

from app import session, store
from app.config import (
    BACKFILL_MONTHS, HEARTBEAT_HOURS, SCRAPE_MONTHS_AHEAD, SCRAPE_MONTHS_BACK,
    SCRAPE_ON_START, SCRAPE_TIME, log,
)
from app.rooms import describe
from app.scrape import (
    ScrapeResult, backfill_windows, default_window, scrape,
)

# Commands the worker understands.
CMD_SCRAPE = "scrape"
CMD_BACKFILL = "backfill"
CMD_LOGIN = "login"
CMD_PROBE = "probe"
CMD_STOP = "stop"


class Orchestrator:
    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()

        self._status: dict[str, Any] = {
            "session": "unknown",
            "session_message": "",
            "busy": False,
            "busy_action": "",
            "last_scrape": None,
            "last_scrape_message": "",
            "next_scrape": None,
            "last_heartbeat": None,
            "progress": "",
            "backfill": None,       # dict while/after a backfill, else None
        }
        self._last_daily: date | None = None
        self._last_heartbeat: datetime | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="lsm-worker", daemon=True
        )
        self._thread.start()
        log.info("orchestrator started")

    def stop(self) -> None:
        self._q.put((CMD_STOP, {}))
        if self._thread:
            self._thread.join(timeout=10)

    def submit(self, command: str, **kwargs: Any) -> None:
        self._q.put((command, kwargs))

    # ── Public API used by Flask / tray ──────────────────────────────────

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def request_scrape(self, interactive: bool = False) -> None:
        self.submit(CMD_SCRAPE, trigger="manual", interactive=interactive)

    def request_backfill(self, months: int | None = None,
                         auto: bool = False) -> None:
        """Queue a history fill. `auto` marks the app's own first-run one."""
        self.submit(CMD_BACKFILL, months=months, auto=auto)

    def request_login(self) -> None:
        self.submit(CMD_LOGIN)

    def request_probe(self) -> None:
        self.submit(CMD_PROBE)

    def is_busy(self) -> bool:
        return bool(self.status().get("busy"))

    # ── Worker loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            store.init_db()
        except Exception:
            log.exception("database init failed")

        self._bootstrap_session()

        while True:
            timeout = self._seconds_until_next_tick()
            try:
                command, kwargs = self._q.get(timeout=timeout)
            except queue.Empty:
                command, kwargs = ("", {})

            if command == CMD_STOP:
                log.info("worker stopping")
                session.shutdown()
                return

            if command == CMD_PROBE:
                self._do_probe()
            elif command == CMD_LOGIN:
                self._do_login()
            elif command == CMD_SCRAPE:
                self._do_scrape(
                    trigger=kwargs.get("trigger", "manual"),
                    interactive=kwargs.get("interactive", False),
                )
            elif command == CMD_BACKFILL:
                self._do_backfill(months=kwargs.get("months"),
                                  auto=kwargs.get("auto", False))
            else:
                self._tick()

    def _seconds_until_next_tick(self) -> float:
        """Sleep in short slices so manual commands feel responsive."""
        return 20.0

    def _tick(self) -> None:
        now = datetime.now()

        if self._due_for_daily(now):
            # Mark the day as attempted *before* scraping. If we only marked
            # it on success, a dead session would make every 20s tick launch
            # another browser against LSM — a retry storm. A failed daily
            # waits for the heartbeat or a manual retry instead.
            self._last_daily = now.date()
            self._do_scrape(trigger="schedule")
            return

        if self._due_for_heartbeat(now):
            self._do_heartbeat()

    def _due_for_daily(self, now: datetime) -> bool:
        if self._last_daily == now.date():
            return False
        target = _parse_time(SCRAPE_TIME)
        if now.time() < target:
            return False
        return True

    def _due_for_heartbeat(self, now: datetime) -> bool:
        if self._last_heartbeat is None:
            return False
        return now - self._last_heartbeat >= timedelta(hours=HEARTBEAT_HOURS)

    # ── Operations (worker thread only) ──────────────────────────────────

    def _bootstrap_session(self) -> None:
        """First-run session check, plus a catch-up scrape if we are stale."""
        self._set(busy=True, busy_action="Checking session", progress="")
        try:
            state = session.probe(headless=True)
            self._set_session(state)

            if not state.ok:
                log.info("starting without a live session (%s)", state.state)
                # A scrape cannot succeed without a session, so do not burn
                # a browser launch on it now. Signing in triggers one.
                self._last_daily = datetime.now().date()
                return

            self._last_heartbeat = datetime.now()

            last = store.last_run()
            stale = True
            if last and last.get("finished_at"):
                try:
                    age = datetime.now() - datetime.fromisoformat(last["finished_at"])
                    stale = age > timedelta(hours=20)
                except ValueError:
                    stale = True

            if SCRAPE_ON_START and stale:
                self._set(busy=False, busy_action="", progress="")
                self._do_scrape(trigger="startup")

            self._queue_backfill_if_owed()
        finally:
            self._set(busy=False, busy_action="Checking session", progress="")

    def _queue_backfill_if_owed(self) -> None:
        """Ask for the one-time history fill, once per install.

        A fresh database holds only the four months the daily window covers,
        and no later scrape reaches further back — so without this the older
        months are simply never fetched. It is a first-run job the app gives
        itself, not a control: there is no button for it, and store.backfill_done()
        retires the call for good as soon as one clean run lands.

        Only ever called once the session is known good. Queued rather than run
        inline so the worker loop owns the busy/progress bookkeeping, and called
        after an interactive sign-in as well as at startup — that way a fill cut
        short by an expired session resumes on the next sign-in rather than
        waiting for a restart.
        """
        if store.backfill_done():
            return
        log.info("no completed backfill yet — queueing the one-time history fill")
        self.request_backfill(auto=True)

    def _do_probe(self) -> None:
        self._set(busy=True, busy_action="Checking session")
        try:
            self._set_session(session.probe(headless=True))
        finally:
            self._set(busy=False, busy_action="")

    def _do_heartbeat(self) -> None:
        log.info("heartbeat")
        ok = session.heartbeat()
        self._last_heartbeat = datetime.now()
        self._set(last_heartbeat=datetime.now().isoformat())
        self._set(session="ok" if ok else "expired")
        if not ok:
            self._set(session_message="Session expired — sign in when convenient")

    def _do_login(self) -> None:
        self._set(busy=True, busy_action="Waiting for sign-in",
                 progress="A browser window is open — sign in and approve Duo.")
        try:
            state = session.interactive_login(
                on_status=lambda m: self._set(progress=m)
            )
            self._set_session(state)
            if state.ok:
                self._set(progress="Signed in")
                self._do_scrape(trigger="manual")
                # A backfill that died on an expired session is still owed, and
                # signing in is the moment it becomes possible again.
                self._queue_backfill_if_owed()
        finally:
            self._set(busy=False, busy_action="", progress="")

    def _do_scrape(self, trigger: str = "manual", interactive: bool = False) -> None:
        self._set(busy=True, busy_action="Scraping LSM",
                  progress="Starting…", last_scrape_message="")
        run_id = store.start_run(trigger)

        try:
            state = session.probe(headless=True)
            self._set_session(state)

            if not state.ok and interactive:
                state = session.interactive_login(
                    on_status=lambda m: self._set(progress=m)
                )
                self._set_session(state)

            if not state.ok:
                store.finish_run(
                    run_id, "auth_required",
                    message="Session expired — sign in required",
                )
                self._set(
                    last_scrape=datetime.now().isoformat(),
                    last_scrape_message="Session expired — sign in required",
                    progress="",
                )
                log.info("scrape skipped: session expired")
                return

            self._set(progress="Running report…")
            date_from, date_to = default_window(
                SCRAPE_MONTHS_BACK, SCRAPE_MONTHS_AHEAD
            )
            result: ScrapeResult = scrape(
                date_from=date_from,
                date_to=date_to,
                on_status=lambda m: self._set(progress=m),
            )

            if result.status in ("ok", "empty"):
                excluded = _excluded_rooms(result.rooms)
                kept = [
                    e for e in result.events
                    if e.get("room") not in excluded and not e.get("cancelled")
                ]
                store.replace_events(kept, result.date_from, result.date_to,
                                     run_id=run_id, trigger=trigger)
                store.replace_rooms(
                    [describe(r) for r in _rooms_from(kept, result.rooms)]
                )
                store.prune()

                status = "ok" if kept else "empty"
                store.finish_run(
                    run_id, status, events_count=len(kept),
                    date_from=result.date_from, date_to=result.date_to,
                    message=f"{len(result.events)} parsed, {len(kept)} kept",
                )
                self._set(
                    last_scrape=datetime.now().isoformat(),
                    last_scrape_message=f"{len(kept)} bookings",
                    progress="",
                    session="ok",
                )
                self._last_daily = datetime.now().date()
                self._last_heartbeat = datetime.now()
                log.info("scrape complete — %d bookings stored", len(kept))
            else:
                store.finish_run(run_id, result.status, message=result.message)
                self._set(
                    last_scrape=datetime.now().isoformat(),
                    last_scrape_message=result.message or result.status,
                    progress="",
                )
                if result.status == "auth_required":
                    self._set(session="expired",
                              session_message="Session expired — sign in required")
        except Exception as exc:
            log.exception("scrape failed")
            try:
                store.finish_run(run_id, "error", message=str(exc))
            except Exception:
                pass
            self._set(last_scrape=datetime.now().isoformat(),
                      last_scrape_message=f"Failed: {exc}", progress="")
        finally:
            self._set(busy=False, busy_action="")

    # ── Helpers ──────────────────────────────────────────────────────────

    def _do_backfill(self, months: int | None = None,
                     auto: bool = False) -> None:
        """
        Fetch `months` calendar months of history, one report run per month.

        `auto` marks the first-run fill the app gives itself (see
        _queue_backfill_if_owed) as opposed to `--backfill` run by hand. It
        changes only what the log and the sidebar say — the work is identical,
        which is what makes the CLI a usable repair path for a fill that did
        not complete.

        Deliberately unlike _do_scrape, and the differences are the point:

        * _last_daily is NOT stamped. The daily scrape has its own window;
          stamping it here would make the worker skip that day's scrape.
        * _last_heartbeat is NOT stamped either. This does exercise the
          session, so resetting the clock would be defensible, but leaving it
          alone costs at most one redundant ping.
        * last_scrape / last_scrape_message are NOT touched — those describe
          the last *scrape*, and a backfill is not one.
        * record_changes=False. A first observation is not a change; a year
          of "added" rows would bury every real change in the feed. The
          run's own scrape_runs row is the record of what it did.
        * prune() is NOT called per chunk. It runs once at the end, against
          the whole year rather than a half-filled window.

        A chunk that comes back EMPTY is a skip, not a failure. See the
        comment at the empty branch below — treating it as a failure is what
        stopped the one-time fill from ever settling.

        Each chunk is its own committed transaction, so a session that dies
        partway leaves the finished months in place and the run can simply be
        started again — a completed month re-scrapes to zero changes.
        """
        months = int(months or BACKFILL_MONTHS)
        windows = backfill_windows(months, SCRAPE_MONTHS_BACK)
        what = "first-run backfill" if auto else "backfill"
        # Guarded once, here. The reach is written to the run row at two points
        # ninety lines apart, and `windows` is empty for any reach of one month
        # or less — backfill_windows() is range(months, 1, -1). Indexing
        # windows[0] at the finish_run call is what turned
        # `--backfill --months 1` into an IndexError.
        reach_from = windows[0][0] if windows else ""
        reach_to = windows[-1][1] if windows else ""
        log.info("%s: %d window(s), %s → %s", what, len(windows),
                 reach_from or "—", reach_to or "—")
        self._set(busy=True,
                  busy_action="Backfilling history" + (" (first run)" if auto else ""),
                  progress="Starting…")
        self._set_backfill(running=True, months=months, done=0,
                           total=len(windows), stored=0, failed=0, empty=0,
                           message="", finished_at=None)
        run_id = store.start_run("backfill")
        stored = 0
        errored: list[str] = []      # fatal: threw, or reported a real failure
        empty: list[str] = []        # skipped: the report had nothing to give

        try:
            if not windows:
                # The reach asked for starts inside the daily window, so there
                # is nothing to fetch. Finishing `ok` here would print
                # "0 bookings over 0 months" and retire the one-time fill
                # without a single booking stored.
                store.finish_run(
                    run_id, "error",
                    message=f"No backfill window for months={months} — the "
                            f"daily scrape already covers that reach",
                )
                errored.append("")
                self._set_backfill(running=False, failed=1,
                                   finished_at=datetime.now().isoformat(),
                                   message=f"Nothing to fetch for months={months}")
                log.warning("%s: no windows for months=%d", what, months)
                return

            state = session.probe(headless=True)
            self._set_session(state)
            if not state.ok:
                store.finish_run(run_id, "auth_required",
                                 message="Backfill skipped — sign in required")
                self._set_backfill(running=False, message="Sign in required")
                return

            for i, (win_from, win_to) in enumerate(windows, 1):
                self._set(progress=f"Backfill {i}/{len(windows)}: "
                                   f"{win_from} → {win_to}")
                try:
                    # No on_status: scrape() reports "Scraping 01/02/2026 →
                    # 28/02/2026", which would overwrite the month counter set
                    # above with the same dates minus the position in the run.
                    # For a backfill the label above is the better one.
                    result = scrape(date_from=win_from, date_to=win_to)
                except Exception:
                    log.exception("backfill chunk threw (%s → %s)",
                                  win_from, win_to)
                    errored.append(win_from)
                    self._set_backfill(failed=len(errored))
                    continue

                if result.status == "auth_required":
                    # The session is gone; every later chunk would fail the
                    # same way. Stop, and leave the finished months standing.
                    store.finish_run(
                        run_id, "auth_required", events_count=stored,
                        date_from=reach_from, date_to=reach_to,
                        message=f"Session expired after {i - 1}/{len(windows)} months",
                    )
                    self._set(session="expired",
                              session_message="Session expired — sign in required")
                    self._set_backfill(running=False,
                                       finished_at=datetime.now().isoformat(),
                                       message="Session expired")
                    log.info("backfill stopped: session expired")
                    return

                if result.status == "empty":
                    # A skip, NOT a failure. The daily scrape treats an empty
                    # window as suspicious because it covers a live four-month
                    # reach; an eleven-month reach into the past always spans a
                    # summer, and "no data found" is the normal answer for a
                    # month in which no Rotman room was booked. Counting those
                    # as failures made the whole run `error`, so
                    # store.backfill_done() never settled and every launch
                    # refetched all eleven months — three minutes of busy and
                    # eleven report runs, forever.
                    #
                    # The cost of the leniency: an empty month and a failed
                    # render are indistinguishable here, so a genuinely failed
                    # render goes unfilled. That is bounded and safe — nothing
                    # is destroyed, because replace_events refuses to reconcile
                    # an empty report, so the month is simply never populated
                    # rather than emptied.
                    log.info("backfill: %s → %s came back empty — skipped",
                             win_from, win_to)
                    empty.append(win_from)
                    self._set_backfill(empty=len(empty))
                    continue

                if result.status != "ok":
                    errored.append(win_from)
                    self._set_backfill(failed=len(errored))
                    continue

                excluded = _excluded_rooms(result.rooms)
                kept = [
                    e for e in result.events
                    if e.get("room") not in excluded and not e.get("cancelled")
                ]
                stored += store.replace_events(
                    kept, result.date_from, result.date_to,
                    run_id=run_id, trigger="backfill",
                    record_changes=False,
                )
                store.replace_rooms(
                    [describe(r) for r in _rooms_from(kept, result.rooms)]
                )
                self._set_backfill(done=i, stored=stored)

            store.prune()

            # `ok` turns on errored alone. store.backfill_done() gates the
            # one-time fill on this status, so a run whose only blemish is a
            # genuinely empty month has to be able to settle.
            status = "ok" if not errored else "error"
            notes = []
            if empty:
                notes.append(f"{len(empty)} month(s) empty")
            if errored:
                notes.append(f"{len(errored)} errored")
            store.finish_run(
                run_id, status, events_count=stored,
                date_from=reach_from, date_to=reach_to,
                message=(f"{stored} bookings over {len(windows)} months"
                         + ("; " + "; ".join(notes) if notes else "")),
            )
            self._set_backfill(running=False, failed=len(errored),
                               empty=len(empty),
                               finished_at=datetime.now().isoformat(),
                               message=f"{stored} bookings over "
                                       f"{len(windows)} months"
                                       + (f" ({'; '.join(notes)})" if notes else ""))
            log.info("%s complete — %d bookings, %d empty, %d errored",
                     what, stored, len(empty), len(errored))
        except Exception as exc:
            log.exception("backfill failed")
            try:
                store.finish_run(run_id, "error", message=str(exc))
            except Exception:
                pass
            # Counted, not merely logged. run_backfill derives its exit code
            # from this count, so without it a run that threw outright still
            # reported success — the worst possible answer from the one command
            # a person runs when the fill has already gone wrong.
            errored.append("")
            self._set_backfill(running=False, failed=len(errored),
                               finished_at=datetime.now().isoformat(),
                               message=f"Failed: {exc}")
        finally:
            self._set(busy=False, busy_action="", progress="")

    def _set_session(self, state: session.SessionState) -> None:
        self._set(session=state.state, session_message=state.message)

    def _set(self, **kwargs: Any) -> None:
        with self._status_lock:
            self._status.update(kwargs)

    def _set_backfill(self, **kwargs: Any) -> None:
        """Merge into the nested backfill dict under the lock.

        _set does a shallow update, so replacing the whole dict from here
        would race /api/status reading it on another thread.
        """
        with self._status_lock:
            bf = dict(self._status.get("backfill") or {})
            bf.update(kwargs)
            self._status["backfill"] = bf


def _excluded_rooms(seen: Iterable[str] = ()) -> set[str]:
    """Rooms to keep out of the calendar.

    `seen` must be the rooms in the report we are holding right now. It used
    to read the persisted rooms table instead, which is filled with every
    room the report shuttle offers — including the excluded ones — so the
    exclusion set was self-referential and nothing was ever excluded.
    """
    import re

    from app.config import EXCLUDED_PATTERNS, EXCLUDED_ROOMS

    excluded = set(EXCLUDED_ROOMS)
    patterns = [re.compile(p) for p in EXCLUDED_PATTERNS]
    for room in seen:
        if any(p.match(room) for p in patterns):
            excluded.add(room)
    return excluded


def _rooms_from(events: list[dict[str, Any]], all_rooms: list[str]) -> list[str]:
    seen = {e["room"] for e in events if e.get("room")}
    return sorted(seen | set(all_rooms or []))


def _parse_time(raw: str) -> dtime:
    try:
        hh, mm = raw.split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return dtime(6, 0)
