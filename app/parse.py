"""
Rotman CSV → structured events.

The APEX report exports a CSV whose columns are deceptively messy. The
three things that bite, all handled here:

1. Dates look like "15-April    -26" — spaces inside, 2-digit year.
2. Times are HHMM ("645" = 06:45, "1800" = 18:00), not clock strings.
3. Times under 600 are *slot indices*, not times. A booking on a
   timetable slot reports "500" meaning "period 5", and the real time is
   buried in the Comment field as "0900-1200". Treating 500 as 05:00
   would silently place bookings five hours early, so when we see a slot
   index we either recover the time from the comment or drop the time
   entirely rather than invent one.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Any

from app.config import DEFAULT_DURATION_MINUTES, log

# Column headers vary slightly between report runs.
COLMAP = {
    "room": ("Room", "Room Number"),
    "title": ("Event/Course", "Event", "Course"),
    "date": ("Date",),
    "start": ("Start Time", "Start"),
    "end": ("End Time", "End"),
    "duration": ("Duration",),
    "building": ("Building",),
    "class_code": ("Class Code",),
    "comment": ("Comment", "Comments", "Description"),
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# The report marks a cancellation by *leading* the Comment field with it —
# "CANCLD DCIT 07.29", "Cancld may 11" — optionally behind the ZZ/ marker it
# puts on the title. Anchored, because the alternative is a substring search
# over free text: unanchored, any booking whose notes merely mention the word
# is read as cancelled, and a cancelled booking is not stored at all, so the
# booking vanishes and nothing says why. "CANCELLATION POLICY" as a course
# topic was enough.
#
# Anchored at both ends: the stem has to *end* there. An anchor alone stops
# the substring search and nothing else, because it still leaves the pattern
# free to match the first six letters of a longer word that opens the field —
# "Cancellation Policy discussion" led with the stem, matched, and the booking
# disappeared with no trace. So the stem must be the *verb*: the abbreviation
# the report uses, or the whole word in its past tense, and never the noun.
# `(?![A-Za-z])` rather than `\b` because the boundary is about the word
# continuing, and a report that runs the date straight onto the marker
# ("CANCLD07.29") still ends the token it means — a digit is a boundary too.
#
# Erring toward *not* flagging is the safe direction and the asymmetry is the
# reason. A cancellation we miss stays on the calendar for one scrape and then
# disappears when the report stops listing it — the reconcile removes it and
# the feed records it, so it corrects itself within a day. A booking we
# wrongly flag is deleted with no trace, and the report will never mention it
# again, so nothing brings it back. That is what decides "Cancelling" and
# "Cancellations" too: both are prose, neither is the marker, and a booking
# pulled off the calendar for one is the loss this whole pattern is shaped to
# avoid.
#
# Note "cancl" is NOT a substring of "cancel", so it needs its own
# alternative — without it this never matched anything, and every cancelled
# booking was treated as live.
_CANCELLED_RE = re.compile(
    r"^\s*(?:ZZ\s*/\s*)?[-–—:.\s]*"
    r"(?:cancel(?:led|ed|d)?|cancl(?:d)?|cnxld|cncld)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_TIME_RANGE_RE = re.compile(r"\b(\d{3,4})\s*[-–—]\s*(\d{3,4})\b")
# Longest alternative first — otherwise "ROTMAN" matches as "RT" and
# leaves "MAN L1060" behind.
_ROOM_PREFIX_RE = re.compile(r"^(?:ROTMAN|ROT|RT)\s*[- ]?\s*", re.IGNORECASE)


def _pick(row: dict[str, str], field: str) -> str:
    for key in COLMAP[field]:
        if key in row and row[key]:
            return str(row[key]).strip()
    # Case-insensitive fallback for header drift
    lowered = {k.lower().strip(): v for k, v in row.items() if k}
    for key in COLMAP[field]:
        if key.lower() in lowered and lowered[key.lower()]:
            return str(lowered[key.lower()]).strip()
    return ""


def parse_csv(content: str) -> list[dict[str, Any]]:
    """Parse the APEX report CSV into event dicts. Never raises on bad rows."""
    if not content.strip():
        return []

    # APEX sometimes prefixes the download with a BOM or a blank line.
    content = content.lstrip("﻿").lstrip("\r\n")

    try:
        dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    events: list[dict[str, Any]] = []
    skipped = 0

    for row in reader:
        if not row:
            continue
        try:
            ev = _row_to_event(row)
            if ev:
                events.append(ev)
            else:
                skipped += 1
        except Exception as exc:
            skipped += 1
            log.debug("skipping malformed CSV row: %s", exc)

    log.info("parsed %d events (%d rows skipped)", len(events), skipped)
    return events


def _row_to_event(row: dict[str, str]) -> dict[str, Any] | None:
    title = _pick(row, "title")
    if not title:
        return None

    room = normalise_room(_pick(row, "room"))
    if not room:
        return None

    event_date = parse_rotman_date(_pick(row, "date"))
    if not event_date:
        return None

    comment = _pick(row, "comment")
    start, end = parse_rotman_time(
        _pick(row, "start"), _pick(row, "end"), event_date, comment
    )

    building = _pick(row, "building")

    # All-day means the report gave us no recoverable time at all (an
    # unresolvable timetable slot index), OR the booking genuinely spans the
    # day. The second case is the 003/RENOVATIONS and 003/AV UPGRADES service
    # blocks, which arrive as 00:00-23:00: without this they rendered as
    # "12:00 a.m. -> 11:00 p.m." and sat at the top of every month cell.
    all_day = start is None or (
        start.hour == 0
        and start.minute == 0
        and end is not None
        and end.hour >= 23
    )

    return {
        "title": title,
        "start": (start or event_date).isoformat(),
        "end": end.isoformat() if end else None,
        "all_day": all_day,
        "room": room,
        "location": f"{building} {room}".strip(),
        "description": clean_description(comment),
        "class_code": _pick(row, "class_code"),
        "timezone": "America/Toronto",
        "cancelled": bool(_CANCELLED_RE.search(comment)),
    }


def normalise_room(raw: str) -> str:
    """'RT 142' / 'RT-142' → '142'. Preserves named spaces like 'Event North'."""
    if not raw:
        return ""
    r = _ROOM_PREFIX_RE.sub("", raw.strip())
    return r.strip(" -").strip()


def parse_rotman_date(raw: str) -> datetime | None:
    """'15-April    -26' → datetime(2026, 4, 15)."""
    if not raw:
        return None
    s = re.sub(r"\s*-\s*", "-", raw.strip())

    m = re.match(r"(\d{1,2})[-/](\w+)[-/](\d{2,4})", s)
    if m:
        day, month_name, year = m.groups()
        if len(year) == 2:
            year = "20" + year
        month = _MONTHS.get(month_name.lower())
        if month:
            try:
                return datetime(int(year), month, int(day))
            except ValueError:
                return None

    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_rotman_time(
    start_raw: str, end_raw: str, ref: datetime, comment: str = ""
) -> tuple[datetime | None, datetime | None]:
    """
    Returns (start, end). Slot indices (< 600) are not times — see module docstring.
    """
    start = _hhmm(start_raw, ref)
    end = _hhmm(end_raw, ref)

    start_is_slot = _is_slot_index(start_raw)
    end_is_slot = _is_slot_index(end_raw)

    recovered = False
    if start_is_slot:
        # Recover the real window from the comment if it is there.
        m = _TIME_RANGE_RE.search(comment or "")
        if m:
            start = _hhmm(m.group(1), ref)
            end = _hhmm(m.group(2), ref)
            recovered = True
        else:
            start = None

    # Only discard a slot-index end if the comment did not supply the real
    # one — otherwise this would undo the recovery above.
    if end_is_slot and not recovered:
        end = None

    if start and end is None:
        end = start + timedelta(minutes=DEFAULT_DURATION_MINUTES)
    if start and end and end <= start:
        # Overnight or mis-ordered — treat as a default-length booking.
        end = start + timedelta(minutes=DEFAULT_DURATION_MINUTES)

    return start, end


def _is_slot_index(raw: str) -> bool:
    if not raw:
        return False
    try:
        return int(raw) < 600
    except ValueError:
        return False


# A meridiem, in the spellings a 12-hour clock string arrives in: "2:00 PM",
# "2:00PM", "2:00 p.m.". Deliberately not \b-anchored — \b wants a non-word
# character before the "P", and "2:00PM" has a digit there.
_AMPM_RE = re.compile(r"(?<![a-z])([ap])\.?\s*m\.?(?![a-z])", re.IGNORECASE)


def _hhmm(raw: str, ref: datetime) -> datetime | None:
    """'645' → 06:45, '1800' → 18:00, '2:00 PM' → 14:00.

    The report's own times are HHMM. This is also pointed at the free text of
    the Comment field, and a 12-hour clock string is *read* here rather than
    assumed away — it used to be assumed away, and that was the bug: stripping
    the non-digits threw the meridiem out along with the punctuation, so
    '2:00 PM' became '200' and 02:00, twelve hours early, with nothing to show
    it had happened. '12:30 AM' became 12:30 rather than 00:30 the same way.

    This is the failure this module already refuses for slot indices: read the
    time or drop it, but never invent one — and a plausible wrong one is the
    worst of the three, because the booking still looks like a booking.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits or len(digits) > 4:
        return None
    try:
        val = int(digits)
    except ValueError:
        return None
    hours, minutes = divmod(val, 100)
    if hours > 23 or minutes > 59:
        return None

    m = _AMPM_RE.search(raw)
    if m:
        # A meridiem only means anything on a 1–12 hour, so "13:00 PM" is a
        # contradiction — reading it as either half is the guess this function
        # exists not to make.
        if hours > 12:
            return None
        if m.group(1).lower() == "p":
            hours = hours + 12 if hours < 12 else 12
        else:
            hours = 0 if hours == 12 else hours

    return datetime(ref.year, ref.month, ref.day, hours, minutes)


# ── Description cleanup ──────────────────────────────────────────────────

_STRIP_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                       # emails
    re.compile(r"\bRITM\d{6,}\b", re.IGNORECASE),                   # ticket refs
    re.compile(r"CAT\s*//\s*CLEAN[^\n]*", re.IGNORECASE),           # catering notes
    re.compile(r"\bsu\.?\s*\d+\s*rnds[^\n]*", re.IGNORECASE),
    re.compile(r"\bno catering\b\s*//?", re.IGNORECASE),
    re.compile(r"\b\d+\s*brkts?/day\b", re.IGNORECASE),
    re.compile(r"\b(canc?ld|cnxld)\s*[\d.]+", re.IGNORECASE),
)


def clean_description(raw: str) -> str:
    """Strip internal ops shorthand so the calendar reads cleanly."""
    if not raw:
        return ""
    s = raw
    for pat in _STRIP_PATTERNS:
        s = pat.sub(" ", s)
    s = s.replace("//", " ")
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{2,}", "\n", s).strip(" \n-/")
    return s if not re.fullmatch(r"[\s\-/]*", s) else ""


def clean_title(raw: str) -> str:
    """Drop the cancelled 'ZZ/' marker, keep course codes readable."""
    s = (raw or "").strip()
    if s.upper().startswith("ZZ/"):
        s = s[3:]
    return s or "Booking"
