"""
Parser tests. Run directly (no pytest needed):

    python tests/test_parse.py

The cases here are the ones that actually bit us against the live report:
the "15-April    -26" date shape, HHMM times, and the slot-index
trap where a value under 600 is a timetable period rather than a time.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# app.parse logs through app.config, which opens the log file as soon as it
# is imported. Point the data directory at a scratch location first, or every
# parser case writes "parsed N events" into the real data/app.log and makes
# the live log look like the app scraped when it did not.
os.environ["LSM_DATA_DIR"] = tempfile.mkdtemp(prefix="lsm-parse-")

from app.parse import (  # noqa: E402
    clean_description, clean_title, normalise_room, parse_csv,
    parse_rotman_date, parse_rotman_time,
)

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")


def test_dates() -> None:
    print("\nparse_rotman_date")
    check("padded with inner spaces", parse_rotman_date("15-April    -26"),
          datetime(2026, 4, 15))
    check("full year", parse_rotman_date("01-September-2026"),
          datetime(2026, 9, 1))
    check("abbreviated month", parse_rotman_date("3-Dec-26"),
          datetime(2026, 12, 3))
    check("unparseable", parse_rotman_date(""), None)
    check("garbage", parse_rotman_date("not a date"), None)


def test_times() -> None:
    print("\nparse_rotman_time")
    ref = datetime(2026, 4, 15)

    s, e = parse_rotman_time("900", "1200", ref)
    check("plain HHMM start", s, datetime(2026, 4, 15, 9, 0))
    check("plain HHMM end", e, datetime(2026, 4, 15, 12, 0))

    s, e = parse_rotman_time("645", "1800", ref)
    check("single-digit hour", s, datetime(2026, 4, 15, 6, 45))
    check("evening end", e, datetime(2026, 4, 15, 18, 0))

    # Slot index with the real window recoverable from the comment.
    s, e = parse_rotman_time("500", "501", ref, comment="0900-1200 setup")
    check("slot index -> time from comment", s, datetime(2026, 4, 15, 9, 0))
    check("slot index -> end from comment", e, datetime(2026, 4, 15, 12, 0))

    # Slot index with nothing to recover: drop the time, keep the date.
    s, e = parse_rotman_time("500", "501", ref, comment="")
    check("slot index with no comment -> no start", s, None)
    check("slot index with no comment -> no end", e, None)

    # Missing end time defaults to one hour.
    s, e = parse_rotman_time("900", "", ref)
    check("default duration", e, datetime(2026, 4, 15, 10, 0))

    # End before start is nonsense; fall back rather than emit a negative span.
    s, e = parse_rotman_time("1400", "0900", ref)
    check("reversed range normalised", e, datetime(2026, 4, 15, 15, 0))

    check("invalid hour rejected", parse_rotman_time("2599", "", ref)[0], None)


def test_rooms() -> None:
    print("\nnormalise_room")
    check("RT prefix", normalise_room("RT 142"), "142")
    check("RT dash", normalise_room("RT-142"), "142")
    check("rotman prefix", normalise_room("ROTMAN L1060"), "L1060")
    check("bare number", normalise_room("142"), "142")
    check("named space preserved", normalise_room("Event North"), "Event North")
    check("empty", normalise_room(""), "")


def test_cleanup() -> None:
    print("\ndescription / title cleanup")
    check("email stripped",
          "email" in clean_description("Contact a.b@utoronto.ca please").lower(),
          False)
    check("ticket ref stripped",
          "RITM0195294" in clean_description("See RITM0195294"), False)
    check("catering note stripped",
          "CLEANING" in clean_description("CAT//CLEANING RQRD - HOT BUFFET"),
          False)
    check("innocuous text kept",
          clean_description("Department meeting"), "Department meeting")
    check("ZZ prefix removed", clean_title("ZZ/208/CIBC.1/A.MAHAJAN"),
          "208/CIBC.1/A.MAHAJAN")
    check("empty title defaulted", clean_title(""), "Booking")


def test_csv() -> None:
    print("\nparse_csv (end to end)")
    csv_text = (
        "Room,Event/Course,Date,Start Time,End Time,Duration,Building,"
        "Class Code,Comment\n"
        "RT 142,RSM6307 Marketing,15-April    -26,900,1200,3h,RT,A,\n"
        "RT L1060,CIBC Info Session,16-April-26,500,501,,\"ROT L1060\",S,"
        "\"0900-1200 su. 4rndsX10 / CAT/ CLEAN //\"\n"
        "RT 127,ZZ/Cancelled Thing,17-April-26,1000,1100,,RT,A,CANCELLED\n"
        ",,18-April-26,900,1000,,,,ignored: no title\n"
        "RT 133,No Date Booking,,900,1000,,,,ignored: no date\n"
    )
    events = parse_csv(csv_text)

    check("three valid rows parsed", len(events), 3)

    first = events[0]
    check("room normalised", first["room"], "142")
    check("title preserved", first["title"], "RSM6307 Marketing")
    check("start parsed", first["start"], "2026-04-15T09:00:00")
    check("end parsed", first["end"], "2026-04-15T12:00:00")
    check("not cancelled", first["cancelled"], False)

    second = events[1]
    check("slot times recovered", second["start"], "2026-04-16T09:00:00")
    check("comment cleaned", "CLEAN" in second["description"].upper(), False)

    third = events[2]
    check("cancellation flagged", third["cancelled"], True)

    empty = parse_csv("")
    check("empty input safe", empty, [])


def _one(comment: str) -> dict:
    csv_text = (
        "Room,Event/Course,Date,Start Time,End Time,Duration,Building,"
        "Class Code,Comment\n"
        f'RT 142,Thing,15-April-26,900,1200,3h,RT,A,"{comment}"\n'
    )
    return parse_csv(csv_text)[0]


def test_cancelled_spellings() -> None:
    print("\ncancellation spellings")
    # The report spells it "CANCLD" / "Cancld". The old pattern was
    # "cancel|cnxld|cncld", and "cancl" is not a substring of "cancel" — so
    # nothing ever matched and every cancelled booking showed as live.
    for comment in ("CANCLD DCIT 07.29", "Cancld may 11", "CANCELLED", "cnxld"):
        check(f"flagged: {comment!r}", _one(comment)["cancelled"], True)
    check("not flagged: 'CAT/ CLEAN'", _one("CAT/ CLEAN")["cancelled"], False)


def test_all_day() -> None:
    print("\nall-day detection")
    csv_text = (
        "Room,Event/Course,Date,Start Time,End Time,Duration,Building,"
        "Class Code,Comment\n"
        # Full-day service block, as the report sends it.
        "RT 157,003/RENOVATIONS/MACPHERSON,01-August-26,0,2300,,RT,Z,\n"
        # Slot index with no time recoverable from the comment.
        "RT 142,Some Slot Booking,02-August-26,500,501,,RT,A,\n"
        # Ordinary timed booking.
        "RT 142,RSM6307 Marketing,03-August-26,900,1200,,RT,A,\n"
    )
    events = parse_csv(csv_text)
    check("three rows parsed", len(events), 3)

    check("full-day block is all_day", events[0]["all_day"], True)
    check("full-day block keeps its real start", events[0]["start"],
          "2026-08-01T00:00:00")
    check("unrecoverable slot is all_day", events[1]["all_day"], True)
    check("ordinary booking is not all_day", events[2]["all_day"], False)


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — parser tests")
    print("=" * 60)

    test_dates()
    test_times()
    test_rooms()
    test_cleanup()
    test_csv()
    test_cancelled_spellings()
    test_all_day()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
