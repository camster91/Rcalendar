"""
The one answer to "is this room busy?".

Free/busy used to live inline in the /api/today route as a point-in-time
comparison. "Free at a time" asks the same question about a window, and a
second implementation of it would be a second answer — the two would drift
and nothing would notice. So both forms live here, `_span` is the only place
a booking's interval is worked out, and /api/today is their only caller.

Comparisons are lexicographic on naive-local ISO strings, which is what the
rest of the codebase does and is correct for a single timezone.
"""

from __future__ import annotations

from typing import Any, Iterable


def busy_at(bookings: Iterable[dict[str, Any]], when_iso: str) -> dict[str, Any] | None:
    """
    The booking covering the instant `when_iso`, or None.

    Half-open, `start <= t < end`: a booking's first instant belongs to the
    booking, so a room booked from 14:00 is not free at 14:00.
    """
    for b in bookings:
        span = _span(b)
        if span and span[0] <= when_iso < span[1]:
            return b
    return None


def busy_between(
    bookings: Iterable[dict[str, Any]], start_iso: str, end_iso: str
) -> dict[str, Any] | None:
    """
    The first booking overlapping the half-open window [start_iso, end_iso).

    Half-open at both ends, so a window that merely touches a booking at a
    boundary does not collide with it: with a booking from 09:00 to 12:00, a
    window of 08:00-09:00 is free (the room is not occupied before it starts)
    and a window of 12:00-13:00 is free (the room is not occupied once it ends).

    Two bookings that run back to back, 09:00-12:00 then 12:00-15:00, leave no
    free instant between them — 12:00 belongs to the second. The half-open rule
    is about the *edges* of a booking, not about manufacturing a gap at a seam.
    """
    for b in bookings:
        span = _span(b)
        if span and span[0] < end_iso and span[1] > start_iso:
            return b
    return None


def _span(b: dict[str, Any]) -> tuple[str, str] | None:
    """The ISO interval a booking occupies, or None if it has no usable one."""
    if b.get("all_day"):
        # An all-day row occupies its whole date, whatever clock times it
        # carries — or fails to carry. The report renders a service block as
        # 00:00-23:00 and leaves a booking whose time could not be recovered
        # with no end at all, and both are flagged all_day. Treating the flag
        # as the whole day is the honest reading, and it is what the card and
        # the change feed already display ("All day").
        #
        # Without this a booking with no recoverable end reads as *free all
        # day despite existing*, which is how Free Right Now behaved.
        day = b.get("date") or (b.get("start") or "")[:10]
        if not day:
            return None
        # T24:00 is ISO-8601's end-of-day. It sorts after every clock time on
        # the day and before any time on the next one, so no date arithmetic
        # is needed to say "the whole of this day".
        return f"{day}T00:00:00", f"{day}T24:00:00"

    start = b.get("start") or ""
    end = b.get("end") or ""
    # A booking with no recoverable end is zero-length rather than spanning to
    # the end of time. The all_day branch above is what catches the real ones.
    return (start, end) if start and end else None
