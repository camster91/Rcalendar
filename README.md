# Rotman LSM Calendar

A Windows desktop app that shows what's booked in Rotman rooms, refreshed
from the LSM portal automatically every morning.

It reads the Rotman Query report (APEX app 143, page 51), stores the
bookings locally, and renders them in a month / week / day calendar with
room filtering by floor, group and free-text search. It is **read-only** —
it never books, changes or cancels anything.

```
┌─ Rotman Room Bookings ───────────────────────────── □ ✕ ┐
│  [Month] [Week] [Today] [Changes]  ⟳  ⬇ .ics  ⬇ JSON   │
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
python -m app.main --backfill       # redo the one-time history fill and exit
python -m app.main --backfill --months 6   # ...or a shorter reach
python -m app.main --probe          # report session state and exit
```

## Starting automatically at login

```powershell
.\packaging\install-autostart.ps1            # add
.\packaging\install-autostart.ps1 -Remove    # remove
```

This drops a shortcut in your Startup folder pointing at
`.venv\Scripts\pythonw.exe -m app.main` — no admin rights, trivially
reversible. `pythonw` rather than `python` so no console window flashes
on every login. The app comes up in the tray, scrapes at 06:00, and
stays out of the way.

## Building a standalone .exe

```powershell
.\packaging\build.ps1
```

Produces `dist\RotmanLSMCalendar.exe`, about 62 MB. The Playwright
browser is deliberately *not* embedded — it lives in the normal
user-level cache at `%LOCALAPPDATA%\ms-playwright`, which is ~150 MB and
only needs installing once per machine.

> **On this machine the built exe cannot be run.** The build itself
> succeeds and the exe sits in `dist\` quite happily — it survived a
> four-minute watch completely untouched. But every attempt to *execute*
> it failed with `Access is denied`, and the file was deleted within
> about ten seconds of the attempt. The first symptom is easy to
> misread: from Git Bash it looks like a bare `Permission denied`, exit
> code 126.
>
> The likely cause is the endpoint agent. This is a UofT-managed machine,
> and Windows Defender is **switched off** in favour of **SentinelOne**
> and **CrowdStrike Falcon**, both of which are running. A
> freshly-compiled unsigned binary is precisely what such agents remove.
> I could not prove that attribution beyond doubt from inside the
> machine — but the practical answer does not depend on it: **run from
> the venv.** It is the same app, and a signed `python.exe` is not
> treated as hostile.
>
> `.\packaging\build.ps1` still works, and the exe is still worth having
> on an unmanaged machine. `install-autostart.ps1 -UseExe` will point
> the Startup shortcut at it if it survives for you.

## Where the data lives

Everything writable is under one directory:

- **Running from the venv** — `.\data`
- **Packaged .exe** — `%LOCALAPPDATA%\RotmanLSMCalendar`

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
  store.py       SQLite persistence, retention, change log
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
.\.venv\Scripts\python.exe tests\test_changes.py
.\.venv\Scripts\python.exe tests\test_session.py
```

The parser tests cover the cases that actually broke against the live
report — the `15-April    -26` date shape, HHMM times, and the
slot-index trap where a start time under 600 is a timetable period, not
an hour.

## History and changes

The app keeps a **rolling year** of bookings, not just the four months it
scrapes each morning:

| Setting | Default | What it does |
|---|---|---|
| `KEEP_DAYS` | 410 | Bookings older than this are pruned. |
| `BACKFILL_MONTHS` | 12 | How far back the one-time history fill reaches. |

**You do not have to ask for that year.** On the first launch that has a live
session the app fetches it by itself — one calendar month per report run,
oldest first, about three minutes end to end — and never offers to do it
again. There is no button for it and no tray item; the sidebar shows the
progress and then reports what is kept. The work is a first-run job the app
gives itself, so the only thing to decide is nothing.

If a fill is cut short — the session expires partway, a month fails to come
back — it stays owed and resumes on the next launch, or the next time you
sign in. Signing in is enough; there is no need to restart.

A month that comes back *empty* does not count as cut short. An eleven-month
reach into the past always spans a summer, and "no data found" is the normal
answer for a month in which no Rotman room was booked, so such a month is
recorded and stepped over rather than treated as a failure. The cost is that a
genuinely failed report is indistinguishable from a quiet month and goes
unfilled. Nothing is lost by that: an empty report is never reconciled against
stored bookings, so the month is simply never populated rather than emptied.

`python -m app.main --backfill` is the repair path, and the only way to ask
for it a second time. Use it if a fill failed and you do not want to wait for
the next launch. **It is safe to repeat**: a month already fetched reports no
changes and writes nothing.

`--backfill --months 6` fetches a shorter reach, and it does **not** retire
the fill: the app keeps asking for the full `BACKFILL_MONTHS` until a run has
actually reached that far back. Without that, a six-month repair run on a
fresh install would look like a finished fill and the six oldest months would
never be fetched by anything.

The **🔀 Changes** view lists what has appeared and disappeared between
scrapes — the only trace a cancelled booking leaves, since a booking that
vanishes from the report leaves nothing behind in the calendar. The report
carries no booking id, so a booking whose *time* is edited is reported
honestly as one removal plus one addition rather than guessed at as a
"move". Backfilled history is deliberately **not** logged as changes: a
first observation is not a change, and 24,000 of them would bury the real
feed.

The retention margin is not decoration. A backfill window starts on the 1st
of the month a year back, and today can be the 31st, so the oldest row a
backfill writes is up to 396 days old — prune at 365 and the first month it
fetched is deleted by the very next scrape.

## Notes and limits

- The report window is **last month → end of next month**. Widen it in
  `app/config.py` (`SCRAPE_MONTHS_BACK` / `SCRAPE_MONTHS_AHEAD`). This is
  separate from retention: the daily scrape only refreshes four months, and
  the rest of the year is kept from earlier runs.
- Some bookings carry a slot index with no recoverable real time; those
  show as all-day rather than a wrong hour. Guessing would be worse.
- Room exclusions (bookable study rooms like the 134-series) are in
  `app/config.py`. They are filtered out of the calendar **and never
  written to the database** — the filter runs before storage, so an
  excluded room is not retrievable even through the JSON export.
- The web UI binds to `127.0.0.1` only and has no authentication, because
  it has no network surface. Do not change the host to `0.0.0.0` — the
  process holds a live LSM session.
