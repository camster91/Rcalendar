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

**This is the way to run the app.** It is what `install-autostart.ps1` points
at by default, and it has a property the packaged build does not: it compiles
nothing. A signed `python.exe` running source is not a binary the endpoint
agents have to form an opinion about, so nothing here appears in the security
console. The packaged build in "Building a standalone .exe" is optional and is
reported when it is made.

Command-line modes:

```powershell
python -m app.main                 # the app (window + tray)
python -m app.main --no-window      # web UI only, no window
python -m app.main --scrape-once    # scrape and exit (for Task Scheduler)
python -m app.main --backfill       # redo the one-time history fill and exit
python -m app.main --backfill --months 6   # ...or a shorter reach
python -m app.main --probe          # report session state and exit
python -m app.main --selftest       # check a packaged build is intact, and exit
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

## Building a standalone .exe (optional — and the build is reported)

**You do not need this to run the app, and it is not part of normal work.** The
venv in "Running it" is the supported path, `install-autostart.ps1` already
defaults to it, and it compiles nothing. Build this only when you specifically
need a self-contained folder — to hand the app to someone without a checkout.

Know the cost before you start: **making the exe puts an entry in the security
console.** The 2026-09-21 build produced two of them, one second apart —
`RotmanLSMCalendar.exe` under `build\` and again under `dist\`, both logged as
`Suspicious Activity · Detected suspicious file`. Nothing was quarantined, the
exe was intact at its full 7,952,049 bytes, and `verify-build.ps1` then passed
end to end. So the report is noise rather than damage — but it is noise on a
managed machine, it names this account in a console someone else reads, and
repeated detections against the same unsigned binary are how a console line
becomes a ticket. Note also that the detection lands when the file is *written*,
not when it runs.

So: build it when there is a reason to, not to keep `dist\` fresh. And do not
leave `-UseExe` autostart pointing at a stale build "because it is already
there". If the exe does have to exist as a shipped artefact, the fix is
allow-listing or signing — see "On this machine" for what each costs.

```powershell
.\packaging\build.ps1
```

Produces a **folder**, `dist\RotmanLSMCalendar\` — not a single file:

| | |
|---|---|
| files | 916 |
| total | ~145 MB |
| `RotmanLSMCalendar.exe` | 7.95 MB |
| `_internal\` | everything else |

The file count is a measurement, not an invariant — it is read off the build
and drifts by a file or two when a dependency changes. What matters is that
`_internal\` travels with the exe.

**Ship the folder: `_internal\` has to travel with the exe.** It holds the
Python runtime, the .NET assemblies the window needs, `web\` and the Playwright
driver. The exe on its own is not the app. It is not a thin launcher either —
the bootloader embeds the Python archive, which is why it is a few MB rather
than a few hundred KB.

The Playwright *browser* is still deliberately **not** embedded: it is ~150 MB
and only needs installing once per machine. `build.ps1` runs
`playwright install chromium`, which puts it in the normal user-level cache at
`%LOCALAPPDATA%\ms-playwright`, and the packaged build reads it from there.
That last clause needed saying in code, not just here — Playwright points
*frozen* builds at a browsers directory inside the bundle instead, so
`app/config.py` names the real cache explicitly. Without that the app starts,
serves the UI, and fails every scrape.

### Verifying a build

```powershell
.\packaging\verify-build.ps1
```

Runs the packaged app's `--selftest` — which imports the GUI stack, checks the
web assets, launches a browser, opens the database and fetches the calendar over
HTTP — then watches the folder for 90 seconds and re-inventories every file
afterwards. It uses a scratch data directory and a spare port, so it will not
disturb a running instance.

The dwell and the inventory are the point. A build does not have to be *deleted*
to be broken: losing one `.pyd` leaves something that starts and misbehaves,
which is worse than a clean kill and invisible to an existence check.

### On this machine

The folder build **runs here**. That is measured, not assumed:
`verify-build.ps1` passes end to end — every check green, 916 files present and
unchanged after the dwell — and the app launches with its window, its web UI and
Chromium all working. (The tray starts too: it logs a line if it fails, and no
such line appears.)

This is a UofT-managed machine: Windows Defender is switched off in favour of
**SentinelOne** and **CrowdStrike Falcon**. A freshly-compiled unsigned binary
is what such agents remove, and the *single-file* build here was removed on
execution. The plausible reason is the one thing the folder build does not do: a
single-file build unpacks itself into `%TEMP%` and executes from there, and that
self-extracting shape is what reads as hostile. That attribution was never
proven, and nothing here depends on it — the folder build is simply measured to
work.

**Flagged is not the same as removed, and the folder build is flagged.** Rebuilt
and verified on 2026-09-21, `build.ps1` produced two SentinelOne detections —
`RotmanLSMCalendar.exe` under `build\` and again under `dist\`, both one second
apart as PyInstaller wrote then collected it, both "Suspicious Activity ·
Detected suspicious file". The console showed **0 quarantined files**, the exe
was still on disk afterwards at its full size, and `verify-build.ps1` then
passed end to end. So on this machine the folder build is reported and left
alone, where the single-file build was reported and taken. Anyone rebuilding
should expect the console to show a detection and should read the quarantine
count, not the threat history, before concluding anything broke.

**What would close the residual risk**, in order of cost — detection here is
reputation-driven, so an unsigned build with a handful of users can be reported
on first sight whatever it does. The folder shape rules out the *dropper*
pattern, not the first impression:

1. **Have the AV team allow-list it.** Key it on **path or publisher, not
   hash** — every rebuild changes the hash, so a hash-keyed exclusion expires
   with the next build. This is the cheap one, and it needs a request rather
   than a config change.
2. **Code-sign it.** The durable fix, and the only one that travels if the app
   ever moves machines. Note that since 2023 an OV certificate needs a hardware
   token or HSM, so this has a purchase and a physical object in it.
3. **Unpack it on one AV machine and read the quarantine count.** Not a fix,
   just the cheapest honest check that the build survives somewhere other than
   here.

None of the three is testable from this box, so none of them should be
promised. Worth naming in an allow-list request is the network surface, which
for this app is small and entirely one-directional: it binds **`127.0.0.1:8765`
only** (loopback, no authentication, no host override), and its only outbound
traffic is HTTPS to `lsm.utoronto.ca` in a Playwright-driven Chromium. There is
no OAuth loopback listener here — that belongs to a different tool of mine, and
naming the wrong port in a security request is worse than naming none. AV teams
approve far faster when told what the binary talks to and why.

A draft of that request, with the measured facts and the placeholders to fill
in, is `packaging/allow-list-request.md`. Both files must agree; the request is
the one that gets sent.

A 90-second dwell is evidence, not a guarantee. If a build ever stops working
here, `verify-build.ps1` is the thing to run, and its output names the stage
that broke.

Running from the venv remains the least surprising option and is unaffected by
any of this; `install-autostart.ps1` still defaults to it, and `-UseExe` points
the Startup shortcut at the folder build.

## Where the data lives

Everything writable is under one directory:

- **Running from the venv** — `.\data`
- **Packaged .exe** — `%LOCALAPPDATA%\RotmanLSMCalendar`

Override with the `LSM_DATA_DIR` environment variable. `LSM_PORT` does the
same job for the web UI's port (default 8765), which is how a packaged build or
a test runs alongside an instance already holding it. There is deliberately no
*matching host* override — the process holds a live LSM session and binds
loopback only.

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
  avail.py       the one answer to "is this room busy?"
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
web/             calendar.html, list.html, filters.js
tests/           parser + end-to-end tests
```

`web/filters.js` holds the helpers both pages need — time and date formatting,
escaping, the event merge, the encoding a booking travels through markup in,
and the URL coercion rules. It is a plain classic
script with **no module wrapper**, so its top-level `const`s share one global
scope with each page's inline script: a name declared in both is a parse-time
`Identifier … has already been declared`, which kills the page blank rather than
degrading. Adding a helper there means deleting its duplicate from the pages in
the same change. The server serves it from a dedicated `/filters.js` route
(`app/server.py`), not a catch-all — this process holds a live LSM session, so
its static surface stays exactly as wide as it needs to be.

## Tests

```powershell
.\.venv\Scripts\python.exe tests\test_parse.py
.\.venv\Scripts\python.exe tests\test_smoke.py
.\.venv\Scripts\python.exe tests\test_changes.py
.\.venv\Scripts\python.exe tests\test_filters.py
.\.venv\Scripts\python.exe tests\test_session.py
.\.venv\Scripts\python.exe tests\test_worker.py
.\.venv\Scripts\python.exe tests\test_web.py
```

The parser tests cover the cases that actually broke against the live
report — the `15-April    -26` date shape, HHMM times, and the
slot-index trap where a start time under 600 is a timetable period, not
an hour. They also pin the 12-hour clock, which the reader accepts and
therefore has to read correctly: `2:00 PM` is 14:00, and `12:30 AM` is
00:30 rather than 12:30.

`test_worker.py` covers the two background-worker guards whose failure is
silent: the report window is read back and checked, and the daily scrape is
marked attempted *before* it runs. Neither opens a browser — the page is
faked and the scrape is stubbed — and both are written so the test fails if
the guard is removed rather than merely passing.

`test_web.py` is the only suite that drives a **real browser** against a real
server, because the defects it exists for lived in the pages and nothing else
could see them. It serves the app with `werkzeug.serving.make_server` on an
ephemeral loopback port and drives it with Playwright.

Two things about it are deliberate. It **aborts every request that is not
loopback** and fails the run if any requested URL mentions `utoronto`, so it
cannot reach UofT SSO by construction rather than by promise — and it takes a
scratch `LSM_DATA_DIR`, so it never touches the real Chromium profile or the
live `session.bin`. It also **fails loudly if Chromium is missing** rather than
skipping, because a suite that quietly does nothing is worse than one that is
absent.

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

## Filters

The sidebar carries the filters you reach for constantly — rooms, search,
groups. Everything else lives behind **⚙ Filters**, which opens over the
calendar: floor, capacity, equipment, and "free at a time".

- **Floor** — one chip per floor *present in the data*, so there is no
  5th Floor chip to select nothing. A room whose floor the data does not state
  gets a **No floor** chip. Floor is a *partition*, unlike capacity below, so
  such a room is not on the floor you picked — the point of the chip is that the
  strict reading is something you can select rather than a room that silently
  vanishes under every floor chip.
- **Seats ≥ N** — a room with **no recorded capacity passes**. 49 of the 91
  rooms have none, and silently hiding half the building from "at least 20" is
  worse than showing a room that might be too small. The panel says the count,
  so the leniency is visible rather than a surprise.
- **Panopto** — the 13 rooms the report flags for capture.
- **Free at a time** — "free from 14:00 for 60 min", answered **per day**: each
  booking is checked against its own date, so a room booked solid on the 11th is
  not shown as free there just because it was free on the 10th you were looking
  at. The answer comes from `/api/today`, which serves both the single-day form
  and a `dates=` batch for the days on screen, and answers both this and Free
  Right Now through the same predicate (`app/avail.py`), so the two cannot drift
  apart.

**Rooms and search also reach the Changes view; the rest do not.** A change row
describes a booking added or removed in the past, so floor, capacity, equipment
and free-at have nothing to say about it. That filtering happens on the server,
not in the browser: the feed's `limit` is applied before any client-side filter
could run, so "no changes for room 142" computed here could be a lie about the
newest 200 rows.

The current filters, the view and the date are written to the URL with
`replaceState`, so **Copy link** hands someone the exact screen:

| Param | Meaning |
|---|---|
| `rooms`, `q` | the same vocabulary the download endpoints already read |
| `groups` | CSV of group names |
| `floors` | CSV of floor names |
| `seats` | minimum capacity, omitted when off |
| `panopto` | `1` when on |
| `free`, `mins` | `HH:MM` and minutes |
| `view`, `date` | the view and the day it was on |

`replaceState` rather than `pushState` is deliberate: with push, every chip
click becomes a back-button step and leaving the page takes a dozen presses.
The cost is that Back does not undo a filter — **Clear all** is the escape
hatch. A **saved filter** captures the filters only, not the view or the date,
because a preset that only makes sense on one day is useless tomorrow. The URL
is the opposite: it carries the date, because a link should land where it was
copied.

The groups are editable in the panel and saved to `room_groups.json` in one
atomic write, so an interrupted save cannot leave truncated JSON behind and
take every group with it. Saving replaces the whole set — the file is the
source of truth — so the calendar no longer synthesizes `Classroom` in the
browser; it is seeded in `app/config.py` and the server is the single source.

`web/list.html` reads **and writes** `rooms`, `q` and `groups`, so a copied link
lands correctly there and the URL stays shareable as you filter. It has no panel
of its own — giving it one would mean a second implementation of every filter,
for the secondary page — but the two pages apply the same three filters the same
way (ANDed across, OR'd within), so a link means the same thing whichever opens
it.

## Notes and limits

- The report window is **last month → end of next month**. Widen it in
  `app/config.py` (`SCRAPE_MONTHS_BACK` / `SCRAPE_MONTHS_AHEAD`). This is
  separate from retention: the daily scrape only refreshes four months, and
  the rest of the year is kept from earlier runs.
- **The window the report actually ran is checked before anything is read
  from it.** The window's bounds are also the bounds of the reconcile delete —
  a stored booking inside the window that the scrape did not report is
  removed, which is how cancellations disappear — so a report that quietly
  ran a narrower window would erase real bookings and still look like a
  success. APEX reformats or clamps a date it does not like rather than
  raising, so the item is read back and a confident disagreement fails the
  run. A readback this code cannot parse *abstains* instead: an unfamiliar
  date format is not evidence of a wrong window, and treating it as one would
  stop every scrape.
- Some bookings carry a slot index with no recoverable real time; those
  show as all-day rather than a wrong hour. Guessing would be worse. The
  same rule covers a 12-hour time: `2:00 PM` is read as 14:00 rather than
  as the `2` the digits spell, and a contradictory `13:00 PM` is dropped
  rather than resolved one way or the other.
- **An exported .ics gives every stored booking its own UID**, derived from
  the store's own event identity (title, room, start *and* end). A calendar
  client matches on UID, so two events sharing one are one event as far as
  the import is concerned — it keeps the first and discards the second
  without saying so. Changing the UID scheme means an export re-imported
  into a client that already holds the old file arrives as new events
  rather than as updates to the existing ones.
- **Free Right Now treats an all-day row as occupying its whole date.** It did
  not always. A `003/RENOVATIONS` block rendered 00:00–23:00 and read as booked
  all day, but a booking whose time could not be recovered — the same all-day
  flag, no end time at all — read as *free all day despite existing*. Rooms
  under a service block now drop out of the free list, which is a visible
  change and the correct one.
- Room exclusions (bookable study rooms like the 134-series) are in
  `app/config.py`. They are filtered out of the calendar **and never
  written to the database** — the filter runs before storage, so an
  excluded room is not retrievable even through the JSON export. The
  patterns match the room whichever way it is written: the report's own
  names and the normalised form the events carry are different strings
  (`RT 134A` against `134A`), and the exclusion holds for both. It used to
  match only the report's spelling, so the exclusion worked or not
  depending on which one the shuttle happened to send.
- The web UI binds to `127.0.0.1` only and has no authentication, because
  it has no network surface. Do not change the host to `0.0.0.0` — the
  process holds a live LSM session.
