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
from typing import Any

from app import session, store
from app.config import (
    HEARTBEAT_HOURS, SCRAPE_MONTHS_AHEAD, SCRAPE_MONTHS_BACK,
    SCRAPE_ON_START, SCRAPE_TIME, log,
)
from app.rooms import describe
from app.scrape import ScrapeResult, default_window, scrape

# Commands the worker understands.
CMD_SCRAPE = "scrape"
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
        finally:
            self._set(busy=False, busy_action="Checking session", progress="")

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
                excluded = _excluded_rooms()
                kept = [
                    e for e in result.events
                    if e.get("room") not in excluded and not e.get("cancelled")
                ]
                store.replace_events(kept, result.date_from, result.date_to)
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

    def _set_session(self, state: session.SessionState) -> None:
        self._set(session=state.state, session_message=state.message)

    def _set(self, **kwargs: Any) -> None:
        with self._status_lock:
            self._status.update(kwargs)


def _excluded_rooms() -> set[str]:
    import re

    from app.config import EXCLUDED_PATTERNS, EXCLUDED_ROOMS

    excluded = set(EXCLUDED_ROOMS)
    patterns = [re.compile(p) for p in EXCLUDED_PATTERNS]
    for room in {r["room"] for r in store.get_rooms()}:
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
