"""
End-to-end smoke test: storage round-trip plus every web endpoint.

Uses Flask's test client and a stub orchestrator, so no browser or LSM
session is involved. Run directly:

    python tests/test_smoke.py
"""

from __future__ import annotations

import sys
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
from app.config import DATA_DIR  # noqa: E402
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

    r = client.get("/api/nope")
    check("unknown route 404", r.status_code, 404)


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


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — smoke tests")
    print(f"  data dir: {DATA_DIR}")
    print("=" * 60)

    test_store()
    test_web()
    test_login_detection()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
