# Rotman LSM Calendar

A Windows desktop app that shows what's booked in Rotman rooms, refreshed
from the LSM portal automatically every morning.

It reads the Rotman Query report (APEX app 143, page 51), stores the
bookings locally, and renders them as a month grid, a week grid, a single day,
or a feed of what changed — filtered by room, group, floor, capacity, Panopto
capture and free-at-a-time. It is **read-only** — it never books, changes or
cancels anything.

```
┌─ Rotman Room Bookings ───────────────────────────── □ ✕ ┐
│  [Month] [Week] [Today] [Changes]  🟢 Free now  ⟳        │
│ ──────────────────────────────────────────────────────── │
│   Mon    Tue    Wed    Thu    Fri                        │
│  ┌─────┐┌─────┐┌─────┐┌─────┐┌─────┐                     │
│  │RSM  ││     ││CIBC ││RSM  ││     │                     │
│  │6307 ││     ││Info ││6307 ││     │                     │
│  └─────┘└─────┘└─────┘└─────┘└─────┘                     │
└──────────────────────────────────────────────────────────┘
  tray ▸ Open Calendar · List View
         ● Connected to LSM   (a live label, not a button)
         Scrape Now · Sign in to LSM · Check Session
         Check for updates · Open Data Folder · Open in Browser · Quit
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
If DPAPI is unavailable (which on Windows means the platform is not
reporting as Windows) the snapshot is **not written at all** rather than
written in the clear, and the log says so: that file holds a live
session cookie, so "encryption unavailable" has to mean "no snapshot",
not "snapshot anyone can read". The cost of refusing is signing in again
on the next launch. No password is ever stored, and the app never types
credentials for you: MFA stays a human decision.

## Running it

```powershell
# one-time setup
.\packaging\setup.ps1

# run
.\.venv\Scripts\python.exe -m app.main
```

**This is the way to run the app.** It is what `install-autostart.ps1` points
at, and it has a property the packaged build does not: it compiles nothing. A
signed `python.exe` running source is not a binary the endpoint agents have to
form an opinion about, so nothing here appears in the security console.

There used to be a second documented path — a PyInstaller folder build. It is
still possible and the record of it is kept under [Not the supported
path](#not-the-supported-path-building-an-exe) below, but it is not how the app
is run, nothing points at it, and building it writes an unsigned binary the
endpoint agents report. If you find yourself reading that section, read the
cost paragraph first.

Command-line modes:

```powershell
python -m app.main                 # the app (window + tray)
python -m app.main --tray          # tray only at startup; open the window from the tray
python -m app.main --no-window      # web UI only, no window
python -m app.main --scrape-once    # scrape and exit (for Task Scheduler)
python -m app.main --backfill       # redo the one-time history fill and exit
python -m app.main --backfill --months 6   # ...or a shorter reach
python -m app.main --probe          # report session state and exit
python -m app.main --selftest       # health-check this install end to end, and exit
python -m app.main --install-browser   # fetch Chromium into the per-user cache, and exit
```

`--install-browser` is the one you want on a machine that has never run the app.
Everything else it needs is in its own folder; the browser deliberately is not
(see [Not the supported path](#not-the-supported-path-building-an-exe)), so a
fresh machine has an empty `%LOCALAPPDATA%\ms-playwright`, and the app would
start, serve the calendar, and fail every scrape. The installer runs this for
you; this is how you run it by hand, and how you retry it if that download
failed at install time.

## Starting automatically at login

```powershell
.\packaging\install-autostart.ps1            # add
.\packaging\install-autostart.ps1 -Remove    # remove
```

This drops a shortcut in your Startup folder pointing at
`.venv\Scripts\pythonw.exe -m app.main --tray` — no admin rights, trivially
reversible. `pythonw` rather than `python` so no console window flashes
on every login, and `--tray` so no calendar window does either. The app
comes up in the tray, scrapes at 06:00, and stays out of the way; the
window opens from the tray's "Open Calendar".

## Staying up to date

The app checks the project's GitHub releases at startup and once a day after
that, plus any time you ask it to — the tray's **Check for updates** item, or
the *Updates* section in the Calendar sidebar. A check that finds a newer
release also **pre-downloads it**: the installer is fetched and verified right
then, so **Install update** starts it instead of a 42 MB wait (a pre-download
that fails changes nothing — the offer stands and the click downloads the old
way). An amber banner appears in the toolbar and the sidebar offers
**Install update**: the app checks the downloaded installer against the
`sha256.txt` the release itself published and only then runs it — a download
that does not match is never run. The installer asks the app to close when it
is ready to copy files; your data folder is untouched throughout. *Skip this
version* hides a release you declined until something strictly newer ships.

Releases are read from a **public mirror repository**
(`camster91/rotman-lsm-calendar-releases`) that holds nothing but release
artifacts — the app's own repository is private, and GitHub cannot serve a
release publicly while its repo is not. That makes checks and downloads
**tokenless and zero-config** on every machine.

A GitHub token is therefore optional — a fallback, not a requirement. If one
is pasted into the sidebar's *GitHub token…* row it is stored the way
`session.bin` is: DPAPI-encrypted, readable only by your Windows account on
this machine, never displayed again, and — same refusal, same reason —
**never written at all** when DPAPI is unavailable. It is still sent on every
request, so the update path keeps working unchanged the day the mirror is
ever made private. Without a token, a refused or empty listing fails softly
with one honest sentence rather than pretending to work.

The token travels as an `Authorization: Bearer` header on requests to
`api.github.com` **only**. The installer download goes through the API's asset
endpoint rather than the browser-facing redirect precisely so the header
never has to survive one.

## Where the data lives

Everything writable is under **`.\data`**, beside the checkout. Override it with
the `LSM_DATA_DIR` environment variable. `LSM_PORT` does the same job for the
web UI's port (default 8765), which is how a test runs alongside an instance
already holding it. There is deliberately no *matching host* override — the
process holds a live LSM session and binds loopback only.

The packaged build used a different location, `%LOCALAPPDATA%\RotmanLSMCalendar`,
and that directory may still exist from the exe era. **Do not delete it** until
you have looked: it holds a separate `calendar.db` and a separate Chromium
profile, so if it is where your sign-in and history actually live, deleting it
loses both. `LSM_DATA_DIR` is how you point the app at it if it is the one you
want. The venv path never reads it, so nothing breaks by leaving it alone.

| File | Purpose |
|---|---|
| `calendar.db` | SQLite: bookings, scrape history, room metadata |
| `profile/` | Chromium profile — **holds a live session; treat as a password** |
| `session.bin` | DPAPI-encrypted cookie snapshot |
| `github-token.bin` | DPAPI-encrypted optional GitHub token (updater) |
| `update-staged/` | Half-downloaded installers; cleared on every check |
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
  main.py        entry point: window + tray
  dpapi.py       Windows at-rest encryption
  updater.py     GitHub release check, verified download, installer launch
web/             calendar.html, list.html, filters.js
tests/           parser + end-to-end tests
packaging/       setup, autostart, and the unsupported build
```

`packaging/` holds `setup.ps1` and `install-autostart.ps1` — the two scripts
this README tells you to run — plus `build.ps1`, `RotmanLSMCalendar.spec` and
`verify-build.ps1`, which are the unsupported exe path and are marked as such in
their own headers. It also holds `sign.ps1` (the self-signed code-signing step
`build.ps1` calls), `release-local.ps1` (the local release pipeline — see
[Versions and releases](#versions-and-releases)) and `release-notes.md` (the
release body). `packaging/allow-list-request.md` is the draft AV request
described in that appendix.

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
.\.venv\Scripts\python.exe tests\test_update.py
.\.venv\Scripts\python.exe tests\test_worker.py
.\.venv\Scripts\python.exe tests\test_web.py
```

The parser tests cover the cases that actually broke against the live
report — the `15-April    -26` date shape, HHMM times, and the
slot-index trap where a start time under 600 is a timetable period, not
an hour. They also pin the 12-hour clock, which the reader accepts and
therefore has to read correctly: `2:00 PM` is 14:00, and `12:30 AM` is
00:30 rather than 12:30.

`test_worker.py` covers the background-worker guards whose failure is silent:
the report window is read back and checked, the daily scrape is marked
attempted *before* it runs, and the worker thread survives a command that
raises. None of them opens a browser — the page is faked and the scrape is
stubbed — and each is written so the suite fails if the guard is removed
rather than merely passing.

Two of its tests are about the **export path**, which is what decides whether a
scrape may delete anything. The Download link hands over the whole report; the
rendered results table is one page of an interactive report. `replace_events`
deletes every stored booking in the window the report did not mention, so
reading the page as if it were the report would erase everything past the first
page and file it as a cancellation. `scrape()` therefore marks a page-read
result incomplete, and an incomplete report adds and updates but deletes
nothing — seeing a booking is evidence it exists, whereas not seeing one says
nothing at all. The export path is asserted from both ends: that `scrape()`
sets the flag, and that the flag reaches the store through `_do_scrape`.
Verified by removal in all three places — dropping the store's guard, the
scrape-side flag, or the scheduler's pass-through each turns the suite red.

`test_update.py` covers the updater, and it holds the same line the worker
suite does: **no suite may talk to GitHub**, so every network call in it goes
through one injectable `_fetch` that the tests replace — the real one is
exercised too, against a fake `urlopen`, to pin the headers. The suite pins
the version compare (`1.1.10` is newer than `1.1.9`, a prerelease tag is
ignored), the checksum being the thing that gates the installer (a download
that does not match is never run), the `Bearer` header travelling only when a
token is set, the 24-hour check clock surviving a restart, the endpoints'
busy guards, and the token's DPAPI round trip — including the sentinel that a
machine without DPAPI writes **no** token file at all rather than a cleartext
one, which is the same refusal `session.bin` makes and for the same reason.

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

Four of its tests are about accessibility, and they were written against
defects found by reading the pages rather than by running a checker. The month,
week and day arrows were bare `◀` and `▶` buttons with no name at all; the
scrape button was a `⟳`; the filter panel's seat, duration, "free from" and
preset-name controls were `<label>`s that sat *next to* their inputs instead of
labelling them, so the label was decoration and the control was unnamed. Two
stylesheets dropped the focus ring (`outline:none` with nothing in its place),
and `list.html` had no `:focus-visible` rule at all.

The name check is the one worth describing, because its **first version could
not fail**. It accepted any non-empty `textContent` as a name — and `◀` is text.
Removing an `aria-label` from the page left the suite green, which is how the
hole was found: a test that passes against the defect it was written for is
worse than no test, because it certifies the page. It now requires a letter or
a digit, so a triangle is not a name and `Today` still is. Verified in both
directions — the suite fails on the stripped label and passes on the fix.

The other three pin the things a name check cannot see. That a toggle says
whether it is on through something other than colour — `aria-pressed`, read back
after clicking, which also covers the group buttons, where colour was the only
signal. That every rule removing an outline has a `:focus-visible` replacement,
on both pages. And that the search dropdown and the filter chips can be worked
from the keyboard, which is the one the name check is *blind* to: the suggestions
were `<div>`s and the calendar's chip ✕ was a `<span>`, both carrying handlers,
and a non-control is not a control, so nothing looked at them. They had to be
found by reading. The test settles it the only way that proves reachability —
type, Tab, Enter, then remove the tag it made without a mouse — and it fails on
a `div` at the Tab, which is how that was checked.

One test covers the **Free Right Now** drawer, and it asserts the four
things the redesign was for: that the answer is timestamped, that the rooms are
grouped with each heading counting the rows beneath it, that every row is a real
button, and that choosing one narrows the filter without discarding the rest of
it or moving you off the view you were on. The grouping was **falsified before it
was trusted** — removing the second group heading made three assertions fail
(`got ['Free all day']`, and the all-day count absorbed the "free until" row),
which is the evidence that the assertions are about the grouping rather than
about the panel merely having rendered something.

One thing the suite **cannot** check, and says so: `test_smoke.py`'s quit test
fakes the window, so it asserts what the app does on a `closing` event rather
than that pywebview *raises* one. `tests/probe_quit_path.py` is the manual
counterpart — it opens a real window, closes it twice, and prints what happens
to each, including whether `webview.start()` actually returns. Run it only when
that premise is in question, because it needs a display and the broken shape
runs to its own twelve-second watchdog:

```
.venv\Scripts\python.exe tests\probe_quit_path.py --before   # the bug: HANG
.venv\Scripts\python.exe tests\probe_quit_path.py            # the fix: start() returns
```

It is not part of any suite and CI never runs it. It earns its place because it
is the only thing in the repo that measures the library directly, and the
measurement is what turned "the quit path looks wrong" into "destroy() fires
`closing`, the handler cancels it, and the process outlives the window".

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

A month that is only *partly read* is the opposite case, and does count. If
the report cannot be downloaded the scraper falls back to the rendered results
table, which is one page of an interactive report — so the month ends up
holding its first page of bookings and nothing else. Nothing is destroyed
either (an incomplete report is never reconciled against stored bookings, so
this is adds and updates only), but the fill is not the history and stays
owed. The run is filed as **`partial`** rather than `ok`, the sidebar shows
the note — *11 month(s) only partly read* — and the fill is asked for again at
the next launch or sign-in, when the same month can be read whole. The
distinction is the whole reason the two cases are not one: a quiet month will
answer the same way however often it is asked, and retrying it forever is what
the leniency above exists to prevent, whereas a failed export is about us and
a second attempt can genuinely do better.

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
calendar: floor, capacity, equipment, and "free at a time". The **Free now**
drawer (see below) repeats the two you reach for *while finding a room* — a
floor, and the Classroom/Events groups — as chips beside the free-rooms list,
wired to the same filter state rather than a copy of it.

- **Floor** — one chip per floor *present in the data*, so there is no
  5th Floor chip to select nothing. A room whose floor the data does not state
  gets a **No floor** chip. Floor is a *partition*, unlike capacity below, so
  such a room is not on the floor you picked — the point of the chip is that the
  strict reading is something you can select rather than a room that silently
  vanishes under every floor chip.
- **Seats ≥ N** — a room with **no recorded capacity passes**. About half the
  rooms in the report carry no capacity at all, and silently hiding half the
  building from "at least 20" is worse than showing a room that might be too
  small. The panel states the exact figure for the rooms on screen, so the
  leniency is visible rather than a surprise. (Measured 2026-09-21: 53 of the
  95 rooms the report covered. The figure moves as the report picks up more
  rooms, which is why the panel computes it live and this line does not assert
  it.)
- **Panopto** — the 13 rooms the report flags for capture.
- **Free at a time** — "free from 14:00 for 60 min", answered **per day**: each
  booking is checked against its own date, so a room booked solid on the 11th is
  not shown as free there just because it was free on the 10th you were looking
  at. The answer comes from `/api/today`, which serves both the single-day form
  and a `dates=` batch for the days on screen, and answers both this and Free
  Right Now through the same predicate (`app/avail.py`), so the two cannot drift
  apart.
- **A window that runs past midnight is a question about two days.** Three hours
  from 23:00 end at 02:00 tomorrow, so the rows the predicate is given include
  the next day's and the predicate is left to decide. It used to be given only
  the day the question was asked about, which made a room booked at 00:30 read
  as free for a window it was not free for — the one wrong answer here that
  sends someone to a room already taken. Widening the *rows* and not the answer
  is what keeps an hour from 23:00 free: it stops at midnight, and the booking
  half an hour past it is not in it.

- **Rooms** — the sidebar list is a *picker*: **clicking a room selects it,
  alone**. It used to toggle, which with every room on by default read exactly
  backwards — the one room you clicked was the one that turned off.
  **Ctrl-click (or Shift-click, or Ctrl+Enter from the keyboard) adds or
  removes one room at a time** for a multi-selection, and clicking the room
  that *is* the whole selection undoes it back to every room rather than
  blanking the calendar — an empty selection has no way back, the same
  fallback the `clear` link documents. **clear** next to the *Rooms* heading
  returns to every room. Ctrl-clicking the last selected room *out* falls
  back the same way: both remove paths land on every room, never on nothing.
- **Groups** — one button per group, plus All: **clicking a group selects its
  rooms** — a group is shorthand for a room selection on this page — and
  clicking it again takes the filter off. Turning the last group off used to
  recompute the selection from the now-empty group set and blank the
  calendar with no way back; it lands on every room now, the same fallback
  the room picker's paths use. The buttons carry `aria-pressed`, so a lit
  group is announced rather than colour alone.

**The Active filters row beside *Rooms* is derived from the filter state, not a
history of what you added.** That is the whole point of it: `readURL` and a
saved filter both replace the whole filter at once, and a row that had
accumulated chips would leave the previous filter's chips on screen naming a
filter that was no longer on — and because each chip undoes its filter by type,
removing one then acted on the *new* filter rather than the one it named. A room
chip left over from before widened a two-room saved filter to the whole
building. Deriving the row from the state makes that unrepresentable, and the
count in the ⚙ badge comes from the same list (it used to count four of the
seven filters, so the badge denied three of them). **clear all** sits at the end
of the row and is a plain button rather than a chip, because it is an action
rather than a filter — and because a second element wearing the chip's class
would make every count of the chips off by one, tests included.

## Free Right Now

The list lives in the **toolbar drawer** — one button, `🟢 Free now`, with the
count of rooms it will list — and opens over the calendar with the **quick
filters** beside it: **one chip per floor**, plus the **Classroom** and
**Events** groups. It used to sit in the sidebar and answer for all 91 rooms at
once, which meant no filter could reach it: "is *any* room free" is a much
weaker question than "is a classroom free", and the second question is the one
that actually gets asked.

The chips are **the same filter state as everything else**, not a private copy:
a floor chip writes the floor filter the ⚙ panel holds, and a kind chip toggles
the group the sidebar's group bar holds. A chip click moves the URL, the Active
filters row, the sidebar and the calendar together, so the drawer can never be
the place where a filter is on that isn't on anywhere else. The list answers
with those same filters — every row is a room the calendar would show — and the
button's count is the number the drawer lists, so the button cannot promise
rooms the filters then hide. `Escape` closes it, and the button says which state
it is in (`aria-expanded`).

The list is **grouped and stamped**, which is what the data asked for. Measured
2026-09-21: of 91 rooms, 58 were free; of those 58, **49 had a booking later the
same day** and **9 were free all day**. So *free* on its own is not the useful
word, and the list splits exactly there — *Free all day* and *Free until
HH:MM* — each group sorted by what makes the offers different rather than by room
number, so a room free until 17:00 is not buried behind one free until 11:15.
The heading carries the strong count (the all-day figure).

Every row is a **button**, so the list is reachable by keyboard like the rest of
the page, and choosing one **narrows the filter to that room without discarding
the rest of the filter** or moving you off the view you were on. It used to
clear everything and change the view, from a panel the surrounding markup calls
read-only.

The list is stamped **as of HH:MM** because `/api/today` answers for the moment
it is called. The page fetches it at boot and every 60 seconds — and the heading
says *Free right now*, a claim about the wrong morning after a morning in a
background tab, which the timestamp is there to make visible rather than to
hide. **refresh ↻** re-measures on demand.

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
| `rooms`, `q` | the vocabulary both pages' URL params share |
| `groups` | CSV of group names |
| `floors` | CSV of floor names |
| `seats` | minimum capacity, omitted when off |
| `panopto` | `1` when on |
| `free`, `mins` | `HH:MM` and minutes |
| `view`, `date` | the view and the day it was on |

The toolbar's **⬇ .ics / ⬇ JSON export buttons, and the server's
`/download/ics` and `/download/json` endpoints behind them, were removed at
the user's request** — `app/ics.py` is gone, and with it the two config values
only it read (`CALENDAR_NAME`, `ICS_PATH`). Nothing in the app exports a file
any more; the list page is the bulk view.

`replaceState` rather than `pushState` is deliberate: with push, every chip
click becomes a back-button step and leaving the page takes a dozen presses.
The cost is that Back does not undo a filter — **Clear all** is the escape
hatch. A **saved filter** captures the filters only, not the view or the date,
because a preset that only makes sense on one day is useless tomorrow. The URL
is the opposite: it carries the date, because a link should land where it was
copied.

Saved filters live in the database rather than in browser storage, so they
follow the app rather than one browser profile. Two limits come with that: at
most **20** are kept — saving a 21st drops the oldest — and a name must be
**1–60 characters**, checked on the server as well as in the panel. Saving
under a name that already exists overwrites that one rather than adding a
second. Applying one makes the same inference a link does: a filter that names
a group but no rooms selects that group's rooms, so what lands on the calendar
is what its URL and the List tab both say it is.

The groups are editable in the panel and saved to `room_groups.json` in one
atomic write, so an interrupted save cannot leave truncated JSON behind and
take every group with it. Saving replaces the whole set — the file is the
source of truth — so the calendar no longer synthesizes `Classroom` in the
browser; it is seeded in `app/config.py` and the server is the single source.
A group name cannot contain a **comma**: the shared-link vocabulary joins and
splits group names on that one character, so a name with it would drop out of
every link it landed in. Refused in the panel, at the field, and again on the
server — the whole map is checked in one place.

`web/list.html` reads **and writes** `rooms`, `q` and `groups`, so a copied link
lands correctly there and the URL stays shareable as you filter. It has no panel
of its own — giving it one would mean a second implementation of every filter,
for the secondary page — and the two pages apply the same three filters the same
way (ANDed across, OR'd within), so a link means the same thing whichever opens
it. The one case the pages model differently — a group is shorthand for a room
selection on the calendar, a filter in its own right on the list — has one
precedence rule: when a link names both `rooms` and `groups`, `rooms` is the
more specific statement on either page, and the list writes the AND of its own
two filters into `rooms=` rather than emit a pair that could disagree. A
`groups`-only link still means the group on both pages.

## Keyboard

The calendar works without a mouse, and nine keys do something. Two rules
decide which of them are live, and both exist so a keystroke cannot act on
something you did not mean:

| Key | Does | Live when |
|---|---|---|
| `Esc` | closes the open dialog | always — the only key that is |
| `/` | focuses the search box | nothing is being typed in |
| `←` `→` | back / forward by one unit: a day in Today, a week in Week, a month in Month | nothing is being typed in |
| `T` | jumps the date to today | no button, link or cell has focus |
| `M` `W` `D` `C` | switches to the Month / Week / Today / Changes view | no button, link or cell has focus |
| `Enter` `Space` | activates the focused control — a room chip, a group button, a day cell | that control has focus |

`T` and `D` are close enough to be worth telling apart: `T` moves the date to
today and leaves the view alone, `D` switches to the one-day view. The letters
are the most restricted of the nine on purpose — with a room chip focused, `D`
would otherwise switch the view out from under you — and the arrows and `/` are
unavailable while a text field has focus, because there an arrow key belongs to
the cursor.

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
- **And the export is made to prove its own window.** Reading the dates back
  is a claim about the *items*, not about the render: a Generate click that
  missed leaves the page holding the report it loaded with — the previous
  scrape's window, persisted in APEX session state, not the one that was
  just set. Two things close that. The click is proven by the page's
  **per-render token** (`p_page_submission_id`, measured on the live page
  2026-09-23): it changes on every render, even a content-identical one,
  so an unchanged token means Generate produced no render and the result
  is adds-only — nothing is deleted off it. And every row's date is
  checked against the window asked for, and a row outside it fails the
  run; a row cannot legitimately be outside: the report is filtered to
  that window. Together the two cover the case neither could alone: the
  date check catches a stale report whose bounds differ, and the token
  catches the stale report whose bounds sit inside the requested window,
  where every row is a date the window could legitimately hold.
- Some bookings carry a slot index with no recoverable real time; those
  show as all-day rather than a wrong hour. Guessing would be worse. The
  same rule covers a 12-hour time: `2:00 PM` is read as 14:00 rather than
  as the `2` the digits spell, and a contradictory `13:00 PM` is dropped
  rather than resolved one way or the other.
- **A booking's identity is the change feed's pairing key** (title, room,
  start *and* end). The `end` matters: two blocks can share a name and a
  start, and without the end the feed would pair them and report the
  difference between two bookings as nothing at all.
- **The code prefix is parsed out of a title for display — the store keeps
  the raw one.** LSM prefixes nearly every booking with an internal code —
  `208/CIBC.1/A.MAHAJAN` is code / event / booker, and 20,358 of a real
  store's 29,057 titles carry one. The code never differs within one event
  and says nothing a reader wants, so the UI prints `CIBC.1 — A.MAHAJAN`.
  The split happens where the server hands events to the page, not at
  ingest: the raw title *is* the booking identity above, and rewriting it
  would give every stored booking a new identity and flood the change feed
  once. Searching still works on every part of the raw title, because a
  search over the store is a search over the same string the display was
  built from.
- **Free Right Now treats an all-day row as occupying its whole date.** It did
  not always. A `003/RENOVATIONS` block rendered 00:00–23:00 and read as booked
  all day, but a booking whose time could not be recovered — the same all-day
  flag, no end time at all — read as *free all day despite existing*. Rooms
  under a service block now drop out of the free list, which is a visible
  change and the correct one.
- Room exclusions (bookable study rooms like the 134-series) are in
  `app/config.py`. They are filtered out of the calendar **and never
  written to the database** — the filter runs before storage, so an
  excluded room is not retrievable even through the API. The
  patterns match the room whichever way it is written: the report's own
  names and the normalised form the events carry are different strings
  (`RT 134A` against `134A`), and the exclusion holds for both. It used to
  match only the report's spelling, so the exclusion worked or not
  depending on which one the shuttle happened to send.
- **Scrape Now never opens a sign-in window.** The tray item and the ⟳ button
  both start a *non-interactive* scrape: it probes the session first and, with
  none live, stops and reports `Session expired — sign in required` rather than
  trying to sign in. Signing in is its own action — **Sign in to LSM** in the
  tray, or the button in the sidebar — because the SSO handoff and the Duo
  approval need a real window and a deliberate click, which a scrape running on
  the worker's thread cannot offer. (`POST /api/scrape` does accept
  `interactive=1` to ask for that window; nothing in this repository posts it,
  and the page sends no body at all.)
- The web UI binds to `127.0.0.1` only and has **no authentication** — loopback
  *is* the access control. Anything on this machine running as this user can
  read the calendar and drive the app. The one check there is refuses a
  state-changing request that announces a foreign origin, which is what stops a
  page you merely have open from starting a scrape or ending your session; it
  does not authenticate anyone and does not hide anything. Do not change the
  host to `0.0.0.0` — the process holds a live LSM session, and
  reachable-from-the-network would mean an unauthenticated calendar that anyone
  could read and a scrape anyone could start.

## Versions and releases

`APP_VERSION` in `app/config.py` is the version; `__version__` in
`app/__init__.py` is the same string by import (`from app.config import
APP_VERSION as __version__`), not a second copy that could drift.
`packaging/build.ps1` reads the first, and `--selftest` writes it into
`selftest.json`, so the number the installer would carry and the number the app
reports are one string. The log line at the top of every run names it,
whichever mode the run takes — the point of that is a log from a machine nobody
can look at, which should say which build produced it without being asked.

Releases are tagged `v<APP_VERSION>` on GitHub. Since v1.1.0 a release
also carries the **installer** (`RotmanLSMCalendar-Setup-<version>.exe`)
with its **sha256** in the notes, so an office machine can fetch it
without a checkout — that is the deployment this release exists for, and
since v1.1.2 the in-app updater does that fetch itself (see [Staying up
to date](#staying-up-to-date)).

Since v1.1.2 **both shipped binaries are Authenticode-signed** with the
project's self-signed code-signing certificate. `packaging/sign.ps1` —
called twice from `build.ps1`, on the inner exe before the installer is
compiled and on the Setup exe after — finds the certificate by its fixed
subject or creates it once, and reuses it for every build after that:
every machine that imported the shipped `.cer` trusts that certificate and
no other, so regenerating it per build would strand each of them back at
"unknown publisher" with no way to notice. What a self-signed certificate
buys is a **stable publisher identity** — an endpoint agent can be told to
trust it by certificate rather than by path — and local trust; it does
not buy the reputation a CA-issued certificate carries, so a managed
machine may still flag a first-sight binary. If one does, the fix is the
allow-list request in `packaging/allow-list-request.md`, keyed on path or
publisher, never on hash.

Releases are **built locally, not by Actions** — because of the certificate,
not because of billing. A release has to ship binaries signed with the
project's code-signing certificate, and that certificate is a per-user,
self-signed one in the build machine's user store: a runner cannot hold
it, and what a runner build signs with is an ephemeral certificate
`build.ps1` mints for it, which dies with the runner — measured
2026-09-25, when the first v1.1.2 publish fired `release.yml` (an
API-created tag fires the push event exactly like a pushed one, a premise
this repo had assumed the other way) and the runner overwrote the signed
release within five minutes with a build signed by a certificate that no
longer exists. `release.yml`'s trigger is `workflow_dispatch` only since
that day, so nothing a release does can start it, and
`packaging/release-local.ps1 -Version <APP_VERSION>` is the release path.
It follows the Actions workflow's order: all eight suites, `build.ps1`
(which signs both binaries), `verify-build.ps1` — whose signature checks
read the signing back from the files, because an unreachable timestamp
server degrades `Set-AuthenticodeSignature` to a warning and an unsigned
release would otherwise ship green — then a hash of the **signed**
installer written to `dist\sha256.txt`. The hash is taken after signing
on purpose: that sidecar is what the updater verifies a download against.
The script publishes with `gh release create --target master` **twice** —
once on this private repo and once on the public releases mirror
(`camster91/rotman-lsm-calendar-releases`), with the same signed Setup,
hash and `.cer`, so the repository the updater reads cannot drift from the
one that ships. The Actions workflow stays as a manual/diagnostic path
until signing moves onto the runner (a CA-issued certificate, or a pfx
held in secrets — a decision not made here).

## Not the supported path: building an .exe

**This is not how the app is run.** The venv in [Running it](#running-it) is the
supported path, `install-autostart.ps1` points at it, and it compiles nothing.
Nothing in this repository points at the build below any more — the `-UseExe`
autostart switch that used to is gone. Build only when you specifically need a
self-contained folder, to hand the app to someone with no checkout, and read the
cost first.

**Making the exe puts an entry in the security console.** The 2026-09-21 build
produced two of them, one second apart — `RotmanLSMCalendar.exe` under `build\`
and again under `dist\`, both logged as `Suspicious Activity · Detected
suspicious file`. Nothing was quarantined, the exe was intact at its full
7,952,049 bytes, and `verify-build.ps1` then passed end to end. So the report is
noise rather than damage — but it is noise on a managed machine, it names this
account in a console someone else reads, and repeated detections against the
same unsigned binary are how a console line becomes a ticket. Note also that the
detection lands when the file is *written*, not when it runs.

No spec change prevents that, because the properties the agent scores — the
PyInstaller bootloader, the embedded Python runtime, a bundled `node.exe`
spawning headless Chromium — are what the app is. That is the whole reason this
section is an appendix rather than a second way to run it.

If the exe does have to exist as a shipped artefact, it ships **signed** —
`packaging/sign.ps1`, called from `build.ps1`, signs both the inner exe and
the installer with a self-signed code-signing certificate created once and
reused (see [Versions and releases](#versions-and-releases)). What signing
does not buy is reputation, and [On this
machine](#on-this-machine) is the honest account of what remains.

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

### The installer

`build.ps1` compiles one, if Inno Setup 6 is present:

```
dist\RotmanLSMCalendar-Setup-<version>.exe
```

It packages the folder build above, so it cannot ship a stale `dist\` — the
ordering is the guarantee. If Inno Setup is *not* installed, the script says so,
prints the fix, and leaves the folder build, which is complete and usable on its
own, in place.

**Per-user, on purpose.** `PrivilegesRequired=lowest`, so it installs to
`%LOCALAPPDATA%\Programs\Rotman LSM Calendar` and never raises a UAC prompt — a
standard account can run it, which matters on a machine where an unsigned exe
asking for admin is exactly the pattern that gets reported. Measured
2026-09-21: silent install, 10.2 s, exit 0, 918 files / 149.7 MB (the folder
build's 916 plus `unins000.exe` and `unins000.dat`). The Setup.exe itself is
42.3 MB, which is small next to what it carries because the folder build
compresses well — nothing was trimmed to get there.

The version is read out of `APP_VERSION` in `app/config.py` and passed to the
compiler, so Add/Remove Programs, the wizard and the app's own startup log
cannot claim different versions. `installer.iss` derives the four-part
`FileVersion` resource from it rather than keeping a second copy.

Three tasks, and the middle one is why this is not just a file copy:

| task | default | what it does |
|---|---|---|
| desktop shortcut | off | `{autodesktop}` |
| start at sign-in | off | a `{userstartup}` shortcut |
| download Chromium | on | runs the app's own `--install-browser` |

That last one closes the gap a fresh machine would otherwise fall into. The
browser is deliberately not in the build, so without it the app would install,
start, serve the calendar, and fail every scrape — and say nothing until someone
tried to sign in. Measured: the installed app's log gained exactly two lines,
both about the browser, and the fetch was a no-op because
`%LOCALAPPDATA%\ms-playwright` already held the revision the bundled Playwright
expects. Nothing was sent to LSM — the install path touches no session, no
database, and no port.

**Uninstalling** removes the folder, both shortcuts and the registry entry, and
**keeps your data**. The data directory is asked about separately, defaulting to
*keep*: it holds your sign-in and eleven months of history, and deleting it is
not something to do to somebody silently. Measured on a silent uninstall, where
no prompt can be answered — the data directory survived, which is the behaviour
the default button is there to give.

**It survived execution here, which the single-file build did not.** The three
Setup exes were still on disk at their full size after one of them had run. That
is a measurement of file survival, not a clean bill of health: the detection
console was not read, and `build.ps1`'s warning about the detections a build
produces still applies — signing the Setup exe is the build's *last* step, so
the unsigned binary the compiler writes is briefly on disk, and it is file
*writes* that get scored. Note that an installer is a self-extracting shape by
nature,
which is the shape that was removed when it was a PyInstaller one-file build.
It was not removed here. Whether that holds on another machine is the same open
question as for the folder build, with the same three answers under
[On this machine](#on-this-machine).

Inno Setup itself is a per-user install with no admin required — 6.7.3 here, at
`%LOCALAPPDATA%\Programs\Inno Setup 6`. Its compiler banner reads
"Non-commercial use only"; that is the licence it was downloaded under, and it
is worth knowing before this installer is put in front of anyone else.

### On this machine

This is a UofT-managed machine: Windows Defender is switched off in favour of
**SentinelOne** and **CrowdStrike Falcon**. That is the whole reason this
section exists. The measured history, which is *why* the app moved to the venv:

- **The single-file build was removed on execution.** Its shape — unpacking
  itself into `%TEMP%` and executing from there — is what reads as hostile. That
  attribution was never proven.
- **The folder build ran here.** `verify-build.ps1` passed end to end on
  2026-09-21 — every check green, 916 files present and unchanged after the
  dwell — and the app launched with its window, its web UI and Chromium all
  working.
- **The folder build is still flagged.** `build.ps1` produced two SentinelOne
  detections that day, `RotmanLSMCalendar.exe` under `build\` and again under
  `dist\`, one second apart as PyInstaller wrote then collected it, both
  "Suspicious Activity · Detected suspicious file". The console showed **0
  quarantined files**, the exe was still on disk afterwards at its full size,
  and `verify-build.ps1` then passed.

So the folder build is reported and left alone where the single-file build was
reported and taken. That was the position until now: *usable, but noisy*.

**The conclusion changed.** Reported-and-left-alone is one notch more permissive
than this is worth. The app runs from the venv, which writes no binary at all, so
there is nothing for an agent to score and no console line naming this account.
That is the supported path, and building the exe is not — nothing points at it
any more.

The detections already in the console stay there. They are a record of past
builds, not a live problem, and nothing here can or should remove them.

**What would still close the residual risk**, if the exe ever has to exist as a
shipped artefact. Detection here is reputation-driven, so a fresh binary with
a handful of users can be reported on first sight whatever it does. The folder
shape rules out the *dropper* pattern, not the first impression:

1. **Have the AV team allow-list it.** Key it on **path or publisher, not
   hash** — every rebuild changes the hash, so a hash-keyed exclusion expires
   with the next build. This is the cheap one, and it needs a request rather
   than a config change.
2. **Code-sign it.** Done since v1.1.2 with a self-signed certificate
   (`packaging/sign.ps1`): that buys a stable publisher identity — an agent
   can be told to trust it by certificate rather than by path — and local
   trust on machines that import the shipped `.cer`. It does **not** buy the
   reputation a CA-issued certificate carries, so whether the signed build
   still collects console lines is re-measured, not assumed. The CA-issued
   route remains the durable fix if the app ever moves beyond this account,
   and since 2023 an OV certificate needs a hardware token or HSM — a
   purchase and a physical object.
3. **Unpack it on one AV machine and read the quarantine count.** Not a fix,
   just the cheapest honest check that the build survives somewhere other than
   here.

None of the three is testable from this box, so none of them should be
promised. Worth naming in an allow-list request is the network surface, which
for this app is small and entirely one-directional: it binds **`127.0.0.1:8765`
only** (loopback, no authentication, no host override), and its outbound
traffic is HTTPS to `lsm.utoronto.ca` in a Playwright-driven Chromium, plus —
since v1.1.2 — HTTPS to `api.github.com` for the update check and, when an
update is accepted, the release's installer download (`app/updater.py`; the
Bearer token, when one is set, travels only on those requests; since v1.2.0
the requests name the public releases mirror
`camster91/rotman-lsm-calendar-releases`, not the private source repo). There is
no OAuth loopback listener here — that belongs to a different tool of mine, and
naming the wrong port in a security request is worse than naming none. AV teams
approve far faster when told what the binary talks to and why.

A draft of that request, with the measured facts and the placeholders to fill
in, is `packaging/allow-list-request.md`. Both files must agree; the request is
the one that gets sent.

The same caveat as above: a 90-second dwell is evidence, not a guarantee. If a
build ever stops working here, `verify-build.ps1` is the thing to run, and its
output names the stage that broke.
