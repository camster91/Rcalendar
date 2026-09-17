# Rotman LSM Calendar

A Windows desktop app that shows what's booked in Rotman rooms, refreshed
from the LSM portal automatically every morning.

It reads the Rotman Query report (APEX app 143, page 51), stores the
bookings locally, and renders them in a month / week / day calendar with
room filtering by floor, group and free-text search. It is **read-only** —
it never books, changes or cancels anything.

```
┌─ Rotman Room Bookings ───────────────────────────── □ ✕ ┐
│  [Month] [Week] [Today]      ⟳   ⬇ .ics   ⬇ JSON        │
│ ──────────────────────────────────────────────────────── │
│   Mon    Tue    Wed    Thu    Fri                        │
│  ┌─────┐┌─────┐┌─────┐┌─────┐┌─────┐                     │
│  │RSM  ││     ││CIBC ││RSM  ││     │                     │
│  │6307 ││     ││Info ││6307 ││     │                     │
│  └─────┘└─────┘└─────┘└─────┘└─────┘                     │
└──────────────────────────────────────────────────────────┘
  tray ▸ Open Calendar · Scrape Now · Sign in to LSM · Quit
```

## How the session works

LSM is behind UofT Shibboleth SSO with Duo MFA. Rather than storing your
UTORid and password, the app keeps a **Chromium profile** on disk:

| When | What happens |
|---|---|
| First run | Click **Sign in to LSM**. A browser window opens; you sign in and approve Duo **once**. The session cookie lands in the profile. |
| Every morning | The 6 AM scrape reuses that profile. Headless, no prompts. |
| Every 4 hours | A heartbeat re-pings LSM so the Shibboleth idle-timeout doesn't lapse. |
| Session dies | The sidebar shows **Session expired** and a **Sign in to LSM** button appears. One click, one Duo tap. |

The cookie snapshot is also written to `session.bin`, encrypted with
Windows DPAPI — readable only by your Windows account on this machine.
No password is ever stored, and the app never types credentials for you:
MFA stays a human decision.

## Running it

```powershell
# one-time setup
.\packaging\setup.ps1

# run
.\.venv\Scripts\python.exe -m app.main
```

Command-line modes:

```powershell
python -m app.main                 # the app (window + tray)
python -m app.main --no-window      # web UI only, no window
python -m app.main --scrape-once    # scrape and exit (for Task Scheduler)
python -m app.main --probe          # report session state and exit
```

## Building a standalone .exe

```powershell
.\packaging\build.ps1
```

Produces `dist\RotmanLSMCalendar.exe`. The Playwright browser stays in
its normal user-level cache (`%LOCALAPPDATA%\ms-playwright`) rather than
being embedded, which keeps the executable around 30 MB instead of 200 MB.

To start automatically at login:

```powershell
.\packaging\install-autostart.ps1            # add
.\packaging\install-autostart.ps1 -Remove    # remove
```

## Where the data lives

Everything writable is under one directory:

- **Packaged .exe** — `%LOCALAPPDATA%\RotmanLSMCalendar`
- **Dev checkout** — `.\data`

Override with the `LSM_DATA_DIR` environment variable.

| File | Purpose |
|---|---|
| `calendar.db` | SQLite: bookings, scrape history, room metadata |
| `profile/` | Chromium profile — **holds a live session; treat as a password** |
| `session.bin` | DPAPI-encrypted cookie snapshot |
| `room_groups.json` | Editable room groupings shown as filter chips |
| `app.log` | Rolling log |

## Project layout

```
app/
  config.py      paths, tunables, room exclusions
  session.py     the cookie keep-alive: persistent profile, probe, heartbeat
  scrape.py      APEX automation (dates, room shuttle, CSV export)
  parse.py       CSV → events (the messy date/time formats)
  store.py       SQLite persistence
  rooms.py       room names, floors, capacities, Panopto flags
  scheduler.py   background worker: daily scrape + heartbeat
  server.py      local Flask API
  ics.py         .ics export
  main.py        entry point: window + tray
  dpapi.py       Windows at-rest encryption
web/             calendar.html, list.html
tests/           parser + end-to-end tests
```

## Tests

```powershell
.\.venv\Scripts\python.exe tests\test_parse.py
.\.venv\Scripts\python.exe tests\test_smoke.py
```

The parser tests cover the cases that actually broke against the live
report — the `15-April    -26` date shape, HHMM times, and the
slot-index trap where a start time under 600 is a timetable period, not
an hour.

## Notes and limits

- The report window is **last month → end of next month**. Widen it in
  `app/config.py` (`SCRAPE_MONTHS_BACK` / `SCRAPE_MONTHS_AHEAD`).
- Some bookings carry a slot index with no recoverable real time; those
  show as all-day rather than a wrong hour. Guessing would be worse.
- Room exclusions (bookable study rooms like the 134-series) are in
  `app/config.py`. They are filtered out of the calendar but still
  recorded in the database.
- The web UI binds to `127.0.0.1` only and has no authentication, because
  it has no network surface. Do not change the host to `0.0.0.0` — the
  process holds a live LSM session.
