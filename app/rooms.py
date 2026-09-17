"""
Room metadata — friendly names, floor, capacity.

The raw report only gives bare room numbers ("142", "L1060"). This turns
them into something readable and lets the calendar group by floor, badge
capacity, and flag Panopto-equipped rooms.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import PANOPTO_ROOMS

ROOM_INFO: dict[str, dict[str, Any]] = {
    # ── Ground floor classrooms ──
    "100": {"name": "Event Hall 100", "floor": "Ground Floor", "capacity": 120,
            "notes": "Large event hall, divisible"},
    "127": {"name": "Room 127", "floor": "Ground Floor", "capacity": 72, "notes": "Tiered classroom"},
    "133": {"name": "Room 133", "floor": "Ground Floor", "capacity": 56},
    "134": {"name": "Room 134", "floor": "Ground Floor", "capacity": 72, "notes": "Tiered classroom"},
    "142": {"name": "Room 142", "floor": "Ground Floor", "capacity": 72, "notes": "Tiered classroom"},
    "147": {"name": "Room 147", "floor": "Ground Floor", "capacity": 65},
    "151": {"name": "Room 151", "floor": "Ground Floor", "capacity": 56},
    "157": {"name": "Room 157", "floor": "Ground Floor", "capacity": 56},
    "287": {"name": "Room 287", "floor": "Ground Floor", "capacity": 40, "notes": "Flat classroom"},
    "349": {"name": "Room 349", "floor": "Ground Floor", "capacity": 30},
    "368": {"name": "Room 368", "floor": "Ground Floor", "capacity": 56},
    "371B": {"name": "Room 371B", "floor": "Ground Floor", "capacity": 30, "notes": "Seminar room"},
    "371C": {"name": "Room 371C", "floor": "Ground Floor", "capacity": 30, "notes": "Seminar room"},
    "374": {"name": "Room 374", "floor": "Ground Floor", "capacity": 56},
    "392": {"name": "Room 392", "floor": "Ground Floor", "capacity": 40, "notes": "Flat classroom"},
    "394": {"name": "Room 394", "floor": "Ground Floor", "capacity": 40, "notes": "Flat classroom"},
    # ── 1st floor ──
    "1007": {"name": "Event Hall 1007", "floor": "1st Floor", "capacity": 120,
             "notes": "Large event hall"},
    "1065": {"name": "Room 1065", "floor": "1st Floor", "capacity": 72, "notes": "Tiered classroom"},
    "1085": {"name": "Room 1085", "floor": "1st Floor", "capacity": 56},
    # ── 2nd floor ──
    "2015": {"name": "Room 2015", "floor": "2nd Floor", "capacity": 12, "notes": "Breakout room"},
    "2030": {"name": "Room 2030", "floor": "2nd Floor", "capacity": 80, "notes": "Large event space"},
    "2050D": {"name": "Room 2050D", "floor": "2nd Floor", "capacity": 20, "notes": "Small seminar room"},
    "2088": {"name": "Room 2088", "floor": "2nd Floor", "capacity": 30},
    "2108": {"name": "Room 2108", "floor": "2nd Floor", "capacity": 30},
    # ── Lower level ──
    "L1010": {"name": "Room L1010", "floor": "Lower Level", "capacity": 72, "notes": "Tiered classroom"},
    "L1020": {"name": "Room L1020", "floor": "Lower Level", "capacity": 72, "notes": "Tiered classroom"},
    "L1025": {"name": "Room L1025", "floor": "Lower Level", "capacity": 72, "notes": "Tiered classroom"},
    "L1030": {"name": "Room L1030", "floor": "Lower Level", "capacity": 56},
    "L1035": {"name": "Room L1035", "floor": "Lower Level", "capacity": 56},
    "L1040": {"name": "Room L1040", "floor": "Lower Level", "capacity": 56},
    "L1045": {"name": "Room L1045", "floor": "Lower Level", "capacity": 56},
    "L1050": {"name": "Room L1050", "floor": "Lower Level", "capacity": 56},
    "L1055": {"name": "Room L1055", "floor": "Lower Level", "capacity": 56},
    "L1058": {"name": "Room L1058", "floor": "Lower Level", "capacity": 56},
    "L1060": {"name": "Room L1060", "floor": "Lower Level", "capacity": 72, "notes": "Tiered classroom"},
    # ── Upper floors ──
    "4001": {"name": "Room 4001", "floor": "4th Floor", "capacity": 40},
    "4005": {"name": "Room 4005", "floor": "4th Floor", "capacity": 40},
    "4057": {"name": "Room 4057", "floor": "4th Floor", "capacity": 40},
    "448": {"name": "Room 448", "floor": "Ground Floor", "capacity": 20, "notes": "Seminar room"},
    "470": {"name": "Room 470", "floor": "Ground Floor", "capacity": 20, "notes": "Seminar room"},
    "548": {"name": "Room 548", "floor": "Ground Floor", "capacity": 20},
    "570": {"name": "Room 570", "floor": "Ground Floor", "capacity": 20},
    "6024": {"name": "Room 6024", "floor": "6th Floor", "capacity": 30},
    "7024": {"name": "Room 7024", "floor": "7th Floor", "capacity": 30},
    "8024": {"name": "Room 8024", "floor": "8th Floor", "capacity": 30},
    "9005": {"name": "Room 9005", "floor": "9th Floor", "capacity": 30},
    "9062": {"name": "Room 9062", "floor": "9th Floor", "capacity": 30},
    "9076": {"name": "Room 9076", "floor": "9th Floor", "capacity": 30},
    # ── Named spaces ──
    "Atrium": {"name": "Atrium", "floor": "Ground Floor", "capacity": None},
    "Event North": {"name": "Event Space — North", "floor": "Ground Floor", "capacity": None},
    "Event South": {"name": "Event Space — South", "floor": "Ground Floor", "capacity": None},
    "FinLab": {"name": "Financial Lab", "floor": "Lower Level", "capacity": None},
}

FLOOR_ORDER = [
    "Lower Level", "Ground Floor", "1st Floor", "2nd Floor", "3rd Floor",
    "4th Floor", "5th Floor", "6th Floor", "7th Floor", "8th Floor",
    "9th Floor", "Mezzanine",
]


def floor_for(room: str) -> str:
    """Explicit metadata first, then a pattern guess from the room number."""
    info = ROOM_INFO.get(room)
    if info:
        return info["floor"]
    if not room:
        return ""
    if room.startswith("L"):
        return "Lower Level"
    if re.fullmatch(r"1\d{2}", room):
        return "Ground Floor"
    if re.fullmatch(r"10\d{2}", room):
        return "1st Floor"
    if re.fullmatch(r"2\d{3}", room):
        return "2nd Floor"
    if re.fullmatch(r"3\d{3}", room):
        return "3rd Floor"
    if re.fullmatch(r"4\d{3}", room):
        return "4th Floor"
    if re.fullmatch(r"5\d{3}", room):
        return "5th Floor"
    if re.fullmatch(r"6\d{3}", room):
        return "6th Floor"
    if re.fullmatch(r"7\d{3}", room):
        return "7th Floor"
    if re.fullmatch(r"8\d{3}", room):
        return "8th Floor"
    if re.fullmatch(r"9\d{3}", room):
        return "9th Floor"
    if room[:1].upper() == "M":
        return "Mezzanine"
    return ""


def display_name(room: str) -> str:
    info = ROOM_INFO.get(room)
    if info:
        return info["name"]
    floor = floor_for(room)
    return f"Room {room} — {floor}" if floor else f"Room {room}"


def capacity(room: str) -> int | None:
    info = ROOM_INFO.get(room)
    return info.get("capacity") if info else None


def has_panopto(room: str) -> bool:
    return room in PANOPTO_ROOMS


def describe(room: str) -> dict[str, Any]:
    """Everything the calendar needs to render one room."""
    info = ROOM_INFO.get(room) or {}
    return {
        "room": room,
        "display": display_name(room),
        "floor": floor_for(room),
        "capacity": capacity(room),
        "panopto": has_panopto(room),
        "notes": info.get("notes", ""),
    }


def floor_sort_key(floor: str) -> int:
    try:
        return FLOOR_ORDER.index(floor)
    except ValueError:
        return len(FLOOR_ORDER)
