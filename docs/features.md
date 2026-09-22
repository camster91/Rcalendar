# Features

An **index of what the app does**, one line per feature, with the file that
implements it. This is deliberately not a second description of anything: where
a feature has a subtlety worth understanding, `README.md` is the account and
this file points at the section rather than restating it. Two descriptions of
one behaviour drift, and nothing notices when they do — the same reason
`app/avail.py` holds the only free/busy predicate.

The app is **read-only**. It never books, changes or cancels anything at LSM,
and nothing here writes to the university's system.

## Getting the data

| Feature | Where |
|---|---|
| Daily scrape at 06:00 local (`SCRAPE_TIME`) | `app/scheduler.py`, `app/scrape.py` |
| Report window: previous month → end of next month (`SCRAPE_MONTHS_BACK` / `_AHEAD`) | `app/config.py` |
| Scrape at launch when the last one is stale (`SCRAPE_ON_START`) | `app/main.py` |
| Scrape Now — the tray item and the ⟳ button | `app/scheduler.py` |
| Session heartbeat every 4 hours (`HEARTBEAT_HOURS`) to keep the login warm | `app/scheduler.py` |
| One-time history fill: 12 months, first launch with a live session, resumable | README → *History and changes* |
| Repair path for a cut-short fill: `python -m app.main --backfill [--months N]` | `app/main.py` |
| The report's own window is read back and checked before its data is used | README → *Notes and limits* |
| Only a **whole** report is reconciled: read off the rendered page, it adds and updates but deletes nothing | `app/scrape.py`, `app/store.py` |
| Room exclusions (134-series study rooms) filtered **before storage** | `app/config.py` |
| Change feed: what appeared or disappeared between scrapes | `app/store.py`, Changes view |
| Search reads `%` and `_` as the characters they are, not as LIKE wildcards | `app/store.py` |
| No DPAPI means **no** cookie snapshot, never a cleartext one — the refusal is logged | `app/dpapi.py` |
| Rolling-year retention (`KEEP_DAYS`, 410 days) | `app/config.py`, `app/store.py` |

## The session

| Feature | Where |
|---|---|
| **No password stored anywhere** — a Chromium profile holds the login | `app/session.py` |
| Interactive SSO + Duo sign-in (tray → *Sign in to LSM*) | `app/session.py` |
| Headless session probe | `app/session.py` |
| Sign out — clears the profile's cookies, then measures the result | `app/session.py`, `POST /api/logout` |
| DPAPI-encrypted cookie snapshot (`session.bin`), re-injected after a hard shutdown | `app/dpapi.py`, `app/session.py` |
| Bound to `127.0.0.1` only, with no host override and no authentication | `app/config.py` |

## Views

| View | What it is | Where |
|---|---|---|
| 📅 Month | Grid of the month | `web/calendar.html` |
| 📊 Week | Grid of the week | `web/calendar.html` |
| 📋 Today | One day, hour by hour | `web/calendar.html` |
| 🔀 Changes | Feed of additions and removals between scrapes | `web/calendar.html` |
| List | Dense table of every matching booking, for scanning | `web/list.html` |

## Filters

Applied the same way on both pages: **AND across filters, OR within one**.

| Filter | Rule | Where |
|---|---|---|
| Rooms | Chips, OR'd | `web/calendar.html`, `web/list.html` |
| Groups | Editable, saved to `room_groups.json` in one atomic write | `app/store.py`, `web/calendar.html` |
| Search | Free text over title, room **and description** (substring, case-insensitive) | both pages |
| Floor | **Strict** — a floor is a partition, so an unknown floor is not on the one you picked; a *No floor* chip makes that selectable | README → *Filters* |
| Seats ≥ N | **Lenient** — a room with no recorded capacity passes, because a threshold is not a partition | README → *Filters* |
| Panopto | The 13 rooms the report flags for capture (`PANOPTO_ROOMS`) | `app/config.py` |
| Free at a time | "free from 14:00 for 60 min", answered **per day**, by `app/avail.py` | `app/avail.py`, `GET /api/today` |
| Free Right Now | The same predicate as free-at, so the two cannot disagree. Grouped into *free all day* and *free until HH:MM*, stamped with when it was measured, and each row a button that narrows the filter to that room | `app/avail.py`, `web/calendar.html` |

## The Active filters row

The row beside *Rooms* is **derived from the filter state**, never accumulated
as a history of what was added. That is what makes a stale chip impossible by
construction: `readURL` and a saved filter both replace the whole filter at
once, and a history would leave the previous filter's chips on screen naming a
filter that is no longer on — which is what happened, and removing one then
acted on the new filter instead of the one it named. The ⚙ badge counts the
same list, so it cannot deny filters it does not know about; it counted four of
seven.

## The free-room list

Grouped rather than one run of rooms, because of what the data actually holds:
of 91 rooms, 58 were free, 49 of those had a booking later the same day and 9
were free all day — so *free* on its own is not the useful word. Each group is
sorted by what makes the offers different (longest free first), and the section
heading carries the strong count. The panel is stamped *as of HH:MM*, because
`/api/today` answers for the moment it is called and a list fetched at boot is
not still "Right Now" after a morning in a background tab.

## Exports

| Export | Notes |
|---|---|
| `.ics` | Every booking gets its own UID, derived from the store's event identity — a shared UID would make a calendar client keep one and silently drop the other |
| JSON | The same rows the page is showing |
| Both | Honour the filters, including a group name, which the server resolves into rooms |

## Saved filters

Captured filters only — not the view or the date, because a preset that only
makes sense on one day is useless tomorrow. Stored in the database rather than
browser storage, so they follow the app rather than one browser profile. At
most **20** are kept; a name is **1–60 characters**, checked on the server as
well as in the panel; saving over a name overwrites it. (`app/store.py`:
`PRESET_LIMIT`, `save_preset`.)

## The URL

The filters, the view and the date are written with `replaceState`, so **Copy
link** hands over the exact screen. `replaceState` rather than `pushState` is
deliberate — see README → *Filters* for what it costs and what `Clear all` is
for.

## Window and tray

| Feature | Where |
|---|---|
| Closing the window hides it; the app keeps scraping | `app/main.py` |
| Tray menu: Open Calendar, List View, a live session label, Scrape Now, Sign in to LSM, Check Session, Open Data Folder, Open in Browser, Quit | `app/main.py` |
| Startup shortcut — points at the venv, and there is no exe fallback | `packaging/install-autostart.ps1` |
| Nine keyboard shortcuts, with two gating rules | README → *Keyboard* |

## Running it, and checking it

| Thing | Where |
|---|---|
| The supported path: `.venv\Scripts\python.exe -m app.main` | README → *Running it* |
| Fetching Chromium on a fresh machine: `--install-browser` | `app/main.py` |
| The unsupported path: the packaged exe, kept as a record | README → *Not the supported path* |
| The installer: per-user, no UAC, with the browser fetch as a task | `packaging/installer.iss`, README → *The installer* |
| Health check end to end: `python -m app.main --selftest` | `app/main.py` |
| Seven suites, 675 assertions, no network and no UofT SSO | `tests/`, README → *Tests* |
| Accessibility: every control named, toggles not colour-alone, nothing focusable removed, suggestions and chips reachable by keyboard | `tests/test_web.py` |
| The same seven suites in CI | `.github/workflows/tests.yml` |
