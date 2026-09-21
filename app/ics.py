"""Calendar (.ics) export — importable into Outlook, Google Calendar, Apple."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.config import CALENDAR_NAME, CALENDAR_TZ, DEFAULT_DURATION_MINUTES, ICS_PATH, log
from app.store import event_uid

TZ = ZoneInfo(CALENDAR_TZ)


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return d.replace(tzinfo=TZ) if d.tzinfo is None else d


def build_ics(events: Iterable[dict[str, Any]]) -> str:
    from icalendar import Calendar, Event

    cal = Calendar()
    cal.add("prodid", "-//Rotman LSM Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", CALENDAR_NAME)
    cal.add("x-wr-timezone", CALENDAR_TZ)

    count = 0
    for ev in events:
        if ev.get("cancelled"):
            continue
        start = _dt(ev.get("start"))
        if not start:
            continue
        end = _dt(ev.get("end")) or (start + timedelta(minutes=DEFAULT_DURATION_MINUTES))

        item = Event()
        room = ev.get("room") or ""
        item.add("summary", ev.get("title") or "Room booking")
        if ev.get("all_day"):
            # All-day events take DATE values, and DTEND is exclusive — a
            # single day ends on the following date. Without this branch a
            # full-day block exported as a bogus 00:00-01:00 timed booking.
            day = start.date()
            item.add("dtstart", day)
            item.add("dtend", day + timedelta(days=1))
        else:
            item.add("dtstart", start)
            item.add("dtend", end)
        item.add("location", ev.get("location") or room)
        item.add("uid", _uid(ev))
        item.add("dtstamp", datetime.now(TZ))

        desc_parts = [p for p in (room and f"Room {room}", ev.get("description")) if p]
        if desc_parts:
            item.add("description", " • ".join(desc_parts))

        cal.add_component(item)
        count += 1

    log.info("generated .ics with %d events", count)
    return cal.to_ical().decode("utf-8")


def _uid(ev: dict[str, Any]) -> str:
    """One stored booking, one calendar UID.

    This used to hash title|room|start — the store's identity with `end`
    dropped, which made it a *second* function answering the question the
    store already answers. Two bookings the database holds as two rows (an
    all-day block and a timed one, or any pair differing only in when they
    finish) then came out under one UID, and a .ics with two VEVENTs sharing
    a UID is two events only until something reads it: by RFC 5545 the UID
    *is* the identity, so an importer keeps one and discards the other with
    no error. A booking vanishing from Outlook is not a thing the user can
    see happening.

    store.event_uid is what "which booking is this?" means everywhere else,
    and it hashes title|room|start|end as its own docstring says. Deriving
    from it makes one row → one UID true by construction rather than by this
    key happening to agree.
    """
    return f"{event_uid(ev)}@rotman-lsm-calendar"


def write_ics(events: Iterable[dict[str, Any]]) -> None:
    try:
        ICS_PATH.write_text(build_ics(events), encoding="utf-8")
    except Exception as exc:
        log.warning("could not write .ics: %s", exc)
