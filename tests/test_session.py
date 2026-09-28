"""
Sign-in window tests. Run directly (no pytest needed):

    python tests/test_session.py

These exercise login_progress(), which decides when the login window is
done. It is pure so the decision can be tested without opening a browser,
which matters here because the bug it guards against looks like success:
the window reported "Signed in", saved a snapshot, and closed three
seconds in — before there was any chance to approve Duo.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# app.session logs through app.config, which opens the log file as soon as it
# is imported. Point the data directory at a scratch location first, so the
# snapshot-guard case does not write "not saving session snapshot" into the
# real data/app.log — that warning in the live log should mean the app hit it.
os.environ["LSM_DATA_DIR"] = tempfile.mkdtemp(prefix="lsm-session-")

from app.config import LSM_PORTAL_URL  # noqa: E402
from app.session import login_progress  # noqa: E402

PASS, FAIL = 0, 0


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


# The URL the login window navigates to on open. Note it carries no APEX
# session id — it is the portal landing page, not an f?p= URL — but it *is*
# on the LSM host and is *not* a login URL, so the old predicate
# "LSM_HOST in url and not is_login_url(url)" evaluated true against the
# very first page and the window declared success.
PORTAL = LSM_PORTAL_URL
SID = "17828197829093"                      # an APEX session id from the log
APEX = f"https://lsm.utoronto.ca/ords/f?p=143:51:{SID}:::::"
APEX_NEW = "https://lsm.utoronto.ca/ords/f?p=143:51:99887766554433:::::"
WEBLOGIN = "https://weblogin.utoronto.ca/idp/profile/SAML2/Redirect/SSO"
IDPZ = "https://idpz.utorauth.utoronto.ca/idp/profile/SAML2/Redirect/SSO"


def test_portal_is_not_arrival() -> None:
    print("\nthe starting page must not read as a completed sign-in")
    # THE REGRESSION. The portal landing page, nothing seen yet: the old
    # code called this arrival and closed the window 3 seconds in.
    check("portal, nothing seen yet",
          login_progress(PORTAL, None, False), "waiting")
    # Still not arrival on the second and third poll either — the stability
    # timer, not this function, is what eventually accepts a warm cookie.
    check("portal, still nothing seen",
          login_progress(PORTAL, None, False), "waiting")
    # A portal URL that does carry a session id is still not arrival, as
    # long as it is the one we started from.
    check("same sid we started with",
          login_progress(APEX, SID, False), "waiting")


def test_login_page() -> None:
    print("\nIdP pages always mean 'not signed in yet'")
    check("weblogin", login_progress(WEBLOGIN, None, False), "login")
    check("idpz", login_progress(IDPZ, None, False), "login")
    check("still a login page after one was seen",
          login_progress(WEBLOGIN, None, True), "login")
    # Even if it somehow carried a session id, a login URL wins.
    check("login url with a sid",
          login_progress(WEBLOGIN + "?f?p=143:51:12345", None, True), "login")


def test_arrival() -> None:
    print("\nreal arrivals")
    # The path the user actually takes: bounced through the IdP, then
    # dropped back on the portal page they started from.
    check("back on the portal after the IdP",
          login_progress(PORTAL, None, True), "signed_in")
    # Came back on an APEX page carrying a new session id.
    check("new session id",
          login_progress(APEX_NEW, SID, False), "signed_in")
    # No session id to compare against, but we watched the IdP happen.
    check("apex page after the IdP",
          login_progress(APEX, None, True), "signed_in")


def test_junk() -> None:
    print("\nanything else is simply not there yet")
    check("about:blank", login_progress("about:blank", None, False), "waiting")
    check("empty", login_progress("", None, False), "waiting")
    check("none", login_progress(None, None, False), "waiting")
    check("some other host", login_progress("https://example.com/", None, False),
          "waiting")


def test_snapshot_guard() -> None:
    print("\na snapshot with no Shibboleth cookie is never written")
    import tempfile
    from pathlib import Path as _Path

    from app import session

    class FakeCtx:
        def __init__(self, cookies):
            self._c = cookies

        def cookies(self):
            return self._c

    def cookie(name):
        return {"name": name, "value": "x", "domain": "lsm.utoronto.ca", "path": "/"}

    real = session.SESSION_FILE
    with tempfile.TemporaryDirectory() as tmp:
        target = _Path(tmp) / "session.bin"
        session.SESSION_FILE = target
        try:
            # The fingerprint of the reported bug: six cookies, none of them
            # Shibboleth. Writing this would clobber a working snapshot with
            # one that can never restore a session.
            session._save_cookies(FakeCtx([cookie("ORA_WWV_APP_143"),
                                           cookie("JSESSIONID")]))
            check("no shibboleth -> nothing written", target.exists(), False)

            # But a real one must still be saved.
            session._save_cookies(FakeCtx([cookie("_shibsession_6465666"),
                                           cookie("JSESSIONID")]))
            check("shibboleth present -> written", target.exists(), True)
        finally:
            session.SESSION_FILE = real


def test_the_session_cookie_is_never_written_in_cleartext() -> None:
    """No cipher must mean no snapshot -- never a snapshot in the clear.

    session.bin holds a Shibboleth cookie, which is a credential: anything
    holding it reads the LSM portal with no MFA. `protect` used to return its
    input unchanged when DPAPI was missing, so the cookie went to disk
    readable -- and nothing distinguished that from success, because
    `_save_cookies` logs "session snapshot saved (N cookies, N shibboleth)"
    either way. A cleartext credential was indistinguishable from an encrypted
    one in the log, in the UI, and in any backup that picked the file up.

    The flag is patched rather than the platform faked. On this machine the
    only way that branch is reached is sys.platform not reporting Windows --
    which is exactly the case that would otherwise ship silently, so it is the
    case worth pinning.
    """
    print("\na missing cipher means no snapshot, not a cleartext one")
    import logging
    import tempfile
    from pathlib import Path as _Path

    from app import dpapi, session

    class FakeCtx:
        def __init__(self, cookies):
            self._c = cookies

        def cookies(self):
            return self._c

    shib = {"name": "_shibsession_6465666", "value": "live-credential",
            "domain": "lsm.utoronto.ca", "path": "/"}

    told: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            told.append(record.getMessage())

    capture = Capture()
    logger = logging.getLogger("lsm")
    logger.addHandler(capture)

    real_flag = dpapi._IS_WINDOWS
    real_file = session.SESSION_FILE
    with tempfile.TemporaryDirectory() as tmp:
        target = _Path(tmp) / "session.bin"
        session.SESSION_FILE = target
        dpapi._IS_WINDOWS = False
        try:
            # The primitive refuses outright...
            refused = False
            try:
                dpapi.protect(b"a live cookie")
            except OSError:
                refused = True
            check("protect refuses instead of passing the bytes through",
                  refused, True)

            # ...and the caller turns that into no file at all, which is the
            # claim that matters: whatever else happens, a live credential
            # does not reach the disk.
            told.clear()
            session._save_cookies(FakeCtx([shib]))
            check("so the snapshot is not written", target.exists(), False)
            check("...and the reason reaches the log",
                  any("DPAPI is unavailable" in m for m in told), True)
            check("...naming cleartext, not a transient failure",
                  any("cleartext" in m for m in told), True)

            # Reading is deliberately the lenient half. The file is already on
            # disk in the clear, so refusing to read it would cost the session
            # and protect nothing -- but it must still say so, because signing
            # out is the only thing that clears it and the user is the only
            # one who can do that.
            told.clear()
            check("unprotect still returns the bytes it was given",
                  dpapi.unprotect(b'{"cookies":[]}'), b'{"cookies":[]}')
            check("...and warns that it did not decrypt them",
                  any("cleartext" in m for m in told), True)
        finally:
            dpapi._IS_WINDOWS = real_flag
            session.SESSION_FILE = real_file
            logger.removeHandler(capture)


def test_logout_clears_and_reports() -> None:
    """Signing out has to clear the live session, and admit it when it cannot.

    clear() deletes session.bin, which is only the DPAPI cookie *snapshot* — a
    copy kept to re-inject the session after a hard shutdown. The live session
    is in the Chromium profile's cookie store, because that is where the login
    put it. So an endpoint that called clear() and answered "Session cleared"
    would report a sign-out that never happened, and the next probe would have
    agreed with the cookie rather than with the message. That is what this
    covers: the order (snapshot, then cookies, then a real probe), that the
    context is opened without re-injecting the snapshot, and that a probe which
    still finds a session is reported as one.

    No browser and no network: browser() and probe() are replaced.
    """
    print("\nlogging out")

    from contextlib import contextmanager

    from app import session

    class FakeCtx:
        def __init__(self) -> None:
            self.cleared = 0

        def clear_cookies(self) -> None:
            self.cleared += 1

    real_file = session.SESSION_FILE
    real_browser = session.browser
    real_probe = session.probe
    opened: list[dict] = []
    ctx = FakeCtx()

    @contextmanager
    def fake_browser(**kwargs):
        opened.append(kwargs)
        yield ctx

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "session.bin"
        snapshot.write_bytes(b"a DPAPI blob")
        session.SESSION_FILE = snapshot
        session.browser = fake_browser
        try:
            expired = session.SessionState("expired", message="wants a login")
            session.probe = lambda headless=True: expired

            got = session.logout()

            check("logout clears the saved snapshot", snapshot.exists(), False)
            check("logout clears the profile's cookies", ctx.cleared, 1)
            check("...in a context that does not restore them again",
                  bool(opened) and opened[0].get("restore"), False)
            check("logout reports what the probe measured, not what it hoped",
                  got, expired)

            # The honest failure, and the reason the probe is in here at all:
            # the cookies survived, so the session is still live. Returning
            # anything but that state would be the original defect.
            alive = session.SessionState("ok", message="")
            session.probe = lambda headless=True: alive
            check("a session the probe still finds is reported as live",
                  session.logout().state, "ok")
        finally:
            session.SESSION_FILE = real_file
            session.browser = real_browser
            session.probe = real_probe


def test_the_browser_is_edge_when_the_machine_has_it() -> None:
    """Edge when found, Chromium otherwise, and each in its own profile.

    Every managed Windows PC has Edge, so driving it spares each new user a
    ~150 MB Chromium download through Playwright's CDN — the install step most
    likely to fail on a managed machine. The detection must look where
    Playwright looks, honour LSM_BROWSER=chromium, and the launch must carry
    the channel, or "found Edge" would still launch Chromium.
    """
    print("\nbrowser choice")

    from app import config, session

    fake_root = Path(tempfile.mkdtemp(prefix="lsm-edge-"))
    exe = fake_root / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")

    saved = {k: os.environ.get(k) for k in
             ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "LSM_BROWSER")}
    try:
        for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LSM_BROWSER"):
            os.environ.pop(k, None)
        os.environ["LOCALAPPDATA"] = str(fake_root)
        check("Edge is found where Playwright looks", config._find_edge(), exe)
        os.environ["LSM_BROWSER"] = "chromium"
        check("LSM_BROWSER=chromium forces the fallback", config._find_edge(), None)
        os.environ.pop("LSM_BROWSER")
        os.environ["LOCALAPPDATA"] = str(fake_root / "nowhere")
        check("no Edge anywhere means Chromium", config._find_edge(), None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # The launch itself: the channel reaches Playwright, and only when set.
    seen: list[dict] = []

    class FakeCtx:
        pages: list = []

        def cookies(self):
            return []

        def close(self):
            pass

    class FakeChromium:
        def launch_persistent_context(self, **kw):
            seen.append(kw)
            return FakeCtx()

    class FakeDriver:
        chromium = FakeChromium()

    real = {"driver": session._driver, "channel": session.BROWSER_CHANNEL}
    try:
        session._driver = lambda: FakeDriver()
        session.BROWSER_CHANNEL = "msedge"
        with session.browser(restore=False):
            pass
        check("an Edge launch names the channel", seen[-1].get("channel"), "msedge")
        session.BROWSER_CHANNEL = None
        with session.browser(restore=False):
            pass
        ok("a Chromium launch names none", "channel" not in seen[-1])
    finally:
        session._driver = real["driver"]
        session.BROWSER_CHANNEL = real["channel"]


def test_install_browser_downloads_nothing_when_edge_is_there() -> None:
    """The installer's browser step must succeed quietly on an Edge machine."""
    print("\ninstall-browser with Edge")

    import subprocess

    from app import main as app_main

    ran: list = []
    real = {"edge": app_main.EDGE_EXE, "run": subprocess.run}
    try:
        app_main.EDGE_EXE = Path(r"C:\fake\msedge.exe")
        subprocess.run = lambda *a, **k: (  # type: ignore[assignment]
            ran.append(a), type("Done", (), {"returncode": 0})())[1]
        check("it succeeds", app_main.run_install_browser(), 0)
        check("...without running the Playwright download", ran, [])
    finally:
        app_main.EDGE_EXE = real["edge"]
        subprocess.run = real["run"]  # type: ignore[assignment]


def test_a_refused_account_is_no_access_not_expired() -> None:
    """Shibboleth let the browser through and LSM refused it: say so."""
    print("\nno-access probe")

    from contextlib import contextmanager

    from app import session

    class FakeLocator:
        def __init__(self, text):
            self.text = text

        def inner_text(self, **kw):
            if self.text is None:
                raise RuntimeError("detached")
            return self.text

    class FakePage:
        def __init__(self, url, text=""):
            self.url, self.text = url, text

        def goto(self, url, **kw):
            pass

        def wait_for_timeout(self, ms):
            pass

        def locator(self, sel):
            return FakeLocator(self.text)

    desk = "https://lsm.utoronto.ca/ords/f?p=143:LOGIN_DESKTOP:1234:::::"
    ok("LSM's own sign-in page after Shibboleth is a refusal",
       session.access_denied(FakePage(desk)))
    ok("APEX's authorization error is a refusal",
       session.access_denied(FakePage("https://lsm.utoronto.ca/ords/f?p=143:51",
                                       "Access denied by Page security check")))
    ok("an ordinary page is not",
       not session.access_denied(FakePage("https://lsm.utoronto.ca/x", "Report")))
    ok("a page whose text cannot be read is not",
       not session.access_denied(FakePage("https://lsm.utoronto.ca/x", None)))
    check("a missing browser gets words a person can act on",
          session.friendly_error(RuntimeError(
              "BrowserType.launch: Executable doesn't exist at C:\\x")),
          session.NO_BROWSER_MESSAGE)

    class FakeCtx:
        def __init__(self, page):
            self.pages = [page]

        def cookies(self):
            return []

    real = session.browser
    try:
        @contextmanager
        def fake_browser(headless=True, restore=True):
            yield FakeCtx(FakePage(desk))

        session.browser = fake_browser
        state = session.probe()
        check("the probe reports no_access", state.state, "no_access")
        ok("...with the message", "LSM administrator" in state.message)
    finally:
        session.browser = real


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — sign-in window tests")
    print("=" * 60)

    test_portal_is_not_arrival()
    test_login_page()
    test_arrival()
    test_junk()
    test_snapshot_guard()
    test_the_session_cookie_is_never_written_in_cleartext()
    test_logout_clears_and_reports()
    test_the_browser_is_edge_when_the_machine_has_it()
    test_install_browser_downloads_nothing_when_edge_is_there()
    test_a_refused_account_is_no_access_not_expired()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
