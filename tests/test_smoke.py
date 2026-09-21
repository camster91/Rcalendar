"""
End-to-end smoke test: storage round-trip and its migrations, the
cross-site refusal, the export's event identity, and every web endpoint.

Uses Flask's test client and a stub orchestrator, so no browser or LSM
session is involved. Run directly:

    python tests/test_smoke.py
"""

from __future__ import annotations

import sys
import sqlite3
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

from app import store  # noqa: E402
from app.config import DATA_DIR, DB_PATH  # noqa: E402
from app.server import create_app  # noqa: E402

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

    r = client.get("/api/events?room=142")
    check("events filter endpoint", r.get_json()["total_events"], 1)

    r = client.get("/api/autocomplete?q=142")
    ok("autocomplete finds room", "142" in r.get_json()["rooms"])
    r = client.get("/api/autocomplete?q=")
    check("autocomplete empty query", r.get_json()["rooms"], [])

    r = client.get("/api/today")
    check("today endpoint", r.status_code, 200)
    ok("today has rooms list", isinstance(r.get_json()["rooms"], list))

    r = client.get("/download/json")
    check("json export status", r.status_code, 200)
    ok("json export body", b"events" in r.data)

    r = client.get("/download/ics")
    check("ics export status", r.status_code, 200)
    ok("ics looks like a calendar", b"BEGIN:VCALENDAR" in r.data)
    ok("ics contains our event", b"RSM6307" in r.data)

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

    # The app's own calls keep working, under either spelling of loopback.
    for origin in ("http://127.0.0.1:8765", "http://localhost:8765"):
        orch.calls.clear()
        r = client.post("/api/scrape", headers={"Origin": origin})
        check(f"loopback origin accepted ({origin})", r.status_code, 200)
        check(f"...and the call landed ({origin})", orch.calls, ["scrape"])

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


def test_ics_uids() -> None:
    """A UID is what a calendar client believes an event *is*.

    Re-importing the export is the normal way this file gets used, and the
    client matches on UID. Two VEVENTs under one UID are therefore not two
    events for long: RFC 5545 makes the UID the identity, so the importer
    keeps one and drops the other with nothing on screen to say a booking
    went missing.

    The pair below is what the old key could not tell apart — the old UID
    hashed title|room|start, so it agreed with itself across two bookings
    that `end` distinguishes. That `end` is in play here and not some other
    field is the point: it is exactly the component the old key dropped.
    """
    print("\nics identity")

    from app import ics

    a = {"title": "Sunday service block", "room": "127",
         "start": "2026-03-15T00:00:00", "end": "2026-03-16T00:00:00",
         "all_day": True, "location": "RT 127"}
    b = {"title": "Sunday service block", "room": "127",
         "start": "2026-03-15T00:00:00", "end": "2026-03-15T02:00:00",
         "all_day": False, "location": "RT 127"}

    # The store holds these as two bookings; the export has to agree.
    ok("the store calls these two bookings",
       store.event_uid(a) != store.event_uid(b))
    ok("so the .ics UID differs too", ics._uid(a) != ics._uid(b))

    import re

    body = ics.build_ics([a, b])
    uids = [m.strip() for m in re.findall(r"^UID:(.+)$", body, re.M)]
    check("both bookings are written", body.count("BEGIN:VEVENT"), 2)
    check("...and they carry two UIDs", len(set(uids)), 2)

    # The UID still has to be globally unique, which is what the domain suffix
    # is for — a bare hash is not a UID an importer can trust.
    ok("the UID keeps its domain suffix",
       all(u.endswith("@rotman-lsm-calendar") for u in uids))

    # Re-exporting the same booking must produce the same UID, or every
    # refresh would land in the client as a new event beside the old one.
    check("the same booking exports to the same UID",
          ics._uid(a), ics._uid(dict(a)))

    # A whole corpus rather than just the pair, including a cancelled booking
    # and one with no end time — the two shapes the exporter skips or fills in.
    # Every booking that reaches a VEVENT has to have a UID of its own.
    corpus = sample_events() + [a, b]
    every = ics.build_ics(corpus)
    written = [r for r in corpus if r.get("start") and not r.get("cancelled")]
    all_uids = [m.strip() for m in re.findall(r"^UID:(.+)$", every, re.M)]
    check("every exported booking is written",
          len(all_uids), len(written))
    check("...and has a UID apiece", len(set(all_uids)), len(written))


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


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — smoke tests")
    print(f"  data dir: {DATA_DIR}")
    print("=" * 60)

    test_store()
    test_migration()
    test_web()
    test_cross_site_writes()
    test_ics_uids()
    test_login_detection()
    test_single_instance()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
