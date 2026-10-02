"""
LSM session management — the "keep the cookie alive" layer.

How the session works
---------------------
LSM sits behind UofT Shibboleth SSO, which sets a `_shibsession_*` cookie
after a UTORid login + Duo MFA. APEX then carries its own session id in
the URL (`f?p=143:<page>:<session_id>`). Both expire — Shibboleth
typically after hours, not days.

Strategy
--------
1. A Chromium *persistent profile* lives at data/profile. Logging in once
   writes the Shibboleth cookie into that profile, so later launches are
   already authenticated and never touch the login page.
2. After every successful probe we snapshot `context.cookies()` and store
   it DPAPI-encrypted (data/session.bin). If Chromium drops a session
   cookie on a hard shutdown, we re-inject the snapshot before giving up.
3. A heartbeat re-pings LSM every few hours. Shibboleth idle-timeouts are
   shorter than its hard expiry, so a periodic touch keeps the session
   warm and the 6 AM scrape silent.

Nothing here stores a UTORid or password. When the session genuinely
dies the app asks the user to log in, opens a visible window, and waits.
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from app import dpapi
from app.config import (
    APEX_APP_ID, APEX_AUTH_URL, CALENDAR_TZ, DATA_DIR, LOGIN_URL_MARKERS,
    BROWSER_CHANNEL, LSM_HOST, LSM_PORTAL_URL, MFA_WAIT_MS,
    NAVIGATE_TIMEOUT_MS, PROFILE_DIR,
    log,
)

SESSION_FILE = DATA_DIR / "session.bin"
SESSION_URL_RE = re.compile(rf"f\?p={APEX_APP_ID}:\d+:(\d+)")

# How long the URL must sit unchanged on the LSM host, with no IdP bounce, before
# the login window concludes the profile's existing cookie is still good.
ALREADY_SIGNED_IN_S = 8.0


def _session_id(url: str | None) -> str | None:
    m = SESSION_URL_RE.search(url or "")
    return m.group(1) if m else None


def login_progress(url: str | None, start_sid: str | None, saw_login: bool) -> str:
    """
    Classify where the login window currently is.

    "login"     — bounced to the IdP; the user still has to authenticate.
    "signed_in" — back on LSM with evidence that we got there by
                  authenticating, rather than by simply loading the URL we
                  started from.
    "waiting"   — neither yet.

    The distinction matters: the portal URL we navigate to at the start is
    itself on the LSM host and is not a login URL, so "on LSM and not a
    login page" is true before the user has done anything. Accepting that
    as arrival is what made the sign-in window report success and close
    three seconds in.
    """
    if is_login_url(url):
        return "login"
    if LSM_HOST not in (url or ""):
        return "waiting"
    sid = _session_id(url)
    if saw_login or (sid and sid != start_sid):
        return "signed_in"
    return "waiting"


def is_login_url(url: str | None) -> bool:
    """
    True when the browser has been bounced to an SSO page, meaning the
    session is gone. UofT redirects through several hosts
    (weblogin.utoronto.ca, idpz.utorauth.utoronto.ca), so this matches
    markers anywhere in the URL rather than specific hostnames.
    """
    u = (url or "").lower()
    return any(marker in u for marker in LOGIN_URL_MARKERS)

# Chromium only allows one process per profile dir; every entry point
# takes this lock so the interactive login window and the scheduler
# can never collide.
_lock = threading.RLock()
_pw = None  # reused Playwright driver, started lazily


# What a person whose UofT account cannot open the report is told, from every
# surface that learns it (probe, sign-in, scrape). Before this state existed
# such an account was sent round a sign-in loop — "session expired", or a raw
# "P51_FR_DATE missing" every morning — with nothing saying that signing in
# again could never help. It is a state rather than an error because only a
# person can fix it, and the fix is outside this app.
NO_ACCESS_MESSAGE = ("Signed in, but this UofT account can't open LSM's Rotman "
                     "bookings report. Ask the LSM administrator for access — "
                     "or, if you used a different UTORid, sign out and sign in "
                     "again.")

# APEX's own wording for an authorization failure ("Access denied by Page
# security check", "... Application security check") and the generic shapes
# around it. Read from the page's text only after the report failed to load,
# never as a reason on its own to distrust a page that did load.
_DENIED_RE = re.compile(
    r"access denied|not authori[sz]ed|insufficient privileges"
    r"|you do not have (?:the )?(?:permission|access|privileges?)",
    re.IGNORECASE,
)

# A missing browser surfaces as Playwright's "Executable doesn't exist at
# ...ms-playwright..." — accurate, and useless to the person reading it.
NO_BROWSER_MESSAGE = ("The browser this app drives isn't installed. Install "
                      "or repair Microsoft Edge, or reinstall the app with "
                      "its browser step ticked.")


def friendly_error(exc: BaseException) -> str:
    text = str(exc)
    if "Executable doesn't exist" in text or "executable doesn't exist" in text:
        return NO_BROWSER_MESSAGE
    return text


def access_denied(page: Any) -> bool:
    """Whether the page says this account is not allowed in."""
    if "LOGIN_DESKTOP" in (page.url or "").upper():
        return True
    try:
        text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False
    return bool(_DENIED_RE.search(text or ""))


@dataclass
class SessionState:
    state: str                      # ok | expired | no_access | error
    session_id: str | None = None
    message: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _driver():
    global _pw
    if _pw is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
    return _pw


def shutdown() -> None:
    global _pw
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None


@contextmanager
def browser(headless: bool = True, restore: bool = True) -> Iterator[Any]:
    """Persistent-profile browser context, serialised behind a process lock.

    Edge when config found it (BROWSER_CHANNEL), Playwright's Chromium
    otherwise — both are driven through the chromium engine.
    """
    from app.config import SLOW_MO_MS

    extra: dict[str, Any] = {"channel": BROWSER_CHANNEL} if BROWSER_CHANNEL else {}
    with _lock:
        ctx = _driver().chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            **extra,
            headless=headless,
            slow_mo=SLOW_MO_MS,
            viewport={"width": 1400, "height": 950},
            locale="en-CA",
            timezone_id=CALENDAR_TZ,
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            if restore:
                _restore_cookies(ctx)
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:
                pass


# ── Cookie snapshot ──────────────────────────────────────────────────────

def _save_cookies(ctx: Any) -> None:
    try:
        cookies = ctx.cookies()
        shib = [c for c in cookies if "shibsession" in c.get("name", "").lower()]
        if not shib:
            # A snapshot with no Shibboleth cookie in it can never restore a
            # session, and writing one would clobber a snapshot that still
            # could. This is the fingerprint of a login that did not really
            # happen — the log used to show "6 cookies, 0 shibboleth" right
            # before the session was found to be expired.
            log.warning(
                "not saving session snapshot: no Shibboleth cookie among %d",
                len(cookies),
            )
            return
        blob = json.dumps({"saved_at": datetime.now().isoformat(),
                           "cookies": cookies}).encode("utf-8")
        # Belt and braces with dpapi.protect, which refuses on its own. This
        # check exists so the log line can be specific about *why* nothing was
        # saved, rather than routing the reason through a generic exception
        # handler: "could not save session snapshot: ..." reads like a failure,
        # and not saving here is the intended behaviour.
        if not dpapi.available():
            log.warning(
                "not saving the session snapshot: DPAPI is unavailable, so it "
                "would be written in cleartext -- that file holds a live cookie, "
                "and anything that can read it can read the LSM portal"
            )
            return
        SESSION_FILE.write_bytes(dpapi.protect(blob))
        log.info("session snapshot saved (%d cookies, %d shibboleth)",
                 len(cookies), len(shib))
    except Exception as exc:
        log.warning("could not save session snapshot: %s", exc)


def _load_cookies() -> list[dict[str, Any]]:
    if not SESSION_FILE.exists():
        return []
    try:
        raw = dpapi.unprotect(SESSION_FILE.read_bytes())
        data = json.loads(raw.decode("utf-8"))
        return data.get("cookies") or []
    except OSError as exc:
        log.warning("session snapshot undecryptable (%s) — ignoring", exc)
        return []
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("session snapshot corrupt (%s) — ignoring", exc)
        return []


def _restore_cookies(ctx: Any) -> None:
    """
    Re-inject the snapshot only when the profile has no live Shibboleth
    cookie. Adding cookies unconditionally would overwrite a good session
    with a stale one.
    """
    try:
        existing = ctx.cookies()
    except Exception:
        return
    if any("shibsession" in c.get("name", "").lower() for c in existing):
        return

    cookies = _load_cookies()
    if not cookies:
        return

    restored = 0
    for c in cookies:
        try:
            ctx.add_cookies([c])
            restored += 1
        except Exception:
            continue  # expired cookies are rejected; that is fine
    if restored:
        log.info("re-injected %d/%d cookies from snapshot", restored, len(cookies))


def clear() -> None:
    """Forget the saved cookie snapshot.

    This is not a sign-out on its own, and must not be wired to one: the live
    session is in the Chromium profile, not in this file. See logout().
    """
    with _lock:
        SESSION_FILE.unlink(missing_ok=True)
        log.info("session snapshot cleared")


def logout() -> SessionState:
    """Sign out for real, then say what is actually left.

    clear() on its own does not sign anyone out. It deletes session.bin, which
    is the DPAPI cookie *snapshot* — a copy kept only to re-inject the session
    after a hard shutdown. The live session is in the Chromium profile's own
    cookie store, because that is where the login put it. So an endpoint that
    called clear() and reported "Session cleared" would have left the user
    signed in, and the next probe would have agreed with the cookie rather
    than with the message. That is the defect this function exists to avoid.

    The cookies are cleared through Chromium instead of by deleting the profile
    directory: Chromium keeps its own store consistent, and a partly deleted
    profile is worse than an intact one. restore=False because this is the one
    caller that must not put the snapshot back.

    The return value is a fresh probe, not an assumption. If the cookies
    survive the clear, this reports the session as still live: a sign-out that
    failed honestly is worth more than one that claims success.
    """
    clear()
    try:
        with browser(headless=True, restore=False) as ctx:
            ctx.clear_cookies()
    except Exception:
        log.exception("could not clear the profile's cookies")
    return probe()


# ── Probing ──────────────────────────────────────────────────────────────

def probe(headless: bool = True) -> SessionState:
    """
    Ask LSM whether we are still authenticated.

    Cheap: one navigation to the APEX entry point. If Shibboleth has
    expired we land on weblogin/idpz instead of an APEX page.
    """
    with _lock:
        try:
            with browser(headless=headless) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto(APEX_AUTH_URL, wait_until="domcontentloaded",
                              timeout=NAVIGATE_TIMEOUT_MS)
                except Exception as exc:
                    return SessionState("error", message=f"navigation failed: {exc}")

                page.wait_for_timeout(1500)
                url = page.url

                if is_login_url(url):
                    log.info("session expired — Shibboleth wants a login")
                    return SessionState("expired", message="Shibboleth session expired")

                m = SESSION_URL_RE.search(url)
                if not m:
                    # Some APEX hops bounce to the app root first; retry once
                    try:
                        page.goto(f"https://{LSM_HOST}/ords/f?p={APEX_APP_ID}:1",
                                  wait_until="domcontentloaded",
                                  timeout=NAVIGATE_TIMEOUT_MS)
                        page.wait_for_timeout(1200)
                        url = page.url
                        m = SESSION_URL_RE.search(url)
                    except Exception:
                        pass

                if is_login_url(url):
                    return SessionState("expired", message="Shibboleth session expired")

                # Shibboleth let the browser through (no login URL above), and
                # LSM's own sign-in page or an authorization error is what
                # came back: the account is known to UofT but not to LSM.
                if not m and access_denied(page):
                    log.info("session reached LSM but the account has no "
                             "access (%s)", url[:120])
                    return SessionState("no_access", message=NO_ACCESS_MESSAGE)

                if not m:
                    return SessionState(
                        "error",
                        message=f"unexpected landing page: {url[:120]}",
                    )

                _save_cookies(ctx)
                log.info("session alive (id=%s)", m.group(1))
                return SessionState("ok", session_id=m.group(1))
        except Exception as exc:
            log.exception("probe failed")
            return SessionState("error", message=friendly_error(exc))


def heartbeat() -> "SessionState":
    """Lightweight keep-warm ping. Never opens a window.

    Returns the probe's own state rather than a bool: an expired session
    and an unreachable LSM both fail this, and the caller shows the
    difference — a collapsed ok/not-ok told the user to sign in when
    signing in could not have helped.
    """
    state = probe(headless=True)
    if state.ok:
        log.info("heartbeat ok")
    else:
        log.info("heartbeat: %s (%s)", state.state, state.message)
    return state


# ── Interactive login ────────────────────────────────────────────────────

def _wait_unless_closed(page: Any, ms: int) -> bool:
    """Wait on the sign-in window; False if the person closed it meanwhile.

    Playwright answers a closed window with "Target page, context or browser
    has been closed". Left to the caller's handler, that raw text became the
    sign-in result and the probe that could have found a perfectly good
    session never ran. Any other failure is still a failure and is raised.
    """
    try:
        page.wait_for_timeout(ms)
        return True
    except Exception as exc:
        try:
            closed = page.is_closed()
        except Exception:
            closed = True
        if closed or "has been closed" in str(exc):
            return False
        raise


def interactive_login(on_status=None) -> SessionState:
    """
    Open a visible Chromium window and let the user authenticate.

    We do not type credentials ourselves — the user drives, approves Duo,
    and we detect arrival at LSM. This keeps the password out of the app
    entirely and sidesteps MFA automation, which is both fragile and
    against the spirit of the second factor.
    """
    def say(msg: str) -> None:
        log.info(msg)
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    with _lock:
        say("Opening a login window — please sign in with your UTORid.")
        try:
            with browser(headless=False, restore=False) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(LSM_PORTAL_URL, wait_until="domcontentloaded",
                          timeout=NAVIGATE_TIMEOUT_MS)

                say("Waiting for you to finish signing in (and approve Duo)…")

                # Do NOT use wait_for_url here. The page we just navigated to
                # is already on the LSM host and is not a login URL, so a
                # predicate like "LSM_HOST in url and not is_login_url(url)"
                # matches the *starting* URL and resolves on the first poll —
                # the window declared success three seconds in, saved a
                # snapshot with zero Shibboleth cookies in it, and closed
                # before there was any chance to approve Duo.
                #
                # Watch the URL instead, and only believe a landing once the
                # page has actually been bounced through the IdP, or has come
                # back carrying a different APEX session id than we started
                # with.
                start_sid = _session_id(page.url)
                deadline = time.monotonic() + MFA_WAIT_MS / 1000.0
                saw_login = False
                stable_since = None
                stable_url = None
                signed_in = False
                closed_early = False

                while time.monotonic() < deadline:
                    if page.is_closed():
                        # Closing the window is a perfectly normal way to
                        # finish — the session may well be signed in. Do not
                        # call it a failure here; fall out and let the probe
                        # below decide.
                        closed_early = True
                        break
                    url = page.url
                    step = login_progress(url, start_sid, saw_login)
                    if step == "login":
                        saw_login = True
                        stable_since = None
                    elif step == "signed_in":
                        signed_in = True
                        break
                    elif LSM_HOST in url and _session_id(url):
                        # Waiting on an APEX page with no IdP bounce at all:
                        # the profile's cookie is probably still good and the
                        # app loaded straight away. Require that same URL to
                        # sit still before believing that, or this is the same
                        # false positive in another shape. "Same URL" is the
                        # point: the clock restarts on every navigation, so
                        # wandering about the LSM host does not add up to 8 s.
                        # The portal landing page carries no session id and
                        # proves nothing, so it never starts the clock.
                        if stable_since is None or url != stable_url:
                            stable_since = time.monotonic()
                            stable_url = url
                        elif time.monotonic() - stable_since > ALREADY_SIGNED_IN_S:
                            signed_in = True
                            break
                    else:
                        stable_since = None
                    if not _wait_unless_closed(page, 1000):
                        # Closed mid-wait, which is where the loop spends
                        # nearly all its time. Same as the check above.
                        closed_early = True
                        break

                if signed_in:
                    # Give the portal a moment to finish writing cookies
                    # before the context goes away. Closing the window now
                    # is fine: save what is there and let the probe judge.
                    _wait_unless_closed(page, 2000)
                    _save_cookies(ctx)
        except Exception as exc:
            log.exception("interactive login failed")
            return SessionState("error", message=friendly_error(exc))

    # The window is closed or gone. Prove the session actually works rather
    # than trusting the URL it ended on — that is what let the earlier false
    # positive report success while the very next call found it expired.
    state = probe(headless=True)
    if state.ok:
        log.info("interactive login verified by probe")
        return SessionState("ok", session_id=state.session_id, message="Signed in")
    if state.state in ("no_access", "error"):
        # Not "try again": the probe knows why, and for these two another
        # sign-in cannot change the answer.
        return state

    log.info("login window ended without a usable session (%s, closed_early=%s)",
             state.state, closed_early)
    if closed_early:
        message = ("The sign-in window was closed before sign-in finished.")
    elif signed_in:
        # We thought we landed, but LSM disagrees — say so plainly instead of
        # repeating "try again" and letting the user wonder what happened.
        message = ("The browser reached LSM but the session did not stick. "
                   "Try again.")
    else:
        message = "Timed out waiting for sign-in. Try again."
    return SessionState("expired", message=message)


# (ensure_session used to live here: probe-then-optionally-login. Nothing
# ever called it — the scheduler probes and signs in as separate, deliberate
# actions, because signing in needs a human for Duo. Removed rather than
# kept as an unused "should" the next reader has to check on.)
