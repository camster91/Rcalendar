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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")


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


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — sign-in window tests")
    print("=" * 60)

    test_portal_is_not_arrival()
    test_login_page()
    test_arrival()
    test_junk()
    test_snapshot_guard()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
