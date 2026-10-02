"""
End-to-end smoke test: storage round-trip and its migrations, the
cross-site refusal, and every web endpoint.

Uses Flask's test client and a stub orchestrator, so no browser or LSM
session is involved. Run directly:

    python tests/test_smoke.py
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit
import sqlite3
import threading
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the data directory at a scratch location *before* app.config is
# imported, so the test never touches real scraped data or the live
# browser profile.
import os  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="lsm-test-")
os.environ["LSM_DATA_DIR"] = _tmp

from app import store, updater  # noqa: E402
from app.config import APP_VERSION, DATA_DIR, DB_PATH  # noqa: E402
from app.server import create_app  # noqa: E402

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


class StubOrch:
    """Stands in for the Playwright worker."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self) -> dict:
        return {
            "session": "ok", "session_message": "", "busy": False,
            "busy_action": "", "progress": "",
            "last_scrape": datetime.now().isoformat(),
            "last_scrape_message": "42 bookings",
            # APP_VERSION, not a literal: a version bump must not turn this
            # suite red for a reason unrelated to the change being made.
            "update": {"state": "idle", "current_version": APP_VERSION,
                       "latest_version": "", "notes_url": "", "message": "",
                       "last_check": None, "progress": "", "token_set": False,
                       "staged": ""},
        }

    def is_busy(self) -> bool:
        return False

    def request_scrape(self, interactive: bool = False) -> None:
        self.calls.append("scrape")

    def request_login(self) -> None:
        self.calls.append("login")

    def request_logout(self) -> None:
        self.calls.append("logout")

    def request_probe(self) -> None:
        self.calls.append("probe")

    def request_update_check(self) -> None:
        self.calls.append("update_check")

    def request_update_install(self) -> None:
        self.calls.append("update_install")

    def skip_update(self, tag: str) -> None:
        self.calls.append("update_skip:" + tag)


def sample_events() -> list[dict]:
    today = date.today()
    return [
        {
            "title": "RSM6307 Marketing",
            "room": "142",
            "start": datetime.combine(today, datetime.min.time()).replace(hour=9).isoformat(),
            "end": datetime.combine(today, datetime.min.time()).replace(hour=12).isoformat(),
            "description": "Lecture", "class_code": "A", "cancelled": False,
            "location": "RT 142",
        },
        {
            "title": "CIBC Info Session",
            "room": "L1060",
            "start": datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(hour=14).isoformat(),
            "end": datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(hour=16).isoformat(),
            "description": "Recruiting", "class_code": "S", "cancelled": False,
            "location": "RT L1060",
        },
        {
            "title": "Cancelled Thing",
            "room": "127",
            "start": datetime.combine(today, datetime.min.time()).replace(hour=10).isoformat(),
            "end": None, "description": "", "class_code": "A", "cancelled": True,
            "location": "RT 127",
        },
    ]


def test_store() -> None:
    print("\nstorage")
    store.init_db()

    today = date.today()
    d_from = (today - timedelta(days=30)).strftime("%d/%m/%Y")
    d_to = (today + timedelta(days=30)).strftime("%d/%m/%Y")

    n = store.replace_events(sample_events(), d_from, d_to)
    check("rows written", n, 3)

    # Cancelled bookings are stored but filtered from the default read.
    check("cancelled hidden by default", len(store.get_events()), 2)
    check("cancelled visible on request",
          len(store.get_events(include_cancelled=True)), 3)

    # Re-running the same scrape must not duplicate.
    store.replace_events(sample_events(), d_from, d_to)
    check("idempotent re-scrape", len(store.get_events(include_cancelled=True)), 3)

    # A scrape that no longer reports a booking should remove it.
    store.replace_events(sample_events()[:1], d_from, d_to)
    check("vanished booking pruned",
          len(store.get_events(include_cancelled=True)), 1)

    check("filter by room", len(store.get_events(room="142")), 1)
    check("filter by query", len(store.get_events(q="Marketing")), 1)
    check("query miss", len(store.get_events(q="nonexistent-xyz")), 0)

    check("filter by exact date",
          len(store.get_events(date=today.isoformat())), 1)

    stats = store.stats()
    check("stats total", stats["total_events"], 1)
    check("stats rooms", stats["rooms"], 1)

    store.set_kv("hello", {"a": 1})
    check("kv round-trip", store.get_kv("hello"), {"a": 1})

    check("run starts", store.start_run("manual") > 0, True)
    store.finish_run(1, "ok", events_count=7)
    last = store.last_run()
    check("last run recorded", last["status"], "ok")
    check("last run count", last["events_count"], 7)

    # A booking's identity is the change feed's pairing key, and has to tell
    # apart two blocks that share one name and start — `end` is the one field
    # that distinguishes them. (Covered here since the .ics export that also
    # asserted it was removed, but this is the store's invariant, not its.)
    a = {"title": "Sunday service block", "room": "127",
         "start": "2026-03-15T00:00:00", "end": "2026-03-16T00:00:00"}
    b = {"title": "Sunday service block", "room": "127",
         "start": "2026-03-15T00:00:00", "end": "2026-03-15T02:00:00"}
    ok("end time is part of a booking's identity",
       store.event_uid(a) != store.event_uid(b))


# The database the rest of the suite uses, so a test that has to build its own
# can put this back afterwards.
SHARED_DB = str(DB_PATH)

# The two tables as they looked before all_day existed. Written by hand rather
# than by importing a retired version of SCHEMA, because the point is a shape
# this code no longer produces.
OLD_EVENTS = """
CREATE TABLE events (
    uid TEXT PRIMARY KEY, title TEXT NOT NULL, room TEXT NOT NULL,
    start_iso TEXT, end_iso TEXT, date TEXT, description TEXT,
    class_code TEXT, cancelled INTEGER NOT NULL DEFAULT 0, scraped_at TEXT NOT NULL
);
"""
OLD_CHANGES = """
CREATE TABLE changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, trigger TEXT NOT NULL,
    kind TEXT NOT NULL, uid TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    room TEXT NOT NULL DEFAULT '', start_iso TEXT, end_iso TEXT, date TEXT,
    description TEXT NOT NULL DEFAULT '', class_code TEXT NOT NULL DEFAULT '',
    detected_at TEXT NOT NULL
);
"""


def use_db(path) -> None:
    """Point store at a different database file, dropping the cached connection."""
    store._local.conn = None
    store.DB_PATH = str(path)


def use_shared_db() -> None:
    use_db(SHARED_DB)


def columns(table: str) -> set:
    return {row["name"]
            for row in store._conn().execute(f"PRAGMA table_info({table})")}


def test_migration() -> None:
    """A database from before all_day existed has to come back usable.

    CREATE TABLE IF NOT EXISTS does nothing when the table is already there, so
    a new column is only ever added by the explicit retrofit in _migrate. Miss
    it and an older database keeps its old shape, and every INSERT that names
    the column fails — which is every scrape, not only the ones that found a
    change, so the app looks broken rather than merely incomplete.

    This runs against its own database files, because the rest of the suite is
    using the shared one.
    """
    print("\nmigration")

    def fresh(name: str) -> Path:
        d = Path(tempfile.mkdtemp(prefix=f"lsm-mig-{name}-"))
        return d / "calendar.db"

    # A brand-new database. This also pins the order inside init_db: _migrate
    # reads PRAGMA table_info, which returns nothing for a table that does not
    # exist, so running it before executescript would try to ALTER a missing
    # table and fail the whole of init_db.
    use_db(fresh("new"))
    store.init_db()
    check("a new database has events.all_day", "all_day" in columns("events"), True)
    check("a new database has changes.all_day",
          "all_day" in columns("changes"), True)

    # Running it again must be a no-op rather than an error: every launch calls
    # init_db, and "duplicate column name" would break the second one.
    try:
        store.init_db()
        check("a second init_db is a no-op", True, True)
    except Exception as exc:
        check("a second init_db is a no-op", f"{type(exc).__name__}: {exc}", True)

    # The old shape, with a row in each table so the retrofit is shown to keep
    # the data rather than merely to add a column.
    db = fresh("old")
    conn = sqlite3.connect(db)
    conn.executescript(OLD_EVENTS)
    conn.executescript(OLD_CHANGES)
    conn.execute(
        "INSERT INTO events (uid,title,room,start_iso,end_iso,date,description,"
        "class_code,cancelled,scraped_at) VALUES "
        "('u1','Old Booking','142','2026-03-10T09:00:00','2026-03-10T12:00:00',"
        "'2026-03-10','','',0,'2026-03-01T06:00:00')"
    )
    conn.execute(
        "INSERT INTO changes (trigger,kind,uid,title,room,start_iso,end_iso,date,"
        "detected_at) VALUES "
        "('manual','added','u1','Old Booking','142','2026-03-10T09:00:00',"
        "'2026-03-10T12:00:00','2026-03-10','2026-03-01T06:00:00')"
    )
    conn.commit()
    conn.close()

    use_db(db)
    store.init_db()
    check("an old database gains events.all_day",
          "all_day" in columns("events"), True)
    check("an old database gains changes.all_day",
          "all_day" in columns("changes"), True)

    # The rows are still there, and the new column has its default rather than
    # NULL — a NULL would read as "not all day" only by luck.
    kept = store.get_events(include_cancelled=True)
    check("the retrofit keeps the rows", len(kept), 1)
    check("...and the new column defaults to 0", kept[0]["all_day"], 0)
    check("...and the feed's rows survive too", len(store.get_changes()), 1)

    # A row written after the retrofit must round-trip the new column, which is
    # the thing the retrofit exists to make possible.
    store.replace_events(
        [dict(sample_events()[0], all_day=True)], "01/03/2026", "31/03/2026"
    )
    re_read = {e["title"]: e for e in store.get_events(include_cancelled=True)}
    check("an all-day row round-trips after the retrofit",
          re_read["RSM6307 Marketing"]["all_day"], 1)

    # The two retrofits are independent checks, so a database that has one
    # column and not the other — which a partly-migrated install would — has to
    # get only what it is missing.
    db = fresh("half")
    conn = sqlite3.connect(db)
    conn.executescript(OLD_EVENTS)
    conn.executescript(OLD_CHANGES)
    conn.execute("ALTER TABLE events ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()

    use_db(db)
    store.init_db()
    check("a half-migrated database still gains changes.all_day",
          "all_day" in columns("changes"), True)

    use_shared_db()


def test_web() -> None:
    print("\nweb API")
    orch = StubOrch()
    app = create_app(orch)
    client = app.test_client()

    pages = [("/", b"Rotman Room Bookings"), ("/list", b"Rotman Room Bookings")]
    for path, needle in pages:
        r = client.get(path)
        check(f"GET {path} status", r.status_code, 200)
        ok(f"GET {path} renders", needle in r.data)

    r = client.get("/api/bootstrap")
    check("bootstrap status", r.status_code, 200)
    data = r.get_json()
    ok("bootstrap has events key", "events" in data)
    ok("bootstrap has rooms key", "rooms" in data)
    ok("bootstrap has groups key", "groups" in data)
    ok("bootstrap rooms non-empty", len(data["rooms"]) > 0)
    ok("bootstrap room_meta present", "142" in data.get("room_meta", {}))

    meta = data["room_meta"].get("142", {})
    check("room friendly name", meta.get("display"), "Room 142")
    check("room floor", meta.get("floor"), "Ground Floor")
    check("room capacity", meta.get("capacity"), 72)
    check("room panopto flag", meta.get("panopto"), True)

    r = client.get("/api/status")
    st = r.get_json()
    check("status session", st.get("session"), "ok")
    ok("status has human timestamp", st.get("last_scrape_human") not in (None, ""))
    ok("status has date range", "date_range" in st)

    r = client.get("/api/autocomplete?q=142")
    ok("autocomplete finds room", "142" in r.get_json()["rooms"])
    r = client.get("/api/autocomplete?q=")
    check("autocomplete empty query", r.get_json()["rooms"], [])

    r = client.get("/api/today")
    check("today endpoint", r.status_code, 200)
    ok("today has rooms list", isinstance(r.get_json()["rooms"], list))

    r = client.get("/api/room-groups")
    ok("groups endpoint", isinstance(r.get_json(), dict))

    r = client.post("/api/scrape")
    check("scrape accepted", r.status_code, 200)
    check("scrape enqueued", orch.calls, ["scrape"])

    r = client.post("/api/login")
    check("login enqueued", orch.calls, ["scrape", "login"])

    # Logout is queued like login, because it drives the same Chromium profile
    # the worker owns. The reply says the request was accepted — not that the
    # session is gone, which only the probe the worker runs can say.
    r = client.post("/api/logout")
    check("logout accepted", r.status_code, 200)
    check("...and enqueued for the worker, not done in this thread",
          orch.calls, ["scrape", "login", "logout"])
    check("...and it does not claim the session is already cleared",
          r.get_json().get("status"), "started")

    # The update commands queue like the rest of the control plane: the
    # reply says "started", and the worker is the one that talks to GitHub.
    r = client.post("/api/update/check")
    check("update check accepted", r.status_code, 200)
    check("...and enqueued", orch.calls, ["scrape", "login", "logout",
                                          "update_check"])
    r = client.post("/api/update/install")
    check("update install accepted", r.status_code, 200)
    check("...and enqueued", orch.calls, ["scrape", "login", "logout",
                                          "update_check", "update_install"])

    # Skip is refused when nothing is available: the point of the state is
    # that a skipped version was *offered* to this person.
    r = client.post("/api/update/skip")
    check("skip refused when idle", r.status_code, 400)

    # The status feed carries the update block, whose token field is a
    # boolean and never the value - the token must not exist anywhere a
    # page script can read it.
    st = client.get("/api/status").get_json()
    ok("status has an update block", isinstance(st.get("update"), dict))
    check("update block carries the current version",
          st["update"].get("current_version"), APP_VERSION)
    ok("update block's token field is a bool",
       isinstance(st["update"].get("token_set"), bool))

    r = client.get("/api/nope")
    check("unknown route 404", r.status_code, 404)


def test_cross_site_writes() -> None:
    """Another site's page must not be able to drive this app's control plane.

    Loopback is a reachability boundary, not an authentication one: a form POST
    from a page open elsewhere is not subject to a CORS preflight, so the
    browser will not stop it. What it will do is announce the origin.

    The refusal is asserted *before* the side effect in every case, because a
    refusal that still enqueued the work would read as green while doing the
    damage it claims to prevent.
    """
    print("\ncross-site writes")
    orch = StubOrch()
    client = create_app(orch).test_client()

    evil = {"Origin": "https://evil.example"}

    r = client.post("/api/scrape", headers=evil)
    check("foreign origin is refused", r.status_code, 403)
    check("...and nothing was enqueued", orch.calls, [])

    check("foreign origin refused on login",
          client.post("/api/login", headers=evil).status_code, 403)
    check("foreign origin refused on logout",
          client.post("/api/logout", headers=evil).status_code, 403)

    # The update endpoints belong in that list too, for three different
    # reasons: check/install would offer the app a real installer and end
    # its process, skip would rewrite the update state, and the token write
    # would swap the credential the app authenticates to GitHub with.
    orch.calls.clear()
    check("foreign origin refused on update check",
          client.post("/api/update/check", headers=evil).status_code, 403)
    check("foreign origin refused on update install",
          client.post("/api/update/install", headers=evil).status_code, 403)
    check("...and neither landed", orch.calls, [])

    before_state = store.load_update_state()
    check("foreign origin refused on update skip",
          client.post("/api/update/skip", headers=evil,
                      json={"version": "v99.99.99"}).status_code, 403)
    check("...and the skip state is untouched",
          store.load_update_state(), before_state)

    check("foreign origin refused on the token write",
          client.post("/api/update/token", headers=evil,
                      json={"token": "ghp_evil"}).status_code, 403)
    ok("...and no token was written",
       not updater.TOKEN_FILE.exists())

    # The destructive-to-config case, checked for its effect and not just its
    # status: this endpoint replaces the whole group set.
    before = store.load_groups()
    r = client.post("/api/room-groups", headers=evil, json={"Owned": ["142"]})
    check("foreign origin refused on a config write", r.status_code, 403)
    check("...and the stored groups are untouched", store.load_groups(), before)

    # A sandboxed frame sends the literal "null", which parses to no host.
    check("opaque origin is refused",
          client.post("/api/scrape", headers={"Origin": "null"}).status_code, 403)

    # Referer is the fallback when Origin is absent.
    check("foreign referer is refused",
          client.post("/api/scrape",
                      headers={"Referer": "https://evil.example/x"}).status_code,
          403)

    # The app's own calls keep working, under either spelling of loopback: the
    # page's Origin is whatever host it was opened as, and so is its Host.
    for origin in ("http://127.0.0.1:8765", "http://localhost:8765"):
        orch.calls.clear()
        r = client.post("/api/scrape", base_url=origin,
                        headers={"Origin": origin,
                                 "Sec-Fetch-Site": "same-origin"})
        check(f"same-origin post accepted ({origin})", r.status_code, 200)
        check(f"...and the call landed ({origin})", orch.calls, ["scrape"])

    # Cookies and SameSite ignore the port, so a page on another local server
    # (a dev server, Jupyter) arrives with the key cookie. Only the origin
    # check can tell it apart, and only by the port.
    orch.calls.clear()
    here = "http://127.0.0.1:8765"
    for label, headers in (
            ("another port's origin", {"Origin": "http://127.0.0.1:8888"}),
            ("the other loopback spelling",
             {"Origin": "http://localhost:8765"}),
            ("another port's referer",
             {"Referer": "http://127.0.0.1:8888/tree"}),
            ("Sec-Fetch-Site same-site", {"Sec-Fetch-Site": "same-site"}),
            ("Sec-Fetch-Site cross-site", {"Origin": here,
                                           "Sec-Fetch-Site": "cross-site"})):
        for path in ("/api/logout", "/api/login", "/api/update/install"):
            check(f"{label} refused on {path}",
                  client.post(path, base_url=here, headers=headers).status_code,
                  403)
    check("...and none of them landed", orch.calls, [])
    check("Sec-Fetch-Site none (typed by the person) is allowed",
          client.post("/api/scrape", base_url=here,
                      headers={"Sec-Fetch-Site": "none"}).status_code, 200)

    # And with the key on, which is how the app actually runs: the cookie a
    # page on another port would carry does not get it past the check.
    key = "k" * 32
    keyed = create_app(orch, access_key=key).test_client()
    keyed.set_cookie("lsm_key_8765", key, domain="127.0.0.1")
    orch.calls.clear()
    check("keyed: same-origin post with the cookie accepted",
          keyed.post("/api/scrape", base_url=here,
                     headers={"Origin": here}).status_code, 200)
    check("keyed: another port's post with the cookie refused",
          keyed.post("/api/logout", base_url=here,
                     headers={"Origin": "http://127.0.0.1:8888"}).status_code,
          403)
    check("...and only the same-origin one landed", orch.calls, ["scrape"])

    # An absent origin is allowed on purpose. curl, the packaged --selftest and
    # this test client all send none; treating "unknown" as "hostile" would
    # break all three to close a hole none of them can open, since a real
    # cross-site form or no-cors fetch always arrives carrying one.
    orch.calls.clear()
    check("an absent origin is allowed",
          client.post("/api/scrape").status_code, 200)
    check("...and that call landed", orch.calls, ["scrape"])

    # Reads are untouched: nothing served here is secret, and no GET writes.
    check("a foreign origin may still read",
          client.get("/api/bootstrap", headers=evil).status_code, 200)


def test_login_detection() -> None:
    """
    UofT bounces through several SSO hosts. Matching exact hostnames
    missed idpz.utorauth.utoronto.ca, which made the app report a generic
    error instead of 'expired' — and so never offered the sign-in button.
    """
    print("\nlogin-redirect detection")
    from app.session import is_login_url

    check("weblogin form", is_login_url(
        "https://weblogin.utoronto.ca/idp/profile/SAML2/Redirect/SSO"), True)
    check("utorauth saml handoff", is_login_url(
        "https://idpz.utorauth.utoronto.ca/idp/profile/SAML2/Redirect/SSO"
        "?execution=e2s1"), True)
    check("generic idp host", is_login_url("https://idpz.utoronto.ca/foo"), True)

    check("apex page is not a login", is_login_url(
        "https://lsm.utoronto.ca/ords/f?p=143:51:17828197829093:::::"), False)
    check("portal root is not a login", is_login_url(
        "https://lsm.utoronto.ca/lsm_portal/"), False)
    check("empty is not a login", is_login_url(""), False)
    check("none is not a login", is_login_url(None), False)


def test_icon() -> None:
    """The mark: that its geometry is what it claims, and that the shipped
    .ico is still the drawing in app/icon.py.

    The icon is a picture, so most of it cannot be asserted in a way worth
    having. Two things can, and both are defects that actually happened:

    1. The header band is *clipped* to the body. It used to be drawn as a plain
       white rectangle over a rounded one, which left square white pixels
       outside the curve -- visible as square shoulders on the top corners of
       the tray icon. Asserted as transparency at the pixel just outside the
       corner.

    2. packaging/RotmanLSMCalendar.ico is a committed build output. Change
       app/icon.py and forget to re-run make-icon.py and the exe keeps the old
       mark with nothing to say so. Asserted by rebuilding every frame here and
       comparing.
    """
    print("\nicon")

    from PIL import Image

    from app.icon import paint

    for size in (16, 20, 24, 32, 48, 64, 256):
        img = paint(size)
        check(f"paint({size}) is {size}x{size} RGBA",
              (img.mode, img.size), ("RGBA", (size, size)))

    # The corner defect. At 64px the body's left edge is at x=6 and its top-left
    # corner arc runs from there, so pixels to the left of it, at header height,
    # must be empty -- opaque white there is exactly the old bug. y=14 is inside
    # the header band (40..84 of the 256 grid, i.e. 10..21 at 64px).
    #
    # A tolerance rather than ==0 because the reduction from the supersampled
    # drawing can leave a faint edge value on the pixel beside a hard edge; 255
    # is the failure being caught, and nothing near it.
    body_left_alpha = [paint(64).getpixel((x, 14))[3] for x in (2, 4, 5)]
    ok("the body's rounded corner is not filled in by the header band",
       all(a < 16 for a in body_left_alpha))

    # ...and the header really is there, so the check above cannot pass by the
    # band having been dropped altogether.
    inside = paint(64).getpixel((32, 14))
    check("the header band is white where it should be", inside, (255, 255, 255, 255))

    ico = ROOT / "packaging" / "RotmanLSMCalendar.ico"
    ok("the .ico is committed", ico.is_file())
    if ico.is_file():
        with Image.open(ico) as f:
            frames = sorted(f.ico.sizes())
            expected = sorted({(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)})
            check("it holds every size Windows asks for", frames, expected)
            stale = []
            for n, _ in expected:
                f.size = (n, n)
                f.load()
                if f.convert("RGBA").tobytes() != paint(n).tobytes():
                    stale.append(n)
            check("every frame matches app/icon.py (re-run make-icon.py if not)",
                  stale, [])

    # The browser tab. This was a bare 204, which draws nothing.
    client = create_app(StubOrch()).test_client()
    resp = client.get("/favicon.ico")
    check("GET /favicon.ico", resp.status_code, 200)
    check("...declares an icon", resp.headers.get("Content-Type"), "image/x-icon")
    ok("...and is a real .ico", resp.data[:4] == b"\x00\x00\x01\x00" and len(resp.data) > 500)


def test_quit_can_actually_end_the_process() -> None:
    """Tray Quit has to get past the handler that hides the window to tray.

    pywebview fires the `closing` event for a programmatic `window.destroy()`
    exactly as it does for the user clicking X, and the handler answers every
    close by returning False, which cancels it. So Tray._quit destroyed the
    window, had its own quit cancelled, and left the process with no window,
    no tray icon (icon.stop() had already run) and nothing able to end it.

    Measured against the installed pywebview 6.2.1, not inferred:
    destroy() returned in 0.01s, the handler ran and returned False, and
    webview.start() never returned -- a 12s watchdog killed the probe.

    The window here is a fake that reproduces the one mechanism that matters
    (destroy fires the event; a False cancels), so the ordering is asserted
    rather than assumed. A real window would need a display and a human.
    """
    print("\nquit -- the tray's Quit must be able to end the process")

    from app.main import Tray, closing_action

    class FakeWindow:
        """destroy() fires `closing`; any handler returning False cancels it."""

        def __init__(self, quitting: threading.Event) -> None:
            self.quitting = quitting
            self.destroyed = False
            self.hidden = 0
            self.flag_at_destroy: bool | None = None

        def hide(self) -> None:
            self.hidden += 1

        def destroy(self) -> None:
            # What pywebview does, and the reason the ordering is the whole
            # bug: this is recorded *before* the handler runs, so the test can
            # see whether the flag was already set when destroy() was called.
            self.flag_at_destroy = self.quitting.is_set()
            if closing_action(self.quitting.is_set()) == "close":
                self.destroyed = True

    class FakeIcon:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    class FakeOrch:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    # The decision itself, both ways -- without this the rest could pass on a
    # handler that never consults the flag at all.
    check("an ordinary close hides to tray", closing_action(False), "hide")
    check("...and the quit's own close is let through",
          closing_action(True), "close")

    quitting = threading.Event()
    quit_only = FakeWindow(quitting)
    check("a window is not destroyed while nothing is quitting",
          quit_only.destroyed, False)

    window = FakeWindow(quitting)
    orch, icon = FakeOrch(), FakeIcon()
    tray = Tray(orch, window, quitting)
    tray._icon = icon
    tray._quit()

    check("quit stops the worker", orch.stopped, True)
    check("quit stops the tray icon", icon.stopped, True)
    # The ordering, which is what was wrong: destroy() must be called with the
    # flag already set. Setting it after would be too late -- the handler has
    # already run and cancelled by then.
    ok("the quitting flag is set before destroy() is called",
       window.flag_at_destroy is True)
    # And the outcome the user sees: the process can end.
    ok("...so the window is really destroyed and start() can return",
       window.destroyed)
    check("...and it did not merely hide again", window.hidden, 0)


def test_tray_update_item() -> None:
    """The tray offers a manual update check, guarded like the scrape.

    The menu is asserted through a stubbed pystray rather than a real icon:
    building the Menu is offline and instant, while Icon.run() would block
    the suite on a tray that does not exist on a headless run. The stub is
    swapped in for the duration of start() only, and removed after.
    """
    print("\ntray -- Check for updates")

    from app.main import Tray

    class GuardOrch:
        def __init__(self, busy: bool) -> None:
            self.busy = busy
            self.requested = False

        def is_busy(self) -> bool:
            return self.busy

        def request_update_check(self) -> None:
            self.requested = True

    # Busy-guards exactly like Scrape Now: a check issued during a download
    # would queue behind the download's busy window and arrive as a surprise.
    busy = GuardOrch(True)
    Tray(busy, window=None)._check_update()
    check("a busy worker means no check is requested", busy.requested, False)

    free = GuardOrch(False)
    Tray(free, window=None)._check_update()
    check("an idle worker gets the check", free.requested, True)

    # And the menu the user actually sees names the thing.
    import sys
    import types

    class FakeMenuItem:
        def __init__(self, label, *args, **kwargs) -> None:
            self.label = label if isinstance(label, str) else ""

    class FakeMenu:
        SEPARATOR = None

        def __init__(self, *items) -> None:
            self.items = items

    captured: dict = {}

    class FakeIcon:
        def __init__(self, name, icon, title, menu) -> None:
            captured["menu"] = menu

        def run(self) -> None:
            pass

    stub = types.SimpleNamespace(
        Menu=FakeMenu, MenuItem=FakeMenuItem, Icon=FakeIcon)
    sys.modules["pystray"] = stub
    try:
        Tray(GuardOrch(False), window=None).start()
    finally:
        sys.modules.pop("pystray", None)

    labels = [getattr(i, "label", "") for i in captured["menu"].items]
    ok("the built menu contains 'Check for updates'",
       "Check for updates" in labels)


def test_the_ui_answers_only_its_own_launch() -> None:
    """A running app serves only the person whose app it is.

    Loopback is shared by every account on a PC. Before the key, a second
    person's app on a shared machine failed to bind 8765 and its window opened
    on the *first* person's server — their calendar, their live LSM session.
    Now each launch mints a key: pages opened through the key URL get a
    cookie, and nothing else gets anything but 401.
    """
    print("\nper-launch key")

    key = "k" * 43
    client = create_app(StubOrch(), access_key=key,
                        instance_id="inst-1").test_client()

    check("the page without the key is refused", client.get("/").status_code, 401)
    ok("...with a page that says where to open it",
       b"notification area" in client.get("/").data)
    check("the API without the key is refused",
          client.get("/api/status").status_code, 401)
    check("a wrong key is refused",
          client.get("/api/status", headers={"X-LSM-Key": "nope"}).status_code,
          401)
    check("the favicon needs no key", client.get("/favicon.ico").status_code, 200)

    entry = client.get(f"/list?rooms=142&k={key}")
    check("the key URL redirects", entry.status_code, 303)
    check("...to the clean URL, other parameters kept",
          urlsplit(entry.headers["Location"]).path + "?"
          + urlsplit(entry.headers["Location"]).query, "/list?rooms=142")
    cookie = entry.headers.get("Set-Cookie", "")
    ok("...setting an HttpOnly, SameSite=Strict cookie",
       "HttpOnly" in cookie and "SameSite=Strict" in cookie and key in cookie)
    check("with the cookie the page is served", client.get("/").status_code, 200)
    status = client.get("/api/status")
    check("...and the API", status.status_code, 200)
    check("...which names the launch", status.get_json().get("instance"),
          "inst-1")

    header = create_app(StubOrch(), access_key=key,
                        instance_id="inst-1").test_client()
    check("the key as a header is the handshake's way in",
          header.get("/api/status", headers={"X-LSM-Key": key}).status_code, 200)

    # And the test clients the other suites build stay open: no key, no gate.
    check("an app built without a key serves openly",
          create_app(StubOrch()).test_client().get("/api/status").status_code, 200)


def test_odd_input_is_a_refusal_not_a_crash() -> None:
    """Input anyone can send gets a 4xx, never a 500 with a traceback.

    hmac.compare_digest raises TypeError on a str with any non-ASCII in it,
    so `?k=%C3%A9` from anyone on the machine was an unauthenticated 500.
    /api/update/token called .get on whatever JSON arrived, so `[1]` was
    another. And LSM_PORT=70000 parsed as an int that socket.bind answers
    with OverflowError — not the OSError main.py catches.
    """
    print("\nodd input is refused, not a crash")
    import logging
    from logging.handlers import RotatingFileHandler

    from app import config

    key = "k" * 43
    client = create_app(StubOrch(), access_key=key).test_client()
    for label, kwargs in (
            ("a non-ASCII ?k=", {"path": "/?k=%C3%A9"}),
            ("a non-ASCII ?k= on the API", {"path": "/api/status?k=%C3%A9"}),
            ("a non-ASCII key header",
             {"path": "/api/status", "headers": {"X-LSM-Key": "é"}})):
        try:
            code = client.get(kwargs["path"],
                              headers=kwargs.get("headers")).status_code
        except Exception as exc:
            code = f"raised {type(exc).__name__}"
        check(f"{label} is a 401", code, 401)
    client.set_cookie("lsm_key", "é", domain="localhost")
    try:
        code = client.get("/api/status").status_code
    except Exception as exc:
        code = f"raised {type(exc).__name__}"
    check("a non-ASCII cookie is a 401", code, 401)
    check("...and the right key still gets in",
          client.get("/api/status", headers={"X-LSM-Key": key}).status_code,
          200)

    open_client = create_app(StubOrch()).test_client()
    for body in ([1], "token", 7):
        r = open_client.post("/api/update/token", json=body)
        check(f"a token body of {body!r} is a 400", r.status_code, 400)
        check("...with the usual sentence", (r.get_json() or {}).get("message"),
              "No token was sent")

    for raw, want in ((None, 8765), ("", 8765), ("9000", 9000), ("0", 0),
                      ("65535", 65535), ("70000", 8765), ("65536", 8765),
                      ("-1", 8765), ("abc", 8765)):
        port, why = config.parse_port(raw)
        warns = bool(raw) and want == 8765
        check(f"LSM_PORT={raw!r} -> {want}", port, want)
        check(f"...and {'a' if warns else 'no'} warning", why is not None, warns)

    ok("app.log rotates",
       any(isinstance(h, RotatingFileHandler) and h.maxBytes > 0
           for h in logging.getLogger("lsm").handlers))


def test_a_second_launch_gets_its_own_server() -> None:
    """Two people on one PC: the second bind falls back, and the handshake
    will not accept the first person's server as its own."""
    print("\nsecond launch on a shared PC")

    import socket

    from app import main as app_main

    saved_port, saved_web = app_main.WEB_PORT, dict(app_main._WEB)
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    first_state = None
    try:
        # The first person's app on its port.
        ok("the first launch binds", app_main._start_server(StubOrch()))
        first_state = dict(app_main._WEB)
        ok("...and its handshake passes", app_main._wait_for_server(timeout=5))

        # The second person's app asks for the same port.
        app_main.WEB_PORT = first_state["port"]
        ok("the second launch still binds", app_main._start_server(StubOrch()))
        second = dict(app_main._WEB)
        ok("...on a different port", second["port"] != first_state["port"])
        ok("...and its handshake passes on its own server",
           app_main._wait_for_server(timeout=5))
        ok("...with a key of its own", second["key"] != first_state["key"])

        # Pointed at the first person's server, the second launch's handshake
        # must refuse it — that is the shape of the old defect.
        app_main._WEB.update(port=first_state["port"])
        check("the handshake refuses another launch's server",
              app_main._wait_for_server(timeout=1.5), False)

        # A keyless server — an older version of this app, still on 8765 for
        # someone else — answers /api/status to anybody. Only the instance id
        # tells it apart, so this is the leg that pins that check.
        from werkzeug.serving import make_server
        old = make_server("127.0.0.1", 0, create_app(StubOrch()), threaded=True)
        threading.Thread(target=old.serve_forever, daemon=True).start()
        try:
            app_main._WEB.update(port=old.port)
            check("the handshake refuses a keyless older version's server",
                  app_main._wait_for_server(timeout=1.5), False)
        finally:
            old.shutdown()
        app_main._WEB.update(second)
    finally:
        for st in (first_state, dict(app_main._WEB)):
            if st and st.get("server"):
                st["server"].shutdown()
        held.close()
        app_main.WEB_PORT = saved_port
        app_main._WEB.clear()
        app_main._WEB.update(saved_web)

def test_single_instance() -> None:
    """A second instance must not share the data directory.

    Two processes on one data dir means one SQLite file *and* one Chromium
    profile holding a live LSM session — the second instance scrapes and
    heartbeats against LSM on its own, over the first one's session. Nothing
    used to stop that: the port bind failed silently inside a daemon thread
    while _wait_for_server succeeded by connecting to the *first* instance's
    server, so the second launch looked healthy.

    The lock is on the data directory rather than the port on purpose: the
    directory is the shared resource, and a port probe would call a TIME_WAIT
    socket "already running" and would not catch an instance whose server had
    died but whose worker was still going.
    """
    print("\nsingle instance")

    from app.main import _claim_instance

    # Two fds on one file, as two processes would be. The second must lose.
    first = _claim_instance(DATA_DIR / "test.lock")
    ok("the first claim is granted", first is not None and first != -1)
    second = _claim_instance(DATA_DIR / "test.lock")
    check("the second claim is refused", second, None)

    # The mechanism is only half of it: the app has to *act* on it, and the
    # first check in each entry point returns before the database is opened or
    # a worker is started, so both are safe to call here. run_app is called with
    # show_window=False so the already-running notice does not put up a modal.
    import contextlib
    import io

    from app.main import run_app, run_scrape_once

    held = _claim_instance(DATA_DIR / "app.lock")

    for label, call in (("run_app", lambda: run_app(show_window=False)),
                        ("run_scrape_once", run_scrape_once)):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = call()
        check(f"{label} refuses to start while another instance holds the dir",
              code, 1)
        ok(f"...and {label} says why", "already running" in buf.getvalue())

    if isinstance(held, int) and held > 0:
        os.close(held)

    # And with the lock free, the refusal is the guard rather than the call
    # failing for some unrelated reason. run_scrape_once would reach LSM from
    # here, so this one is not driven past the gate.
    check("with the lock free, nothing refuses", _claim_instance(
        DATA_DIR / "app.lock") is not None, True)

    # A different directory is a different instance, which is how a second copy
    # is meant to be run — LSM_DATA_DIR is the documented escape hatch.
    other = Path(_tmp) / "second-instance"
    other.mkdir(exist_ok=True)
    third = _claim_instance(other / "test.lock")
    ok("a different data directory is a different instance",
       third is not None and third != -1)

    # Dropping the first releases the lock, so a restart after a clean quit is
    # not permanently blocked. (A crash releases it the same way: the kernel
    # owns the lock, not the process.)
    if isinstance(first, int) and first > 0:
        os.close(first)
    fourth = _claim_instance(DATA_DIR / "test.lock")
    ok("closing the holder frees the lock", fourth is not None and fourth != -1)
    if isinstance(fourth, int) and fourth > 0:
        os.close(fourth)


def test_tray_flag() -> None:
    """--tray is how autostart keeps the window off the desktop.

    The autostart shortcut promised "starts minimized to the tray" while the
    app had no start-hidden mode at all: run_app created the window
    unconditionally, so every login put a 1400x920 window over the desktop.
    The flag only has to reach run_app as start_hidden=True, and to refuse
    to combine with --no-window — the hiding itself is pywebview's hidden=
    parameter, and the tray's Open Calendar already calls the window.show()
    that unhides it.

    run_app is stood in for, not run: the real one starts a server and a
    worker, which no smoke test may do.
    """
    print("\n--tray flag")

    import contextlib
    import io

    from app import main as app_main

    calls = {}

    def fake_run_app(show_window=True, start_hidden=False):
        calls["show_window"] = show_window
        calls["start_hidden"] = start_hidden
        return 0

    saved = app_main.run_app
    app_main.run_app = fake_run_app
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = app_main.main(["--tray"])
        check("--tray reaches run_app as start_hidden=True",
              calls.get("start_hidden"), True)
        check("--tray still creates the window, hidden",
              calls.get("show_window"), True)
        check("the app's exit code travels back", code, 0)

        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            app_main.main([])
        check("no flags means start_hidden=False",
              calls.get("start_hidden"), False)
    finally:
        app_main.run_app = saved

    # The two window shapes are one choice, not two: a headless serve that
    # also owns a hidden window is a contradiction, and letting it pass
    # would leave whichever branch happened to be checked first as winner.
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            app_main.main(["--tray", "--no-window"])
        ok("--tray and --no-window together are refused", False)
    except SystemExit as e:
        check("--tray and --no-window together are refused", e.code, 2)


def test_the_handshake_ignores_the_system_proxy() -> None:
    """The loopback handshake must not go through a configured proxy.

    urlopen() follows the proxy settings, and Windows' "<local>" bypass does
    not cover 127.0.0.1, so on a proxied machine the launch key went to the
    proxy and startup failed. A dead proxy with no bypass reproduces it on
    any OS: the old code times out, the fixed one never asks the proxy.
    """
    print("\nloopback handshake vs. a system proxy")

    from app import main as app_main

    keys = ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")
    saved_env = {k: os.environ.get(k) for k in keys}
    saved_web = dict(app_main._WEB)
    try:
        ok("the server binds", app_main._start_server(StubOrch()))
        for k in keys:
            os.environ.pop(k, None)
        os.environ["http_proxy"] = "http://127.0.0.1:9"   # nothing listens
        ok("the handshake passes with a dead proxy configured",
           app_main._wait_for_server(timeout=5))
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if app_main._WEB.get("server"):
            app_main._WEB["server"].shutdown()
        app_main._WEB.clear()
        app_main._WEB.update(saved_web)


def test_the_tray_status_line_refreshes() -> None:
    """pystray's win32 menu is built once; the status line has to be pushed.

    Nothing called update_menu(), so the line kept its launch-time text
    ("Checking session…") for as long as the app ran.
    """
    print("\ntray -- status line refresh")

    import time

    from app.main import Tray

    class Orch:
        def __init__(self) -> None:
            self.st = {"busy": True, "busy_action": "Checking session"}

        def status(self) -> dict:
            return dict(self.st)

        def stop(self) -> None:
            pass

    class Icon:
        def __init__(self) -> None:
            self.updates = 0

        def update_menu(self) -> None:
            self.updates += 1

        def stop(self) -> None:
            pass

    orch, icon = Orch(), Icon()
    tray = Tray(orch, window=None)
    tray._icon = icon
    worker = threading.Thread(target=tray._refresh_label, args=(0.01,),
                              daemon=True)
    worker.start()
    time.sleep(0.1)
    check("an unchanged status does not rebuild the menu", icon.updates, 0)
    orch.st = {"busy": False, "session": "ok"}
    time.sleep(0.1)
    check("a changed status rebuilds it, once", icon.updates, 1)
    tray._quit()
    worker.join(timeout=2)
    ok("quitting stops the refresher", not worker.is_alive())


def test_a_second_launch_hands_over() -> None:
    """Opening the app while it runs shows its window, not an error box.

    With autostart the app is always already running, so every Start-menu
    launch used to end on "already running". The OS side (a named event) is
    Windows-only; what is asserted here is the decision around it.
    """
    print("\nsecond launch hands over")

    import contextlib
    import io

    from app import main as app_main

    name = app_main._show_event_name(DATA_DIR)
    ok("the event lives in the session's Local namespace",
       name.startswith("Local\\"))
    check("...is stable for one data directory",
          app_main._show_event_name(DATA_DIR), name)
    ok("...and differs for another (LSM_DATA_DIR copies stay apart)",
       app_main._show_event_name(Path(_tmp) / "other") != name)

    signalled: list[bool] = []
    notices: list[bool] = []
    saved = (app_main._ask_running_instance_to_show,
             app_main._already_running_notice)
    answer = {"ok": True}

    def fake_signal() -> bool:
        signalled.append(True)
        return answer["ok"]

    app_main._ask_running_instance_to_show = fake_signal
    app_main._already_running_notice = lambda gui=False: notices.append(gui)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            check("a windowed launch that reaches the instance exits 0",
                  app_main._second_launch(True, False), 0)
            check("...with no notice", notices, [])
            check("a --tray launch exits quietly",
                  app_main._second_launch(True, True), 0)
            check("...without waking the window", len(signalled), 1)
            answer["ok"] = False
            check("an instance that cannot be reached is still reported",
                  app_main._second_launch(True, False), 1)
            check("...with the GUI notice", notices, [True])
            check("a headless launch reports on the console",
                  app_main._second_launch(False, False), 1)
            check("...and does not signal", len(signalled), 2)
    finally:
        (app_main._ask_running_instance_to_show,
         app_main._already_running_notice) = saved


def test_system_closes_get_past_hide_to_tray() -> None:
    """Sign-out, shutdown and installers must be able to close the window.

    pywebview cancels every close our `closing` handler answers False, so
    hide-to-tray blocked Windows sign-out and the installer's Restart Manager.
    The handler added after pywebview's lets any close but the user's through
    and runs the quit path. Driven with fake event args: the real Form needs
    Windows.
    """
    print("\nsystem closes get past hide-to-tray")

    import types

    from app.main import _system_close_handler

    quitting = threading.Event()
    quits: list[int] = []
    done = threading.Event()

    def quit_app() -> None:
        quits.append(1)
        done.set()

    handler = _system_close_handler("UserClosing", quitting, quit_app)

    user = types.SimpleNamespace(CloseReason="UserClosing", Cancel=True)
    handler(None, user)
    check("the user's X still hides to tray", user.Cancel, True)
    ok("...and does not quit", not quitting.is_set() and not quits)

    shutdown = types.SimpleNamespace(CloseReason="WindowsShutDown", Cancel=True)
    handler(None, shutdown)
    check("a Windows shutdown is let through", shutdown.Cancel, False)
    done.wait(2)
    ok("...and the app quits", quitting.is_set() and quits == [1])

    again = types.SimpleNamespace(CloseReason="TaskManagerClosing", Cancel=True)
    handler(None, again)
    check("a later system close is let through too", again.Cancel, False)
    check("...without starting a second quit", quits, [1])


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — smoke tests")
    print(f"  data dir: {DATA_DIR}")
    print("=" * 60)

    test_store()
    test_migration()
    test_web()
    test_cross_site_writes()
    test_login_detection()
    test_quit_can_actually_end_the_process()
    test_tray_update_item()
    test_the_ui_answers_only_its_own_launch()
    test_odd_input_is_a_refusal_not_a_crash()
    test_a_second_launch_gets_its_own_server()
    test_single_instance()
    test_tray_flag()
    test_the_handshake_ignores_the_system_proxy()
    test_the_tray_status_line_refreshes()
    test_a_second_launch_hands_over()
    test_system_closes_get_past_hide_to_tray()
    test_icon()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
