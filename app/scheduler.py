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
* Update check every UPDATE_CHECK_HOURS against the repo's GitHub releases
  (see app/updater.py), plus a manual tray/sidebar check.
* Serve manual "Scrape now" / "Sign in" / "Check session" requests.
"""

from __future__ import annotations

import queue
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app import session, store, updater
from app.config import (
    APP_VERSION, BACKFILL_MONTHS, HEARTBEAT_HOURS, SCRAPE_MONTHS_AHEAD,
    SCRAPE_MONTHS_BACK, SCRAPE_ON_START, SCRAPE_TIME, UPDATE_CHECK_HOURS, log,
)
from app.rooms import describe
from app.scrape import (
    ScrapeResult, backfill_windows, default_window, scrape,
)

# Commands the worker understands.
CMD_SCRAPE = "scrape"
CMD_BACKFILL = "backfill"
CMD_LOGIN = "login"
CMD_LOGOUT = "logout"
CMD_PROBE = "probe"
CMD_CHECK_UPDATE = "check_update"
CMD_INSTALL_UPDATE = "install_update"
CMD_STOP = "stop"


class Orchestrator:
    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        # Set by stop(), read by the loop before each command. CMD_STOP alone
        # queues behind whatever is already waiting — a first-run backfill is
        # a dozen report runs — so Quit waited out stop()'s join and the
        # process exited with the worker still inside Playwright. The flag
        # jumps the queue; CMD_STOP is still sent to wake an idle get().
        self._stop = threading.Event()
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
            # The updater's half of the status page. A nested dict for the
            # same reason backfill is: it has its own writers (_set_update)
            # and its own reader (/api/status merges it wholesale).
            "update": {
                # idle|checking|available|latest|downloading|ready|installing|failed
                "state": "idle",
                "current_version": APP_VERSION,
                "latest_version": "",
                "notes_url": "",
                "message": "",
                "last_check": None,
                "progress": "",
                "token_set": False,
                "staged": "",
            },
        }
        self._last_daily: date | None = None
        # The day a scheduled-window scrape last *succeeded*. _last_daily is
        # only "attempted" (the retry-storm guard), so on its own a daily that
        # failed — a laptop waking before its network, LSM down at 06:00 —
        # left the calendar a day old until tomorrow. The heartbeat reads this
        # to retry at its own pace (see _do_heartbeat).
        self._daily_ok: date | None = None
        self._last_heartbeat: datetime | None = None
        # UI hooks for the updater (main.py wires the tray in): a check that
        # finds something announces it, an install that has staged and
        # verified the installer ends the process. Held as callables so this
        # module stays UI-agnostic — the worker must not import the window.
        # A check that lands before they are set simply does not toast;
        # the sidebar is the durable record either way.
        self._update_notify = None
        self._update_quit = None
        # (path, sha256) of the installer _stage_update last verified. The
        # staged file sits in a user-writable directory between the check
        # and the click — hours, sometimes — so the install re-hashes it
        # against this before running it rather than trusting the earlier
        # verdict. Private, not in the status dict: /api/status has no use
        # for it.
        self._staged_sha256: tuple[str, str] | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="lsm-worker", daemon=True
        )
        self._thread.start()
        log.info("orchestrator started")

    def stop(self) -> None:
        self._stop.set()
        self._q.put((CMD_STOP, {}))
        # The update path calls the quit hook from this worker's own thread,
        # and a thread joining itself raises RuntimeError. It also does not
        # need the join: the caller *is* the loop being stopped, and the
        # process is about to exit through the window's teardown anyway.
        if self._thread and self._thread is not threading.current_thread():
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

    def request_logout(self) -> None:
        self.submit(CMD_LOGOUT)

    def request_probe(self) -> None:
        self.submit(CMD_PROBE)

    def request_update_check(self, trigger: str = "manual") -> None:
        self.submit(CMD_CHECK_UPDATE, trigger=trigger)

    def request_update_install(self) -> None:
        self.submit(CMD_INSTALL_UPDATE)

    def set_update_hooks(self, notify: Any = None, quit: Any = None) -> None:
        """Wire the tray in: notify(title, message) on a found update,
        quit() once a verified installer is running. main.py calls this
        after the Tray exists; before then the hooks are simply None."""
        self._update_notify = notify
        self._update_quit = quit

    def skip_update(self, tag: str) -> None:
        """Mirror a skip in the live state, not only in the store.

        The store write (server.py) is what makes the skip survive a
        restart, but without this the banner, the Install button and the
        Skip link stay on screen until the next check — up to 24 h — and a
        person who just declined an offer keeps looking at it. This runs on
        the Flask request thread, not the worker: it touches only the
        lock-guarded status dict, enqueues nothing, and the next check
        re-derives the same state from the store anyway.
        """
        self._set_update(state="latest",
                         message=f"Skipped {tag} — a newer release will "
                                 "show here.")

    def is_busy(self) -> bool:
        return bool(self.status().get("busy"))

    # ── Worker loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            store.init_db()
        except Exception:
            log.exception("database init failed")

        # Guarded too: a staged installer left by a previous run is stale
        # baggage, and failing to delete it must not fail the worker.
        try:
            updater.purge_staging()
        except Exception:
            log.exception("update staging purge failed")

        # This thread is the app's only scraper and nothing restarts it, so
        # every failure inside it is contained here. Before this, one exception
        # anywhere below -- a Playwright launch failure inside the bootstrap
        # probe, an OSError out of the heartbeat, a raising store read -- ended
        # _run for the life of the process: no further scrape would ever be
        # queued, no error would be shown, and the window would go on
        # displaying whatever status it held at the time. A dropped command is
        # a bad afternoon. A dead worker is a silently frozen app.
        try:
            self._bootstrap_session()
        except Exception:
            log.exception("session bootstrap failed; the worker will keep running")

        while True:
            # Checked before the get as well as after it: work queued ahead
            # of CMD_STOP is skipped, not run, once Quit has been asked for.
            command, kwargs = (CMD_STOP, {})
            if not self._stop.is_set():
                timeout = self._seconds_until_next_tick()
                try:
                    command, kwargs = self._q.get(timeout=timeout)
                except queue.Empty:
                    command, kwargs = ("", {})

            if command == CMD_STOP or self._stop.is_set():
                log.info("worker stopping")
                # Guarded too, and for the same reason: raising here would
                # kill the thread on its way out rather than stop it, so the
                # browser would be left open with nothing to close it later.
                try:
                    session.shutdown()
                except Exception:
                    log.exception("session shutdown failed")
                return

            try:
                self._dispatch(command, kwargs)
            except Exception:
                log.exception("command %r failed; the worker will keep running",
                              command or "(tick)")
                # busy is cleared as well. A handler that died between setting
                # it and reaching its own finally would otherwise leave the UI
                # claiming work is in progress that nothing is doing -- the
                # same freeze as a dead thread, seen from the window.
                self._set(busy=False, busy_action="", progress="")

    def _dispatch(self, command: str, kwargs: dict) -> None:
        if command == CMD_PROBE:
            self._do_probe()
        elif command == CMD_LOGIN:
            self._do_login()
        elif command == CMD_LOGOUT:
            self._do_logout()
        elif command == CMD_SCRAPE:
            self._do_scrape(
                trigger=kwargs.get("trigger", "manual"),
                interactive=kwargs.get("interactive", False),
            )
        elif command == CMD_BACKFILL:
            self._do_backfill(months=kwargs.get("months"),
                              auto=kwargs.get("auto", False))
        elif command == CMD_CHECK_UPDATE:
            self._do_check_update(trigger=kwargs.get("trigger", "manual"))
        elif command == CMD_INSTALL_UPDATE:
            self._do_install_update()
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
            # waits for the heartbeat (which retries it once the session
            # answers again) or a manual retry instead.
            self._last_daily = now.date()
            self._do_scrape(trigger="schedule")
            return

        if self._due_for_heartbeat(now):
            self._do_heartbeat()

        if self._due_for_update_check(now):
            self._do_check_update(trigger="auto")

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
        age = now - self._last_heartbeat
        # A negative age is a stamp taken while the clock ran ahead. Read as
        # "not yet", it would hold the keep-alive off until that future time.
        return age < timedelta(0) or age >= timedelta(hours=HEARTBEAT_HOURS)

    def _due_for_update_check(self, now: datetime) -> bool:
        """Due when the last check is unknown or older than 24 hours.

        The timestamp lives in the database (store.load_update_state), not in
        this object: the cadence is a fact about the machine, not the process,
        so a restart must not reset it — otherwise an app that is relaunched
        often would check GitHub on every launch instead of once a day. A
        corrupt timestamp reads as due; the cost is one check. So does one in
        the future — stamped while the clock was set ahead, it would
        otherwise stop every check until the clock caught up with it.
        """
        last = store.load_update_state().get("last_check")
        if not last:
            return True
        try:
            age = now - datetime.fromisoformat(last)
        except (TypeError, ValueError):
            return True
        return age < timedelta(0) or age >= timedelta(hours=UPDATE_CHECK_HOURS)

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
                # But do arm the heartbeat. Left at None it never fires, and a
                # probe that failed only because the network was not up yet
                # (autostart at Windows sign-in) would cost the whole day's
                # refresh and every keep-alive after it. The heartbeat re-probes
                # in HEARTBEAT_HOURS and runs the owed daily if it answers.
                self._last_heartbeat = datetime.now()
                return

            self._last_heartbeat = datetime.now()

            # Fresh means the newest scrape *stored* something. A run that
            # failed or wanted a sign-in is a reason to scrape, not to skip.
            now = datetime.now()
            last = store.last_run()
            stale = True
            if last and last.get("status") in ("ok", "empty"):
                try:
                    age = now - datetime.fromisoformat(last["finished_at"])
                    stale = age > timedelta(hours=20)
                    # And if that run was today's daily, this process owes no
                    # other. Without this every relaunch after 06:00 — the
                    # updater's own included — scraped again 20 s in.
                    started = datetime.fromisoformat(last["started_at"])
                    if started.date() == now.date() and _counts_as_daily(started):
                        self._last_daily = self._daily_ok = now.date()
                except (TypeError, ValueError):
                    stale = True

            if SCRAPE_ON_START and stale:
                if _counts_as_daily(now):
                    # This run is today's daily, so mark it attempted just as
                    # _tick does; if it fails the heartbeat retries it, and the
                    # tick does not launch a second browser 20 s later.
                    self._last_daily = now.date()
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
        # The probe's own state and message, not a collapsed ok/expired: a
        # slow or unreachable LSM fails this the same way an expired session
        # does, and the two call for different advice from the sidebar.
        state = session.heartbeat()
        self._last_heartbeat = datetime.now()
        self._set(last_heartbeat=datetime.now().isoformat())
        self._set_session(state)
        # The retry for a daily that failed. Paced by the heartbeat rather
        # than the tick, so a session that stays dead costs one headless probe
        # per HEARTBEAT_HOURS, never a browser launch every twenty seconds.
        if state.ok and self._owes_daily(datetime.now()):
            log.info("today's scheduled scrape has not succeeded — retrying")
            self._do_scrape(trigger="schedule")

    def _owes_daily(self, now: datetime) -> bool:
        """True once SCRAPE_TIME has passed today without a scrape succeeding."""
        return (self._daily_ok != now.date()
                and now.time() >= _parse_time(SCRAPE_TIME))

    # ── Update check / install (worker thread only) ──────────────────────

    def _do_check_update(self, trigger: str = "manual") -> None:
        """Ask GitHub what the latest release is, and say which of four
        things is true: something is available, we are current, the found
        release was skipped, or the check could not be made.

        The 24 h stamp is written *before* the check runs, for the same
        reason _due_for_daily marks the day first: an unreachable GitHub or
        a refused token must not turn the 20 s tick into a retry storm. A
        failed check waits a day, or for the manual tray item.
        """
        self._set(busy=True, busy_action="Checking for updates", progress="")
        self._set_update(state="checking", message="")
        try:
            # The stamp is written inside the try so a database error lands
            # in the failed state below instead of killing the command with
            # the update dict still looking idle — "a check happened and it
            # failed" must not read as "no check happened at all".
            state = store.load_update_state()
            state["last_check"] = datetime.now().isoformat()
            store.save_update_state(state)
            self._set_update(last_check=state["last_check"])
            token = updater.load_token()
            self._set_update(token_set=bool(token))
            release = updater.fetch_latest(token)
            tag = release["tag"]
            self._set_update(latest_version=tag, notes_url=release["notes_url"])

            if updater.parse_version(tag) is None:
                # /releases/latest only returns a release a maintainer
                # deliberately published, so an unparsable tag here is a
                # real release this app cannot place — "up to date" would
                # be a claim about a version it failed to read, and every
                # user would wait for a fix that already shipped.
                self._set_update(
                    state="failed",
                    message=f"The latest release is tagged '{tag}', which "
                            "this app cannot compare — it may be newer.")
                return

            if not updater.is_newer(tag, APP_VERSION):
                self._set_update(state="latest")
                return

            # A skipped release is hidden until something strictly newer
            # ships — the skip records "not this one", not "never tell me
            # again", and comparing against the skipped version rather than
            # erasing it on the next check is what keeps that distinction.
            skipped = state.get("skipped")
            if skipped and not updater.is_newer(tag, skipped):
                self._set_update(
                    state="latest",
                    message=f"Skipped {tag} — a newer release will show here.",
                )
                return

            # Pre-stage: the check already knows a newer release is out, so
            # it downloads and verifies the installer now — "Install update"
            # then starts the installer instead of a 42 MB wait, and a
            # machine that checks while connected has the bytes before its
            # user ever clicks. The worker is busy for the download either
            # way; the label says what is actually happening. A pre-stage
            # that fails is NOT a failed check: the offer still stands, the
            # sentence says why the click will be slower, and the install
            # path downloads the old way.
            self._set(busy_action="Downloading update")
            try:
                staged = self._stage_update(release, token)
                self._set_update(staged=staged, state="ready",
                                 message="", progress="")
            except updater.UpdateError as exc:
                log.warning("update pre-stage failed: %s", exc)
                self._set_update(state="available", message=str(exc))
            log.info("update available: %s (found via %s check)", tag, trigger)
            if self._update_notify:
                self._update_notify(
                    "Update available",
                    f"{tag} — see the calendar sidebar to install it",
                )
        except updater.UpdateError as exc:
            # The one honest sentence, into the sidebar where the fix is —
            # a toast would say "something failed" and vanish, leaving no
            # instruction behind. The same event goes to app.log, which is
            # the only record that survives the next check overwriting the
            # sidebar sentence.
            log.warning("update check failed: %s", exc)
            self._set_update(state="failed", message=str(exc))
        except Exception:
            log.exception("update check failed")
            self._set_update(state="failed",
                             message="The update check failed unexpectedly.")
        finally:
            self._set(busy=False, busy_action="", progress="")

    def _stage_update(self, release: dict, token: str | None) -> str:
        """Download the release's installer and verify it against the
        release's own checksum, returning the staged path.

        One method for both writers — the check's pre-stage and the install
        click — because the hash check is the load-bearing step and must be
        identical wherever the bytes came from: a download that does not
        match what the publisher published is never staged, never run,
        whatever went wrong with it. The hash is fetched before the download
        it describes: a release published without its sidecar fails in one
        small request instead of after a 42 MB download.
        """
        setup_name = str(updater.select_setup(release)["name"])
        expected = updater.fetch_sha256(release, token, setup_name)

        def _progress(done: int, total: int | None) -> None:
            text = f"{done / 1048576:.1f} MB"
            if total:
                text += f" of {total / 1048576:.1f} MB"
            self._set_update(progress=text)

        path = updater.download_setup(release, token, _progress)
        if not updater.verify_sha256(path, expected):
            raise updater.UpdateError(
                "The downloaded installer did not match the "
                "release's checksum — it will not be run. Try again."
            )
        # The checksum proves the file is what was published; the signature
        # proves who published it. Both, before anything is staged.
        problem = updater.verify_signature(path)
        if problem:
            raise updater.UpdateError(problem)
        self._staged_sha256 = (str(path), expected)
        return str(path)

    def _reverify_staged(self, staged: str) -> str | None:
        """None when the staged installer still passes both checks, else why.

        The verdict _stage_update reached is about the bytes as they were at
        check time, and the staging directory is writable by anything running
        as this user. So the checks are asked again right before the launch.
        The hash needs the expected value _stage_update kept; a path it did
        not stage (none is, in the app) gets the signature check alone.
        """
        path = Path(staged)
        try:
            known = self._staged_sha256
            if known and known[0] == staged and not updater.verify_sha256(
                    path, known[1]):
                return ("The staged installer has changed since it was "
                        "verified — it will not be run. Try again.")
        except OSError:
            return ("The staged installer could not be read — it will not "
                    "be run. Try again.")
        return updater.verify_signature(path)

    def _do_install_update(self) -> None:
        """Put the release's installer on disk verified (unless the check
        already did), run it, and end the process.

        The installer gates its file copy on this app exiting (Inno's
        CloseApplications), so launching before the quit is an ordering, not
        a race: the installer waits for the teardown rather than replacing
        files under a running app.
        """
        st = (self.status().get("update") or {})
        staged = st.get("staged") or ""
        if st.get("state") != "ready" or not staged or not Path(staged).exists():
            self._set(busy=True, busy_action="Downloading update", progress="")
            self._set_update(state="downloading", message="", progress="")
            try:
                token = updater.load_token()
                self._set_update(token_set=bool(token))
                release = updater.fetch_latest(token)
                tag = release["tag"]
                if not updater.is_newer(tag, APP_VERSION):
                    self._set_update(state="latest",
                                     message="Nothing to install — you are "
                                             "already up to date.")
                    return

                # The click answered an offer, not "whatever is latest at
                # this moment". A release published between the check and
                # the click is not the version the sidebar was showing when
                # the user accepted it, so it is offered again rather than
                # installed — and latest_version is never rewritten past
                # the user's consent to mean a version they never saw.
                shown = (st.get("latest_version") or "")
                if shown and tag != shown:
                    self._set_update(
                        latest_version=tag,
                        state="available",
                        message=f"{tag} is out — a newer release shipped "
                                "since the last check.")
                    return

                staged = self._stage_update(release, token)
                self._set_update(staged=staged, state="ready", progress="")
            except updater.UpdateError as exc:
                # Error-grade on purpose: this handler is where a checksum
                # mismatch lands — the app was served a binary that does not
                # match what the publisher published — and app.log is the
                # only record that survives the next check overwriting the
                # sidebar sentence. The store write above is durable; the
                # sentence alone is not.
                log.error("update install failed: %s", exc)
                self._set_update(state="failed", message=str(exc))
                return
            except Exception:
                log.exception("update download failed")
                self._set_update(state="failed",
                                 message="The update download failed "
                                         "unexpectedly.")
                return
            finally:
                self._set(busy=False, busy_action="", progress="")

        # The state above (or the one we just built) says the staged file
        # was verified against the release's own checksum — but that was at
        # check time, so both checks are asked again on the bytes about to
        # run. A refusal clears `staged`, so the next click downloads afresh.
        problem = self._reverify_staged(staged)
        if problem:
            log.error("update install refused: %s", problem)
            self._set_update(state="failed", message=problem, staged="")
            return
        # From here on, the app's job is to get out of the installer's way.
        self._set_update(state="installing",
                         message="The installer is running — the app will "
                                 "close now.")
        try:
            updater.launch_installer(Path(staged))
        except Exception:
            log.exception("update installer could not be launched")
            self._set_update(state="failed",
                             message="The installer could not be started.")
            return
        # Close the browser driver here, on the thread that owns it. The quit
        # hook ends the process, and the CMD_STOP it queues — whose handler
        # would otherwise do this — is never reached by this same thread.
        try:
            session.shutdown()
        except Exception:
            log.exception("session shutdown failed")
        if self._update_quit:
            self._update_quit()

    def _do_logout(self) -> None:
        """Clear the session, then report what the probe found afterwards.

        Queued rather than performed in the request thread so it cannot open a
        second Chromium on the profile the worker is using. The probe inside
        session.logout() is the point of the whole thing: the message that
        reaches the sidebar says what is left, not what was attempted, so a
        clear that did not take effect reads as "still signed in" rather than
        as a success nobody can check.
        """
        self._set(busy=True, busy_action="Signing out", progress="")
        try:
            state = session.logout()
            self._set_session(state)
            if state.ok:
                self._set(session_message="Still signed in — the session "
                                          "cookies could not be cleared")
            else:
                self._set(progress="Signed out")
        finally:
            self._set(busy=False, busy_action="", progress="")

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
        # The day this run counts against is the day it started. Stamping
        # _last_daily with the completion date would let a run that crosses
        # midnight mark the new day as already scraped — suppressing its 06:00
        # run and stretching the gap to ~30 hours, with a window computed
        # against the old day besides.
        started = datetime.now()
        started_date = started.date()
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
                # The probe knows why it failed, and the reasons want different
                # advice: an expired session is fixed by signing in, a failed
                # navigation is not — one message for both sent the user
                # chasing a sign-in no amount of signing in could fix. The
                # panel was already told by _set_session above; this is the
                # message that gets persisted with the run.
                why = ("Session expired — sign in required"
                       if state.state == "expired"
                       else "No access to LSM's report"
                       if state.state == "no_access"
                       else (state.message or "Could not reach LSM"))
                store.finish_run(
                    run_id, "auth_required",
                    message=why,
                )
                self._set(
                    last_scrape=datetime.now().isoformat(),
                    last_scrape_message=why,
                    progress="",
                )
                log.info("scrape skipped: %s", why)
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
                                     run_id=run_id, trigger=trigger,
                                     complete=result.complete)
                store.replace_rooms(
                    [describe(r) for r in _rooms_from(kept, result.rooms)]
                )
                store.prune()

                status = "ok" if kept else "empty"
                # An incomplete report is not a quiet success. What it stored
                # is only part of the window and, more to the point, the
                # sidebar is about to say "N bookings" over a window that
                # really holds more. Saying so is the difference between a
                # scrape someone can act on and one that looks fine.
                note = (f"{len(result.events)} parsed, {len(kept)} kept"
                        if result.complete else
                        f"{len(kept)} bookings — read from the page, not the "
                        f"export, so this is partial")
                store.finish_run(
                    run_id, status, events_count=len(kept),
                    date_from=result.date_from, date_to=result.date_to,
                    message=note,
                )
                self._set(
                    last_scrape=datetime.now().isoformat(),
                    last_scrape_message=note,
                    progress="",
                    session="ok",
                )
                # Only a run from SCRAPE_TIME on is the day's refresh. A
                # sign-in or Scrape Now at 01:00 used to mark the day done and
                # cancel the 06:00 run, leaving the calendar on the night's data.
                if _counts_as_daily(started):
                    self._last_daily = started_date
                    self._daily_ok = started_date
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
                elif result.status == "no_access":
                    self._set(session="no_access",
                              session_message=result.message)
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
                           partial=0, message="", finished_at=None)
        run_id = store.start_run("backfill")
        stored = 0
        errored: list[str] = []      # fatal: threw, or reported a real failure
        empty: list[str] = []        # skipped: the report had nothing to give
        partial: list[str] = []      # stored, but read off the page not the file

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
                # Same distinction as _do_scrape's: an expired session wants
                # the sign-in advice, an unreachable LSM wants its own
                # message, and neither benefits from the other's.
                why = ("Backfill skipped — sign in required"
                       if state.state == "expired"
                       else "Backfill skipped — "
                            + (state.message or "could not reach LSM"))
                store.finish_run(run_id, "auth_required", message=why)
                # `failed` is what the CLI's exit code reads, and every
                # window here is one that was not fetched. Leaving it at 0
                # let --backfill exit 0 over an empty fill the run still owes.
                # finished_at goes with it so the row is not left looking
                # in-flight, unlike the mid-fill path below.
                self._set_backfill(running=False, message=why,
                                   failed=len(windows),
                                   finished_at=datetime.now().isoformat())
                return

            for i, (win_from, win_to) in enumerate(windows, 1):
                if self._stop.is_set():
                    # Quit was asked for. The months already fetched are
                    # committed; the rest stay owed (an `error` run does not
                    # retire the fill), and the worker gets to its shutdown
                    # inside stop()'s join instead of a dozen reports later.
                    store.finish_run(
                        run_id, "error", events_count=stored,
                        date_from=reach_from, date_to=reach_to,
                        message=f"Stopped after {i - 1}/{len(windows)} "
                                f"months — the app was closing")
                    self._set_backfill(running=False,
                                       finished_at=datetime.now().isoformat(),
                                       message="Stopped — the app was closing",
                                       failed=len(errored)
                                             + (len(windows) - (i - 1)))
                    log.info("backfill stopped: the app is closing")
                    return
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

                if result.status in ("auth_required", "no_access"):
                    # The session is gone, or this account cannot open the
                    # report; every later chunk would fail the same way.
                    # Stop, and leave the finished months standing.
                    denied = result.status == "no_access"
                    store.finish_run(
                        run_id, "auth_required", events_count=stored,
                        date_from=reach_from, date_to=reach_to,
                        message=(f"No access to LSM's report after "
                                 f"{i - 1}/{len(windows)} months" if denied else
                                 f"Session expired after {i - 1}/{len(windows)} months"),
                    )
                    if denied:
                        self._set(session="no_access",
                                  session_message=result.message)
                    else:
                        self._set(session="expired",
                                  session_message="Session expired — sign in required")
                    # Every month after the last fetched one is skipped by
                    # this stop, so they count as failed for the CLI exit.
                    self._set_backfill(running=False,
                                       finished_at=datetime.now().isoformat(),
                                       message="Session expired",
                                       failed=len(errored)
                                             + (len(windows) - (i - 1)))
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
                    #
                    # That answer has to be this month's, though. An empty
                    # result that is not complete — a Generate with no
                    # evidence of a render, or a partial room selection — is
                    # no evidence the month is quiet, only that some page
                    # was. It is counted partial, not empty, so the fill
                    # stays owed and the month is asked again.
                    if not result.complete:
                        log.info("backfill: %s → %s came back empty from an "
                                 "unproven report — left owed",
                                 win_from, win_to)
                        partial.append(win_from)
                        self._set_backfill(partial=len(partial))
                        continue
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
                    complete=result.complete,
                )
                store.replace_rooms(
                    [describe(r) for r in _rooms_from(kept, result.rooms)]
                )
                if not result.complete:
                    # The report could not be downloaded and this month was read
                    # off the rendered page instead, which is one page of an
                    # interactive report. `replace_events` already refuses to
                    # delete anything for it, so nothing is lost — but what the
                    # month holds is a fraction of its bookings, and the month
                    # is indistinguishable from a genuinely quiet one once it
                    # is stored. That has to reach the run's status.
                    partial.append(win_from)
                    self._set_backfill(partial=len(partial))
                self._set_backfill(done=i, stored=stored)

            store.prune()

            # `ok` turns on errored alone, and `partial` exists for the one
            # other blemish that is not a failure of the run: a month read off
            # the page rather than out of the report. store.backfill_done()
            # gates the one-time fill on this status, so both of the quiet
            # blemishes have to be told apart from each other.
            #
            # An empty month settles the fill, because an empty month is the
            # report's *answer* — a summer month with no Rotman bookings, which
            # will answer the same way however many times it is asked. Refusing
            # to settle on those retried eleven months at every launch for ever.
            # A partial month is the opposite: it is an answer about us, not
            # about the bookings. The export failed and we saw page one. Asking
            # again can genuinely do better, so the fill must stay owed — a
            # `partial` run retiring it would cement one page per month as the
            # history, and the sidebar would call that "History filled".
            if errored:
                status = "error"
            elif partial:
                status = "partial"
            else:
                status = "ok"
            notes = []
            if empty:
                notes.append(f"{len(empty)} month(s) empty")
            if partial:
                notes.append(f"{len(partial)} month(s) only partly read")
            if errored:
                notes.append(f"{len(errored)} errored")
            store.finish_run(
                run_id, status, events_count=stored,
                date_from=reach_from, date_to=reach_to,
                message=(f"{stored} bookings over {len(windows)} months"
                         + ("; " + "; ".join(notes) if notes else "")),
            )
            self._set_backfill(running=False, failed=len(errored),
                               empty=len(empty), partial=len(partial),
                               finished_at=datetime.now().isoformat(),
                               message=f"{stored} bookings over "
                                       f"{len(windows)} months"
                                       + (f" ({'; '.join(notes)})" if notes else ""))
            log.info("%s complete — %d bookings, %d empty, %d partial, %d errored",
                     what, stored, len(empty), len(partial), len(errored))
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
        # Any probe that found the session alive has just kept it warm, and
        # arms the heartbeat if nothing had yet. Before this only a finished
        # scrape armed it, so a sign-in or a "Check session" that succeeded
        # left a worker with no keep-alive until a scrape happened to land.
        if state.ok:
            self._last_heartbeat = datetime.now()

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

    def _set_update(self, **kwargs: Any) -> None:
        """Merge into the nested update dict under the lock — the same
        shallow-update race _set_backfill exists for."""
        with self._status_lock:
            upd = dict(self._status.get("update") or {})
            upd.update(kwargs)
            self._status["update"] = upd


def _excluded_rooms(seen: Iterable[str] = ()) -> set[str]:
    """Rooms to keep out of the calendar.

    `seen` must be the rooms in the report we are holding right now. It used
    to read the persisted rooms table instead, which is filled with every
    room the report shuttle offers — including the excluded ones — so the
    exclusion set was self-referential and nothing was ever excluded.

    Both spellings of a room go into the set, because the two things it gets
    compared against are spelled differently: the patterns are matched against
    the shuttle's own names, and the events carry what parse.normalise_room
    made of them ('RT 134A' → '134A'). Matching only the raw spelling made the
    exclusion depend on how the shuttle happened to write the room — '134A'
    was dropped and 'RT 134A' was stored — which for the 134-series, the one
    room the exclusion exists for, is the whole of it silently not happening.
    """
    import re

    from app.config import EXCLUDED_PATTERNS
    from app.parse import normalise_room

    # Case-insensitive: a room's letter is a letter and its case means nothing
    # to the room, so '134a' from the shuttle is the same room the pattern was
    # written to catch. Spelling, prefix and case are all dimensions this code
    # does not control, and the set is built to hold whichever it is handed.
    patterns = [re.compile(p, re.IGNORECASE) for p in EXCLUDED_PATTERNS]

    def matches(room: str) -> bool:
        return any(p.match(room) for p in patterns)

    excluded: set[str] = set()
    for room in seen:
        normalised = normalise_room(room)
        if matches(room) or matches(normalised):
            excluded.add(room)
            excluded.add(normalised)
    return excluded


def _rooms_from(events: list[dict[str, Any]], all_rooms: list[str]) -> list[str]:
    seen = {e["room"] for e in events if e.get("room")}
    return sorted(seen | set(all_rooms or []))


def _counts_as_daily(started: datetime) -> bool:
    """A scrape started at or after SCRAPE_TIME is that day's refresh."""
    return started.time() >= _parse_time(SCRAPE_TIME)


def _parse_time(raw: str) -> dtime:
    try:
        hh, mm = raw.split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return dtime(6, 0)
