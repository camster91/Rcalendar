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
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator, Sequence

from app.config import DB_PATH, GROUPS_PATH, ROOM_GROUPS, ROOMS_PATH, log

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

def replace_events(events: Iterable[dict[str, Any]], date_from: str, date_to: str) -> int:
    """
    Upsert a scraped window.

    Deletes rows inside [date_from, date_to] that this scrape did not
    report — that is how cancellations disappear — then upserts what we
    did get. Rows outside the window are left alone so a narrow scrape
    never wipes a wider one.
    """
    events = list(events)
    now = datetime.now().isoformat()
    seen: set[str] = set()
    rows = []

    for ev in events:
        uid = event_uid(ev)
        seen.add(uid)
        rows.append((
            uid,
            ev.get("title") or "",
            ev.get("room") or "",
            ev.get("start"),
            ev.get("end"),
            (ev.get("start") or "")[:10] or None,
            ev.get("description") or "",
            ev.get("class_code") or "",
            1 if ev.get("cancelled") else 0,
            1 if ev.get("all_day") else 0,
            now,
        ))

    with _tx() as conn:
        iso_from = _to_iso_date(date_from)
        iso_to = _to_iso_date(date_to)
        if iso_from and iso_to:
            placeholders = ",".join("?" for _ in seen) or "''"
            conn.execute(
                f"DELETE FROM events WHERE date BETWEEN ? AND ? "
                f"AND uid NOT IN ({placeholders})",
                (iso_from, iso_to, *seen),
            )
        conn.executemany(
            """INSERT INTO events
                 (uid, title, room, start_iso, end_iso, date, description,
                  class_code, cancelled, all_day, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uid) DO UPDATE SET
                 title=excluded.title,
                 description=excluded.description,
                 class_code=excluded.class_code,
                 cancelled=excluded.cancelled,
                 all_day=excluded.all_day,
                 scraped_at=excluded.scraped_at""",
            rows,
        )
    log.info("stored %d events (%s → %s)", len(rows), date_from, date_to)
    return len(rows)


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


def prune(keep_days_back: int = 120) -> int:
    """Drop bookings whose date is well in the past."""
    with _tx() as conn:
        cur = conn.execute(
            "DELETE FROM events WHERE date IS NOT NULL "
            "AND date < date('now', ?)",
            (f"-{int(keep_days_back)} days",),
        )
        return cur.rowcount


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


def last_run() -> dict[str, Any] | None:
    row = _conn().execute(
        "SELECT * FROM scrape_runs WHERE status != 'running' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Reads ────────────────────────────────────────────────────────────────

def get_events(
    room: str | None = None,
    date: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_cancelled: bool = False,
    limit: int = 20_000,
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
        sql.append("AND (title LIKE ? OR room LIKE ? OR description LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
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
        "cancelled": bool(r["cancelled"]),
        "all_day": bool(r["all_day"]),
    }


def get_rooms() -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT * FROM rooms ORDER BY room"
    ).fetchall()
    return [
        {
            "room": r["room"],
            "display": r["display"] or r["room"],
            "floor": r["floor"] or "",
            "capacity": r["capacity"],
            "panopto": bool(r["panopto"]),
        }
        for r in rows
    ]


def stats() -> dict[str, Any]:
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    rooms = conn.execute("SELECT COUNT(DISTINCT room) c FROM events").fetchone()["c"]
    rng = conn.execute(
        "SELECT MIN(date) a, MAX(date) b FROM events WHERE date IS NOT NULL"
    ).fetchone()
    return {
        "total_events": total,
        "rooms": rooms,
        "date_from": rng["a"],
        "date_to": rng["b"],
    }


# ── Key/value ────────────────────────────────────────────────────────────

def get_kv(key: str, default: Any = None) -> Any:
    row = _conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
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
    return dict(ROOM_GROUPS)


def save_groups(groups: dict[str, list[str]]) -> None:
    GROUPS_PATH.write_text(json.dumps(groups, indent=2), encoding="utf-8")


def room_display(room: str) -> str:
    for r in get_rooms():
        if r["room"] == room and r["display"]:
            return r["display"]
    return f"Room {room}"


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
