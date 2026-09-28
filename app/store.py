"""
SQLite persistence for scraped bookings.

Every scrape replaces a rolling date window, so upserts are keyed on a
content hash (title + room + start + end) rather than a row id. That
keeps re-scrapes idempotent and lets a booking survive across runs
without accumulating duplicates.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator, Sequence

from app.config import (
    BACKFILL_MONTHS, CHANGES_KEEP_DAYS, DB_PATH, GROUPS_PATH, KEEP_DAYS,
    ROOM_GROUPS, log,
)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    uid         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    room        TEXT NOT NULL,
    start_iso   TEXT,
    end_iso     TEXT,
    date        TEXT,
    description TEXT,
    class_code  TEXT,
    location    TEXT NOT NULL DEFAULT '',
    cancelled   INTEGER NOT NULL DEFAULT 0,
    all_day     INTEGER NOT NULL DEFAULT 0,
    scraped_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_date  ON events(date);
CREATE INDEX IF NOT EXISTS idx_events_room  ON events(room);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_iso);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,          -- running | ok | empty | error | auth_required
    trigger      TEXT,                   -- startup | schedule | manual | heartbeat
    events_count INTEGER DEFAULT 0,
    date_from    TEXT,
    date_to      TEXT,
    message      TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
    room     TEXT PRIMARY KEY,
    display  TEXT,
    floor    TEXT,
    capacity INTEGER,
    panopto  INTEGER DEFAULT 0,
    seen_at  TEXT
);

-- What changed between scrapes. Append-only, and deliberately denormalised:
-- a 'removed' row describes a booking that no longer exists in `events`, so
-- the feed cannot join back to it and the payload is copied in. There is no
-- foreign key to scrape_runs either — an audit log should not be coupled to
-- a table a retention policy might trim.
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,                 -- scrape_runs.id; NULL if unknown
    trigger     TEXT NOT NULL,           -- startup | schedule | manual | backfill
    kind        TEXT NOT NULL,           -- added | removed
    uid         TEXT NOT NULL,           -- content hash; may recur over time
    title       TEXT NOT NULL DEFAULT '',
    room        TEXT NOT NULL DEFAULT '',
    start_iso   TEXT,
    end_iso     TEXT,
    date        TEXT,
    description TEXT NOT NULL DEFAULT '',
    class_code  TEXT NOT NULL DEFAULT '',
    all_day     INTEGER NOT NULL DEFAULT 0,
    detected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_detected ON changes(detected_at);
CREATE INDEX IF NOT EXISTS idx_changes_date     ON changes(date);
CREATE INDEX IF NOT EXISTS idx_changes_run      ON changes(run_id);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _conn() -> sqlite3.Connection:
    """One connection per thread — Flask and the scheduler both touch this."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate(conn: sqlite3.Connection) -> None:
    """Retrofit columns onto an existing database.

    CREATE TABLE IF NOT EXISTS silently does nothing when the table is
    already there, so a new column has to be added explicitly.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "all_day" not in have:
        conn.execute(
            "ALTER TABLE events ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0"
        )
        log.info("migrated: added events.all_day")
    if "location" not in have:
        # The parser has always built a building-qualified location, but this
        # column is what kept it: without it, rows written before it stored
        # the bare room number as the location. Old rows keep that fallback.
        conn.execute(
            "ALTER TABLE events ADD COLUMN location TEXT NOT NULL DEFAULT ''"
        )
        log.info("migrated: added events.location")

    # Same retrofit for the feed. Without this, a database created before the
    # column existed keeps the old shape and every INSERT that names it fails —
    # which is every scrape, not just the ones that found a change.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(changes)")}
    if "all_day" not in cols:
        conn.execute(
            "ALTER TABLE changes ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0"
        )
        log.info("migrated: added changes.all_day")


def init_db() -> None:
    with _tx() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    log.info("database ready at %s", DB_PATH)


# ── Event identity ───────────────────────────────────────────────────────

def event_uid(ev: dict[str, Any]) -> str:
    key = "|".join(
        str(ev.get(k) or "")
        for k in ("title", "room", "start", "end")
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ── Writes ───────────────────────────────────────────────────────────────

def replace_events(
    events: Iterable[dict[str, Any]],
    date_from: str,
    date_to: str,
    run_id: int | None = None,
    trigger: str = "manual",
    record_changes: bool = True,
    complete: bool = True,
) -> int:
    """
    Upsert a scraped window and record what changed in it.

    Deletes rows inside [date_from, date_to] that this scrape did not
    report — that is how cancellations disappear — then upserts what we
    did get. Rows outside the window are left alone so a narrow scrape
    never wipes a wider one.

    The delta is worked out against what the window already held, captured
    immediately before that delete: anything reported but not stored is an
    add, anything stored but not reported is a removal. Both are written to
    the `changes` table, so a booking that vanishes leaves a trace instead of
    silently disappearing. Comparing against the window (rather than against
    the whole table) is what makes a narrow re-scrape of wider data report no
    adds at all.

    `record_changes=False` is the backfill path: a first observation is not a
    change, and a year of them would bury the real feed.

    `complete=False` says the caller could not establish that what it read was
    the *whole* report. The absence of a booking is then no evidence that the
    booking is gone, so nothing is deleted and no removal is recorded — but
    adds and updates still land, because seeing a booking is evidence it
    exists whatever else was missed. This is the same principle as the empty
    guard below, at the resolution the caller can actually establish: the
    guard catches "we saw nothing", this catches "we saw some, and do not know
    whether that was all". A partial report that deletes is the worse failure
    of the two, because it destroys bookings that are still real and logs them
    as cancellations, so `scrape` marks a report incomplete whenever it reads
    the rendered page instead of the export.

    Note that a booking whose time is edited gets a different uid, so it is
    recorded as one removal and one addition rather than a "move". The report
    carries no booking id, so a move cannot be told apart from a cancel plus a
    rebook; the feed does not pretend otherwise.
    """
    events = list(events)
    now = datetime.now().isoformat()
    seen: set[str] = set()
    rows = []
    by_uid: dict[str, dict[str, Any]] = {}

    for ev in events:
        uid = event_uid(ev)
        seen.add(uid)
        by_uid.setdefault(uid, ev)
        rows.append((
            uid,
            ev.get("title") or "",
            ev.get("room") or "",
            ev.get("start"),
            ev.get("end"),
            (ev.get("start") or "")[:10] or None,
            ev.get("description") or "",
            ev.get("class_code") or "",
            ev.get("location") or "",
            1 if ev.get("cancelled") else 0,
            1 if ev.get("all_day") else 0,
            now,
        ))

    with _tx() as conn:
        iso_from = _to_iso_date(date_from)
        iso_to = _to_iso_date(date_to)
        have_window = bool(iso_from and iso_to)

        before: dict[str, sqlite3.Row] = {}
        if have_window:
            before = {
                r["uid"]: r
                for r in conn.execute(
                    # all_day is here for the feed, not the reconcile: a removal
                    # row is built from this snapshot, and _change_rows reads
                    # the flag off it. Leaving it out raises IndexError on the
                    # first removal of any scrape.
                    "SELECT uid, title, room, start_iso, end_iso, date, "
                    "  description, class_code, all_day FROM events "
                    "WHERE date BETWEEN ? AND ?",
                    (iso_from, iso_to),
                )
            }
        else:
            # Without a window there is nothing to reconcile against. Do not
            # fall back to "everything is an add" — that would log a mass
            # addition that never happened.
            log.warning(
                "window %r → %r is unparseable — reconcile and change "
                "detection skipped", date_from, date_to,
            )

        added = [u for u in seen if u not in before] if have_window else []
        removed = [u for u in before if u not in seen] if have_window else []

        # An empty report for a window that currently holds bookings is far
        # more likely to be a failed render than a genuine mass cancellation.
        # Acting on it would empty the window and log a removal burst that
        # never happened, and the daily scrape runs again anyway.
        wipe = have_window and not seen and bool(before)
        if wipe:
            log.warning(
                "refusing to reconcile: an empty report would delete %d "
                "bookings in %s → %s", len(before), date_from, date_to,
            )
            added, removed = [], []

        # A report the caller could not establish as whole. Keep the adds --
        # seeing a booking says it exists -- and drop every deletion, because
        # not seeing one says nothing.
        if not complete:
            if removed:
                log.warning(
                    "not reconciling removals: %s → %s was read from the "
                    "rendered page, so the %d bookings it did not report may "
                    "still exist", date_from, date_to, len(removed),
                )
            removed = []

        if record_changes and (added or removed):
            conn.executemany(
                """INSERT INTO changes
                     (run_id, trigger, kind, uid, title, room, start_iso,
                      end_iso, date, description, class_code, all_day,
                      detected_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                _change_rows(added, removed, by_uid, before,
                             run_id, trigger, now),
            )

        # `complete` gates the delete as well as the feed: `removed` is what
        # the feed reads, but the DELETE is what actually destroys the row.
        if have_window and not wipe and complete:
            placeholders = ",".join("?" for _ in seen) or "''"
            conn.execute(
                f"DELETE FROM events WHERE date BETWEEN ? AND ? "
                f"AND uid NOT IN ({placeholders})",
                (iso_from, iso_to, *seen),
            )
        conn.executemany(
            """INSERT INTO events
                 (uid, title, room, start_iso, end_iso, date, description,
                  class_code, location, cancelled, all_day, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uid) DO UPDATE SET
                 title=excluded.title,
                 description=excluded.description,
                 class_code=excluded.class_code,
                 location=excluded.location,
                 cancelled=excluded.cancelled,
                 all_day=excluded.all_day,
                 scraped_at=excluded.scraped_at""",
            rows,
        )
    log.info("stored %d events (%s → %s) +%d -%d",
             len(rows), date_from, date_to, len(added), len(removed))
    return len(rows)


def _change_rows(
    added: list[str],
    removed: list[str],
    by_uid: dict[str, dict[str, Any]],
    before: dict[str, sqlite3.Row],
    run_id: int | None,
    trigger: str,
    now: str,
) -> list[tuple[Any, ...]]:
    """One feed row per change, carrying everything needed to render it."""
    out: list[tuple[Any, ...]] = []
    for uid in added:
        ev = by_uid.get(uid) or {}
        out.append((
            run_id, trigger, "added", uid,
            ev.get("title") or "", ev.get("room") or "",
            ev.get("start"), ev.get("end"),
            (ev.get("start") or "")[:10] or None,
            ev.get("description") or "", ev.get("class_code") or "",
            1 if ev.get("all_day") else 0,
            now,
        ))
    for uid in removed:
        r = before[uid]
        out.append((
            run_id, trigger, "removed", uid,
            r["title"] or "", r["room"] or "",
            r["start_iso"], r["end_iso"], r["date"],
            r["description"] or "", r["class_code"] or "",
            1 if r["all_day"] else 0,
            now,
        ))
    return out


def replace_rooms(rooms: Iterable[dict[str, Any]]) -> None:
    now = datetime.now().isoformat()
    rows = [
        (
            r.get("room"),
            r.get("display"),
            r.get("floor"),
            r.get("capacity"),
            1 if r.get("panopto") else 0,
            now,
        )
        for r in rooms
        if r.get("room")
    ]
    if not rows:
        return
    with _tx() as conn:
        conn.executemany(
            """INSERT INTO rooms (room, display, floor, capacity, panopto, seen_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(room) DO UPDATE SET
                 display=excluded.display, floor=excluded.floor,
                 capacity=excluded.capacity, panopto=excluded.panopto,
                 seen_at=excluded.seen_at""",
            rows,
        )


def prune(
    keep_days_back: int = KEEP_DAYS,
    changes_keep_days: int = CHANGES_KEEP_DAYS,
) -> dict[str, int]:
    """Drop bookings — and change-feed rows about them — once they age out.

    Change rows are aged by the *booking's* date, not by when we noticed the
    change, so the feed's horizon tracks the calendar's: a removal recorded in
    March about an August booking survives until August ages out, and a change
    about a booking we no longer keep goes with it. Ageing them by
    `detected_at` instead would drop feed entries for bookings still on the
    calendar.
    """
    # The `date` column holds local Toronto days — every write into it is
    # datetime.now()/today() — so the horizon is computed on the same clock.
    # SQLite's date('now') is UTC, and for a stretch of every Toronto
    # evening the two disagree about which day it is; on the wrong side of
    # that the horizon moved a day and rows left a day early or hung on a
    # day late. There is enough slack over the backfill reach that nothing
    # was lost, but the slack is a cushion, not a licence to keep two
    # clocks in one comparison.
    cutoff = (date.today() - timedelta(days=int(keep_days_back))).isoformat()
    change_cutoff = (
        date.today() - timedelta(days=int(changes_keep_days))
    ).isoformat()
    with _tx() as conn:
        events = conn.execute(
            "DELETE FROM events WHERE date IS NOT NULL AND date < ?",
            (cutoff,),
        ).rowcount
        # `date IS NOT NULL` keeps an undated row forever rather than
        # silently dropping it.
        changes = conn.execute(
            "DELETE FROM changes WHERE date IS NOT NULL AND date < ?",
            (change_cutoff,),
        ).rowcount
    if events or changes:
        log.info("pruned %d events, %d changes", events, changes)
    return {"events": events, "changes": changes}


# ── Runs ─────────────────────────────────────────────────────────────────

def start_run(trigger: str) -> int:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO scrape_runs (started_at, status, trigger) VALUES (?,?,?)",
            (datetime.now().isoformat(), "running", trigger),
        )
        return int(cur.lastrowid)


def finish_run(
    run_id: int,
    status: str,
    events_count: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    message: str | None = None,
) -> None:
    with _tx() as conn:
        conn.execute(
            """UPDATE scrape_runs
                  SET finished_at=?, status=?, events_count=?,
                      date_from=?, date_to=?, message=?
                WHERE id=?""",
            (
                datetime.now().isoformat(), status, events_count,
                date_from, date_to, (message or "")[:500], run_id,
            ),
        )


def last_run(
    exclude_triggers: Sequence[str] = ("backfill",),
) -> dict[str, Any] | None:
    """The newest finished run — by default the newest finished *scrape*.

    A backfill is a run but not a scrape. Counting it here would make it read
    as recent activity (suppressing the startup scrape, which the backfill
    does not refresh) and would report a backfill's finish time as
    `last_scrape_*` next to a different job's message.
    """
    sql = "SELECT * FROM scrape_runs WHERE status != 'running'"
    args: list[Any] = []
    if exclude_triggers:
        marks = ",".join("?" for _ in exclude_triggers)
        sql += f" AND (trigger IS NULL OR trigger NOT IN ({marks}))"
        args += list(exclude_triggers)
    row = _conn().execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return dict(row) if row else None


def last_backfill() -> dict[str, Any] | None:
    row = _conn().execute(
        "SELECT * FROM scrape_runs WHERE trigger = 'backfill' "
        "AND status != 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _ddmmyyyy(s: str | None) -> date | None:
    """Parse the dd/mm/yyyy a backfill window is written in."""
    try:
        return datetime.strptime(s or "", "%d/%m/%Y").date()
    except ValueError:
        return None


def _first_of_month_months_ago(months: int, when_iso: str | None) -> date:
    """First day of the month `months` before the one `when_iso` falls in."""
    try:
        when = datetime.fromisoformat(when_iso or "").date()
    except ValueError:
        when = date.today()
    m = when.month - 1 - months          # 0-based, may go negative
    return date(when.year + m // 12, m % 12 + 1, 1)


def backfill_done() -> bool:
    """Has the one-time history fill already landed?

    The gate for the automatic first-run backfill. A run only counts when it
    finished `ok`, stored something, *and reached far enough back*.

    The reach is the part that is easy to get wrong. Asking only "did a
    backfill succeed" lets `--backfill --months 6` retire the fill for good:
    the run is honest and clean, it just never fetched the six oldest months,
    and nothing afterwards reaches them — the daily window is four months wide
    and the automatic fill will not run again. So the run's own `date_from`
    (the oldest day it covered, written by _do_backfill) has to clear the floor
    that BACKFILL_MONTHS implies.

    Measured against the run's `started_at` rather than against today, because
    a reach that was complete when it ran must stay complete. Compared with
    today, the floor would drift forward a month at a time until a finished
    fill un-settled itself and refetched eleven months to no purpose.

    `events_count > 0` closes the degenerate case: a run asked for a reach that
    yields no windows at all finishes `ok` having fetched nothing, and must not
    retire the fill for good.
    """
    rows = _conn().execute(
        "SELECT started_at, date_from FROM scrape_runs "
        "WHERE trigger = 'backfill' AND status = 'ok' AND events_count > 0"
    ).fetchall()
    for row in rows:
        # Any one qualifying run settles it, rather than only the newest. A
        # later short repair run — `--backfill --months 6` on an already-filled
        # install — must not un-settle a fill that genuinely reached the floor.
        reached = _ddmmyyyy(row["date_from"])
        if reached is None:
            continue
        if reached <= _first_of_month_months_ago(BACKFILL_MONTHS,
                                                 row["started_at"]):
            return True
    return False


# ── Change feed ──────────────────────────────────────────────────────────

def _like(q: str) -> str:
    """A LIKE pattern for "contains q", with q's own wildcards defused.

    LIKE has two wildcards and the search box has neither. `%` and `_` arrive
    from the front end as ordinary characters, so an unescaped pattern reads
    them as "anything" and "any one character": typing `_` into the search box
    returned every booking in the window, and a search for `L1060_A` would
    match `L1060XA` as though the underscore were not there. Neither is
    exotic -- `_` is what a room code uses.

    The backslash is escaped *first*. Doing it after the others would escape
    the escapes this had just added, turning a literal backslash-then-`%` into
    a live wildcard again.

    Each call site must pair this with `ESCAPE '\\'` in the SQL. SQLite's LIKE
    does not treat the backslash as an escape character on its own -- the
    clause is what gives it that meaning, and without it the pattern is
    searched for literally, backslashes and all, and matches nothing.
    """
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def get_changes(
    since: str | None = None,
    kind: str | None = None,
    room: str | None = None,
    run_id: int | None = None,
    include_backfill: bool = False,
    limit: int = 200,
    q: str | None = None,
    rooms: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Reported additions and removals, newest first.

    `since` compares `detected_at`, which is ISO-8601 and so sorts
    lexicographically — the same trick the date filters use. `kind` is only
    applied when it is one of the two real values, so a junk value widens the
    filter rather than silently emptying it.

    `q` and `rooms` mirror get_events so the Changes view narrows with the same
    vocabulary the calendar does. The filtering is deliberately here rather than
    in the browser: `limit` is applied before any client-side filter, so a feed
    narrowed in JS could report "no changes for room 142" about the newest 200
    rows while older ones exist.
    """
    sql = ["SELECT * FROM changes WHERE 1=1"]
    args: list[Any] = []
    if not include_backfill:
        sql.append("AND trigger != 'backfill'")
    if since:
        sql.append("AND detected_at > ?")
        args.append(since)
    if kind in ("added", "removed"):
        sql.append("AND kind = ?")
        args.append(kind)
    if room:
        sql.append("AND room = ?")
        args.append(room)
    if rooms:
        # Same trap get_events documents: an empty selection means "no
        # restriction", not "match nothing". The caller must omit the argument
        # rather than send an empty list.
        sql.append(f"AND room IN ({','.join('?' for _ in rooms)})")
        args += list(rooms)
    if q:
        sql.append("AND (title LIKE ? ESCAPE '\\' OR room LIKE ? ESCAPE '\\'"
                   " OR description LIKE ? ESCAPE '\\')")
        args += [_like(q), _like(q), _like(q)]
    if run_id is not None:
        sql.append("AND run_id = ?")
        args.append(run_id)
    sql.append("ORDER BY id DESC LIMIT ?")
    args.append(limit)

    rows = _conn().execute(" ".join(sql), args).fetchall()
    return [_row_to_change(r) for r in rows]


def _row_to_change(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "trigger": r["trigger"],
        "kind": r["kind"],
        "uid": r["uid"],
        "title": r["title"],
        "room": r["room"],
        "start": r["start_iso"],
        "end": r["end_iso"],
        "date": r["date"],
        "description": r["description"] or "",
        "class_code": r["class_code"] or "",
        # The column is no use unless it reaches the wire: the feed branches on
        # this to print "All day" instead of the 00:00 → 23:00 a service block
        # actually carries.
        "all_day": bool(r["all_day"]),
        "detected_at": r["detected_at"],
    }


# ── Reads ────────────────────────────────────────────────────────────────

def rooms_present() -> list[str]:
    """Every room with at least one stored booking, distinct and sorted.

    /api/today seeds its per-room answer from this rather than from the
    rows of one day: a room with nothing booked that day is free, and it
    is exactly the room the question is about — seeding from a narrowed
    read would drop every empty room from the answer. One indexed scan;
    the room list is tiny (~91) even though the table is not (~29k rows).
    """
    rows = _conn().execute(
        "SELECT DISTINCT room FROM events WHERE cancelled = 0 AND room <> ''"
        " ORDER BY room"
    ).fetchall()
    return [r["room"] for r in rows]


def suggest_rooms(q: str) -> list[str]:
    """Distinct rooms whose name contains q, in one SQL scan.

    /api/autocomplete runs on every keystroke, and reading the whole table
    into Python for each one was the cost — SQL does the same scan without
    materialising a year of rows. q's own wildcards are defused by _like,
    as everywhere: `_` is what a room code uses.
    """
    rows = _conn().execute(
        "SELECT DISTINCT room FROM events"
        " WHERE cancelled = 0 AND room LIKE ? ESCAPE '\\'"
        " ORDER BY room", (_like(q),)
    ).fetchall()
    return [r["room"] for r in rows]


def suggest_titles(q: str) -> list[str]:
    """Distinct raw titles containing q, for the autocomplete to parse.

    Returns the title exactly as stored; the route applies split_title —
    the suggestion is what the screen shows, and suggesting "208/CIBC.1"
    for a screen that prints "CIBC.1" would put text in the search box the
    results then display without.
    """
    rows = _conn().execute(
        "SELECT DISTINCT title FROM events"
        " WHERE cancelled = 0 AND title LIKE ? ESCAPE '\\'"
        " ORDER BY title", (_like(q),)
    ).fetchall()
    return [r["title"] for r in rows]


def get_events(
    room: str | None = None,
    date: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_cancelled: bool = False,
    # Rows come back ORDER BY start_iso *ascending*, so a limit below the row
    # count keeps the oldest bookings and silently drops the upcoming ones —
    # /api/today would report every room free. A year of history is ~24k rows,
    # so the cap has to clear that with room to spare.
    limit: int = 200_000,
    rooms: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    sql = ["SELECT * FROM events WHERE 1=1"]
    args: list[Any] = []
    if not include_cancelled:
        sql.append("AND cancelled = 0")
    if room:
        sql.append("AND room = ?")
        args.append(room)
    if rooms:
        # A room filter with nothing selected means "no restriction", not
        # "match nothing" — the UI sends an empty set when every room is on.
        sql.append(f"AND room IN ({','.join('?' for _ in rooms)})")
        args += list(rooms)
    if date:
        sql.append("AND date = ?")
        args.append(date)
    if date_from:
        sql.append("AND date >= ?")
        args.append(date_from)
    if date_to:
        sql.append("AND date <= ?")
        args.append(date_to)
    if q:
        sql.append("AND (title LIKE ? ESCAPE '\\' OR room LIKE ? ESCAPE '\\'"
                   " OR description LIKE ? ESCAPE '\\')")
        args += [_like(q), _like(q), _like(q)]
    sql.append("ORDER BY start_iso LIMIT ?")
    args.append(limit)

    rows = _conn().execute(" ".join(sql), args).fetchall()
    return [_row_to_event(r) for r in rows]


def _row_to_event(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "title": r["title"],
        "room": r["room"],
        "start": r["start_iso"],
        "end": r["end_iso"],
        "date": r["date"],
        "description": r["description"] or "",
        "class_code": r["class_code"] or "",
        # The building-qualified string the parser builds; empty for rows
        # written before the column existed, so consumers fall back to room.
        "location": r["location"] or "",
        "cancelled": bool(r["cancelled"]),
        "all_day": bool(r["all_day"]),
    }


def stats() -> dict[str, Any]:
    """Counts and date range of the stored events, recomputed only on change.

    /api/status calls this on every poll — every 60 s per open page, every
    few seconds while the worker is busy — and the answer only moves when the
    events table does. So the answer is kept per connection (connections are
    per thread) with a key that moves on any write: `total_changes` counts
    this connection's own writes, and `PRAGMA data_version` moves when any
    *other* connection commits (measured: it does not move for our own). A
    write made through another connection — the scheduler's thread, or a
    test's raw sqlite3 handle — therefore invalidates it too, which a cache
    cleared only by this module's writers would miss.
    """
    conn = _conn()
    key = (conn.total_changes,
           conn.execute("PRAGMA data_version").fetchone()[0])
    cached = getattr(_local, "stats", None)
    if cached and cached[0] is conn and cached[1] == key:
        return dict(cached[2])

    total = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    rooms = conn.execute("SELECT COUNT(DISTINCT room) c FROM events").fetchone()["c"]
    rng = conn.execute(
        "SELECT MIN(date) a, MAX(date) b FROM events WHERE date IS NOT NULL"
    ).fetchone()
    result = {
        "total_events": total,
        "rooms": rooms,
        "date_from": rng["a"],
        "date_to": rng["b"],
    }
    _local.stats = (conn, key, result)
    return dict(result)


# ── Key/value ────────────────────────────────────────────────────────────

def get_kv(key: str, default: Any = None) -> Any:
    row = _conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        # Not silent, like load_groups: a corrupt row is a persisted decision
        # (the updater's skip, its last-check stamp) disappearing, and the
        # user who skipped a version only to see it offered again deserves
        # a line in the log explaining why.
        log.warning("kv row '%s' is corrupt — returning the default", key)
        return default


def set_kv(key: str, value: Any) -> None:
    with _tx() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


# ── Room metadata / groups ───────────────────────────────────────────────

def load_groups() -> dict[str, list[str]]:
    if GROUPS_PATH.exists():
        try:
            data = json.loads(GROUPS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except (ValueError, OSError):
            log.warning("room_groups.json unreadable — using defaults")
    # A fresh set of lists, not a shallow copy: dict() would share the list
    # objects with the module constant, so an editor mutating a group in place
    # would rewrite app.config.ROOM_GROUPS for the life of the process.
    return {name: list(rooms) for name, rooms in ROOM_GROUPS.items()}


def save_groups(groups: dict[str, list[str]]) -> None:
    """Replace the group set, atomically.

    Written to a sibling temp file and moved into place. A plain write that is
    interrupted — a crash, a full disk — leaves truncated JSON behind, and
    load_groups rejects unparseable JSON in favour of the built-in defaults, so
    the failure mode of a half-write is losing *every* user group at once.
    os.replace is atomic within a filesystem, so a reader sees the old file or
    the new one, never a partial one.
    """
    tmp = GROUPS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(groups, indent=2), encoding="utf-8")
    os.replace(tmp, GROUPS_PATH)


def valid_groups(groups: Any) -> bool:
    """Is this a usable group map? A dict of name -> list of room numbers."""
    if not isinstance(groups, dict) or not groups:
        return False
    for name, rooms in groups.items():
        if not isinstance(name, str) or not name.strip():
            return False
        # A comma cannot survive the shared-link vocabulary: the writers
        # comma-join group names into groups= and parseFilters splits on the
        # same character, so a name with one is silently dropped on read-back
        # — the whole map is rejected because one bad name must not slip in
        # beside a save the user cannot see the reason for.
        if "," in name:
            return False
        if not isinstance(rooms, list):
            return False
        if not all(isinstance(r, str) and r.strip() for r in rooms):
            return False
    return True


# ── Saved filter presets ─────────────────────────────────────────────────

PRESETS_KEY = "filter_presets"
PRESET_LIMIT = 20


def load_presets() -> list[dict[str, Any]]:
    """Saved filter sets, newest last. Never raises — a bad blob reads empty."""
    raw = get_kv(PRESETS_KEY, [])
    if not isinstance(raw, list):
        log.warning("presets: expected a list, got %s — ignoring", type(raw))
        return []
    out = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            filters = item.get("filters")
            out.append({
                "name": item["name"],
                "filters": filters if isinstance(filters, dict) else {},
            })
    return out[:PRESET_LIMIT]


def save_preset(name: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Add a preset, or overwrite the one with the same name. Name-insensitive."""
    presets = [p for p in load_presets() if p["name"] != name]
    presets.append({"name": name, "filters": filters})
    # Oldest first, so the newest arrival is the one dropped at the cap.
    presets = presets[-PRESET_LIMIT:]
    set_kv(PRESETS_KEY, presets)
    return presets


def delete_preset(name: str) -> list[dict[str, Any]]:
    presets = [p for p in load_presets() if p["name"] != name]
    set_kv(PRESETS_KEY, presets)
    return presets


# ── Update-check state ────────────────────────────────────────────────────

UPDATE_KEY = "update_state"


def load_update_state() -> dict[str, Any]:
    """When the updater last looked, and which version it was told to skip.

    In the database rather than the orchestrator's memory because the 24 h
    cadence is a fact about the *machine*, not the process: a restart must
    not reset the clock, or an app left on overnight would check on every
    relaunch instead of once a day. Never raises — a bad blob reads empty
    and the next check simply happens sooner.
    """
    raw = get_kv(UPDATE_KEY, {})
    if not isinstance(raw, dict):
        log.warning("update_state: expected a dict, got %s — ignoring", type(raw))
        return {"last_check": None, "skipped": None}
    return {
        "last_check": raw.get("last_check") if isinstance(raw.get("last_check"), str) else None,
        "skipped": raw.get("skipped") if isinstance(raw.get("skipped"), str) else None,
    }


def save_update_state(state: dict[str, Any]) -> None:
    set_kv(UPDATE_KEY, state)


def _to_iso_date(ddmmyyyy: str | None) -> str | None:
    """'01/04/2026' → '2026-04-01'."""
    if not ddmmyyyy:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(ddmmyyyy, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
