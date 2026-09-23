"""
Configuration — paths, tunables and room metadata.

Everything writable lives under a single data directory so the packaged
.exe and a dev checkout behave identically:

    frozen (.exe)  -> %LOCALAPPDATA%\\RotmanLSMCalendar
    dev checkout   -> <project>/data

No credentials are stored here. The LSM session lives in a Chromium
profile under <data>/profile (see app/session.py).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

APP_NAME = "Rotman LSM Calendar"

# The one place a version is written down. It appears in the log at startup and
# in the selftest report, and packaging\build.ps1 reads it out of this file and
# hands it to the installer, so Add/Remove Programs and the app cannot end up
# claiming different versions. Bump it when you ship a build.
APP_VERSION = "1.1.1"
APP_SLUG = "RotmanLSMCalendar"

# ── Paths ────────────────────────────────────────────────────────────────
FROZEN = getattr(sys, "frozen", False)
PROJECT_DIR = Path(__file__).resolve().parent.parent

# LSM_DATA_DIR wins if set — lets a scheduled task or a test run use its
# own profile and database without touching the interactive one.
_data_override = os.environ.get("LSM_DATA_DIR")

if _data_override:
    DATA_DIR = Path(_data_override).expanduser().resolve()
    BUNDLE_DIR = Path(sys._MEIPASS) if FROZEN else PROJECT_DIR  # type: ignore[attr-defined]
elif FROZEN:
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_SLUG
    BUNDLE_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    DATA_DIR = PROJECT_DIR / "data"
    BUNDLE_DIR = PROJECT_DIR

# Web assets ship inside the bundle; user data never does.
WEB_DIR = BUNDLE_DIR / "web" if FROZEN else PROJECT_DIR / "web"

# The per-user Playwright browser cache (chromium, ffmpeg, ...). Bundling it
# would add ~150 MB to every copy of the app, so build.ps1 installs it here and
# the packaged build reads it from here too.
PLAYWRIGHT_BROWSERS_DIR = (
    Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
)

# Playwright points *frozen* builds somewhere else entirely. Its
# _impl/_transport.py does `env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")`
# when sys.frozen is set, and "0" means "<driver>/package/.local-browsers" —
# a directory nothing ever installs into. Left alone, the package starts,
# serves the UI, and fails every scrape with "Executable doesn't exist ...
# _internal\playwright\driver\package\.local-browsers\...". Worse, nothing in
# the UI says so until someone tries to sign in.
#
# setdefault is the lever: Playwright supplies that default only when the
# variable is unset, so naming the real cache wins. Unfrozen this is already
# the directory Playwright picks on its own (its defaultRegistryDirectory() is
# <AppData>\Local\ms-playwright), so the two builds look in the same place
# rather than merely appearing to.
if FROZEN:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PLAYWRIGHT_BROWSERS_DIR))

PROFILE_DIR = DATA_DIR / "profile"      # Chromium profile — holds the session cookie
DB_PATH = DATA_DIR / "calendar.db"
LOG_PATH = DATA_DIR / "app.log"
ROOMS_PATH = DATA_DIR / "rooms.json"
GROUPS_PATH = DATA_DIR / "room_groups.json"
ICS_PATH = DATA_DIR / "schedule.ics"

for _d in (DATA_DIR, PROFILE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── LSM / APEX endpoints ─────────────────────────────────────────────────
LSM_PORTAL_URL = "https://lsm.utoronto.ca/lsm_portal/"
APEX_AUTH_URL = (
    "https://lsm.utoronto.ca/ords/f?p=143:101::BRANCH_TO_PAGE_ACCEPT::::"
)
ROTMAN_PAGE_ID = 51
APEX_APP_ID = 143
LSM_HOST = "lsm.utoronto.ca"

# Markers that identify an unauthenticated redirect. UofT's SSO lands on
# several hosts — weblogin.utoronto.ca for the form, and
# idpz.utorauth.utoronto.ca for the SAML handoff — so matching exact
# hostnames is brittle. Substring markers on host *or* path are safer.
LOGIN_URL_MARKERS = (
    "weblogin", "idpz", "utorauth", "shibboleth",
    "/idp/profile", "saml2", "/cas/",
)

# ── Scrape behaviour ─────────────────────────────────────────────────────
# "Pull as much data as we can": the report window covers the previous
# month through the end of next month, so a month view rarely runs off
# the edge of the data.
SCRAPE_MONTHS_BACK = 1
SCRAPE_MONTHS_AHEAD = 2
SCRAPE_TIME = "06:00"           # local time, daily
HEARTBEAT_HOURS = 4             # how often to re-ping LSM to keep the session warm
SCRAPE_ON_START = True          # scrape at launch if the last one is stale

# ── History ──────────────────────────────────────────────────────────────
# Keep a rolling year of bookings rather than only the four-month scrape
# window. The margin is not decoration: a backfill window starts on the 1st
# of the month BACKFILL_MONTHS back and today can be the 31st, so the oldest
# row a backfill writes is up to 365 + 30 days old (396 across a leap day).
# Prune at 365 and the first month the backfill fetched is deleted by the
# very next scrape.
KEEP_DAYS = 410                 # ~13.5 months of bookings
CHANGES_KEEP_DAYS = 410         # change-feed rows age out on the same horizon
BACKFILL_MONTHS = 12            # how far back the one-time backfill reaches

CALENDAR_NAME = "UofT Rotman Room Bookings"
CALENDAR_TZ = "America/Toronto"
DEFAULT_DURATION_MINUTES = 60

# ── Browser ──────────────────────────────────────────────────────────────
HEADLESS_WHEN_POSSIBLE = True
NAVIGATE_TIMEOUT_MS = 60_000
BROWSER_TIMEOUT_MS = 30_000
MFA_WAIT_MS = 180_000           # how long to wait for a Duo approval
SLOW_MO_MS = 0

# ── Web UI ───────────────────────────────────────────────────────────────
WEB_HOST = "127.0.0.1"
# LSM_PORT mirrors LSM_DATA_DIR: it lets a packaged build or a test run on a
# spare port without disturbing the instance already holding 8765. There is
# deliberately no matching *host* override — this process holds a live LSM
# session, so it must never bind anything but loopback.
#
# Parsed defensively because this module is imported before logging exists: a
# malformed value must not raise at import time, which in a windowed build is
# an invisible dialog rather than a traceback.
try:
    WEB_PORT = int(os.environ.get("LSM_PORT") or 8765)   # avoid 5000 — often taken
except ValueError:
    WEB_PORT = 8765
WINDOW_TITLE = APP_NAME

# ── Room exclusions ──────────────────────────────────────────────────────
# Rooms that are bookable study space rather than teachable/AV space.
# Kept as patterns so new 134-series breakout rooms are caught automatically.
EXCLUDED_ROOMS: set[str] = set()
EXCLUDED_PATTERNS = (r"^134[A-Z]$",)

ROOM_GROUPS: dict[str, list[str]] = {
    "Classroom": [
        "142", "147", "157", "368", "374", "127", "133", "1065",
        "L1010", "L1020", "L1025", "L1030", "L1060",
    ],
    "North": ["142", "147", "157", "368", "374"],
    "South": ["127", "133", "1065", "L1010", "L1020", "L1025", "L1030", "L1060"],
    "Events": ["100", "2057"],
    "Self Serve": [
        "349", "392", "394", "371B", "371C", "448", "470", "548", "570", "1007",
    ],
}

# Rooms with Panopto capture, for the badge in the calendar.
PANOPTO_ROOMS = {
    "127", "133", "142", "147", "151", "157", "368", "374",
    "1065", "L1010", "L1020", "L1025", "L1060",
}


# ── Logging ──────────────────────────────────────────────────────────────
def setup_logging(level: int = logging.INFO) -> logging.Logger:
    log = logging.getLogger("lsm")
    if log.handlers:
        return log
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    # In a windowed (console=False) frozen build sys.stderr is None, and it is
    # launch-dependent rather than a property of the build. A StreamHandler on
    # None does not raise — handleError is itself gated on sys.stderr — but
    # attaching one anyway only churns exceptions. The FileHandler below
    # carries the log either way.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        log.addHandler(stream)

    try:
        fileh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fileh.setFormatter(fmt)
        log.addHandler(fileh)
    except OSError:
        pass  # read-only data dir — console logging is enough

    log.setLevel(level)
    return log


log = setup_logging()
