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

_CANCELLED_RE = re.compile(r"cancel|cnxld|cncld", re.IGNORECASE)
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
    return {
        "title": title,
        "start": (start or event_date).isoformat(),
        "end": end.isoformat() if end else None,
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


def _hhmm(raw: str, ref: datetime) -> datetime | None:
    """'645' → 06:45, '1800' → 18:00."""
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
