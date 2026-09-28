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
    clean_description, clean_title, is_report_csv, normalise_room,
    parse_csv,
    parse_rotman_date, parse_rotman_time, split_title,
)

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        # This console is cp1252 and the assertions below carry the odd
        # non-ascii title character. A failed assertion should report the
        # mismatch, not die printing it.
        g = f"{got!r}".encode("ascii", "replace").decode()
        w = f"{want!r}".encode("ascii", "replace").decode()
        print(f"  FAIL  {label}\n          got:  {g}\n          want: {w}")


def ok(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


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

    # The recovery reads free text, so ranges that are not times match the
    # same pattern. A count is the case that matters: "expect 300-400
    # attendees" used to read as 03:00-04:00 — a wrong-but-plausible window,
    # the exact failure _hhmm exists to refuse. Both ends now have to land
    # inside the bookable day or the booking stays all-day.
    s, e = parse_rotman_time("500", "501", ref,
                             comment="expect 300-400 attendees")
    check("a count range is not a window -> no start", s, None)
    check("a count range is not a window -> no end", e, None)

    s, e = parse_rotman_time("500", "501", ref, comment="0300-1400 overnight")
    check("a window starting before the bookable day is refused", s, None)

    s, e = parse_rotman_time("500", "501", ref, comment="0900-1299 typo")
    check("a range with an unreadable end is not half-trusted", s, None)

    s, e = parse_rotman_time("500", "501", ref, comment="900-1200 setup")
    check("an unpadded three-digit start still recovers", s,
          datetime(2026, 4, 15, 9, 0))
    check("an unpadded three-digit start still recovers -> end", e,
          datetime(2026, 4, 15, 12, 0))

    # Missing end time defaults to one hour.
    s, e = parse_rotman_time("900", "", ref)
    check("default duration", e, datetime(2026, 4, 15, 10, 0))

    # End before start is nonsense; fall back rather than emit a negative span.
    s, e = parse_rotman_time("1400", "0900", ref)
    check("reversed range normalised", e, datetime(2026, 4, 15, 15, 0))

    check("invalid hour rejected", parse_rotman_time("2599", "", ref)[0], None)


def test_twelve_hour_clock() -> None:
    """A 12-hour string carries half its meaning in letters.

    Stripping the non-digits took the meridiem with it, and "2:00 PM" read as
    02:00 — twelve hours early, on a booking that still looked like a booking.
    Both halves of the clock are pinned here, along with the hour that only
    the meridiem can resolve (12 is 12:00 or 00:00, never both).
    """
    print("\ntwelve-hour clock")
    ref = datetime(2026, 4, 15)

    for raw, want in (("2:00 PM", (14, 0)), ("2:00 AM", (2, 0)),
                      ("11:30 pm", (23, 30)), ("9:00AM", (9, 0)),
                      ("4:15 p.m.", (16, 15)), ("4:15 a.m.", (4, 15))):
        check(f"{raw!r} is {want[0]:02d}:{want[1]:02d}",
              parse_rotman_time(raw, "", ref)[0],
              datetime(2026, 4, 15, want[0], want[1]))

    # The two hours a meridiem is the only thing that can place.
    check("12:30 PM is noon-thirty, not midnight-thirty",
          parse_rotman_time("12:30 PM", "", ref)[0],
          datetime(2026, 4, 15, 12, 30))
    check("12:30 AM is midnight-thirty",
          parse_rotman_time("12:30 AM", "", ref)[0],
          datetime(2026, 4, 15, 0, 30))

    # A meridiem on a 13–23 hour contradicts it, and either reading is a
    # guess — so the time is dropped, the same answer a slot index with no
    # comment gets, rather than a number picked out of the air.
    check("13:00 PM is refused, not guessed",
          parse_rotman_time("13:00 PM", "", ref)[0], None)
    check("18:00 PM is refused too",
          parse_rotman_time("18:00 PM", "", ref)[0], None)

    # And nothing that was reading correctly before reads differently now.
    check("bare HHMM is untouched", parse_rotman_time("1800", "", ref)[0],
          datetime(2026, 4, 15, 18, 0))
    check("a colon is still just punctuation",
          parse_rotman_time("6:45", "", ref)[0], datetime(2026, 4, 15, 6, 45))
    check("the comment recovery still recovers",
          parse_rotman_time("500", "501", ref, comment="0900-1200 setup")[0],
          datetime(2026, 4, 15, 9, 0))


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


def test_split_title() -> None:
    print("\ntitle shape: code / name / booker")
    # The report's own examples, measured off the store: 20,358 of 29,057
    # stored titles carry the code prefix, and 18,504 of those are exactly
    # code/name/booker.
    check("the common shape", split_title("208/CIBC.1/A.MAHAJAN"),
          ("CIBC.1", "A.MAHAJAN"))
    check("service block", split_title("003/RENOVATIONS/MACPHERSON"),
          ("RENOVATIONS", "MACPHERSON"))
    check("XXX is a code too", split_title("XXX/ICPM CONFERENCE/MACPHERSON"),
          ("ICPM CONFERENCE", "MACPHERSON"))
    # 4+ segments: the middle qualifiers belong with the booker, because the
    # report uses them for people ("007/PRE-TERM MTG/CHENG/D'ANGEL") and the
    # two cannot be told apart by shape alone.
    check("a second person travels with the booker",
          split_title("007/PRE-TERM MTG/CHENG/D'ANGEL"),
          ("PRE-TERM MTG", "CHENG · D'ANGEL"))
    check("an empty segment does not become an empty booker half",
          split_title("227/END OF YR1 LUNCH//CHAR"),
          ("END OF YR1 LUNCH", "CHAR"))
    # 36 titles in the corpus carry a second code segment.
    check("a doubled code is not the name",
          split_title("014/012/DEAN & HR MTG/T. YOUNG"),
          ("DEAN & HR MTG", "T. YOUNG"))
    # Two segments: the event name with no booker at all.
    check("no booker", split_title("227/EY INFO SESSION"),
          ("EY INFO SESSION", ""))
    # Everything that is not this report's shape passes through untouched.
    check("plain course title", split_title("RSM6307 Marketing"),
          ("RSM6307 Marketing", ""))
    check("system booking", split_title("Rotman System Booking"),
          ("Rotman System Booking", ""))
    check("empty", split_title(""), ("", ""))
    check("a bare code with nothing after it", split_title("208/"),
          ("Booking", ""))
    # clean_title runs at ingest, but the split is safe on raw input too.
    check("ZZ before the code", split_title("ZZ/208/CIBC.1/A.MAHAJAN"),
          ("CIBC.1", "A.MAHAJAN"))


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


def test_is_report_csv() -> None:
    """A download is the report only if its header names the report's columns.

    Issue #14: an HTML error page saved as the download parsed to zero events,
    and zero events from the export is a trusted "nothing booked".
    """
    print("\nis_report_csv")
    head = "Room,Event/Course,Date,Start Time,End Time,Class Code,Comment\n"
    check("the report's header", is_report_csv(head), True)
    check("...with rows", is_report_csv(head + '"142","A","1-Mar-2026"\n'), True)
    check("...behind a BOM", is_report_csv("﻿" + head), True)
    check("...semicolon-separated", is_report_csv(head.replace(",", ";")), True)
    check("...in another case", is_report_csv(head.upper()), True)
    check("...with the aliases", is_report_csv("Room Number,Course,Date\n"), True)
    check("an HTML page", is_report_csv("<!DOCTYPE html><html><body>x</body>"
                                        "</html>"), False)
    check("an empty file", is_report_csv(""), False)
    check("a header without Date", is_report_csv("Room,Event/Course\n"), False)


def test_cancelled_spellings() -> None:
    print("\ncancellation spellings")
    # The report spells it "CANCLD" / "Cancld", leading the Comment field.
    # The old pattern was "cancel|cnxld|cncld", and "cancl" is not a substring
    # of "cancel" — so nothing ever matched and every cancelled booking showed
    # as live.
    for comment in ("CANCLD DCIT 07.29", "Cancld may 11", "CANCELLED", "cnxld"):
        check(f"flagged: {comment!r}", _one(comment)["cancelled"], True)
    # The marker the report puts ahead of it on some rows.
    for comment in ("ZZ/ CANCLD 07.29", "ZZ/Cancld", "  CANCLD  may 11"):
        check(f"flagged behind the ZZ marker: {comment!r}",
              _one(comment)["cancelled"], True)
    # The word written out, and the token with the date run straight onto it.
    # The boundary is `(?![A-Za-z])`, not `\b`, for this second one: a digit
    # ends the marker just as a space does.
    for comment in ("Cancelled 07.29", "Canceled", "CANCELD", "CANCLD07.29"):
        check(f"flagged: {comment!r}", _one(comment)["cancelled"], True)

    # The direction the anchor exists for. The Comment field is free text, so
    # a substring search reads any booking that merely *mentions* cancellation
    # as cancelled — and a cancelled booking is never stored, so it disappears
    # with nothing to say why. "CAT/ CLEAN" is the easy negative; these are the
    # ones a substring search got wrong.
    for comment in ("CAT/ CLEAN",
                    "RSM6307 Cancellation Policy",
                    "Course topic: cancellation modelling",
                    "Dept meeting re cancelled flights",
                    "AV UPGRADES - cnxld vendor on site"):
        check(f"not flagged: {comment!r}", _one(comment)["cancelled"], False)

    # The other direction, and the one an *anchor alone* does not close: the
    # pattern is free to match the opening letters of any longer word that
    # happens to lead the field. Every one of these was read as cancelled
    # before the stem had to end where it does, so each booking was deleted
    # with no trace — the same loss as the substring search above, reached
    # from the other side. The line between these and "Cancelled 07.29" is
    # verb against noun: the report's marker is the verb, or its abbreviation.
    for comment in ("Cancellation Policy discussion",
                    "Cancellations this term",
                    "Cancellation of the previous booking",
                    "Cancelation request",
                    "Cancelling the booking",
                    "Cancellations - see email"):
        check(f"not flagged, it only opens with the stem: {comment!r}",
              _one(comment)["cancelled"], False)


def test_a_mentioned_cancellation_does_not_drop_the_booking() -> None:
    """The consequence, not the flag: the booking is still on the calendar.

    parse.py setting `cancelled` is only half of it — `_do_scrape` drops every
    event the flag is set on, so a false positive is a deleted booking rather
    than a mislabelled one. Asserted through the store path because that is
    where the loss happens: a comment that mentions cancellation is a real
    course, and it has to come back from parse_csv and survive the filter.

    The first row's comment *leads* with the stem, which is the shape the
    anchor alone let through: "Cancellation" opening the field matched as if
    it were the marker, and the course the flag names vanished.
    """
    print("\na mentioned cancellation keeps its booking")
    csv_text = (
        "Room,Event/Course,Date,Start Time,End Time,Building,Class Code,Comment\n"
        "RT 1065,RSM2600 Cancellation Policy,04-August-26,900,1200,RT,A,"
        "Cancellation policy discussion\n"
        "RT 1065,RSM2602 Ops,04-August-26,1200,1259,RT,A,"
        "Course topic: cancellation modelling\n"
        "RT 1065,RSM2601 Finance,04-August-26,1300,1600,RT,A,CANCLD DCIT 07.29\n"
    )
    events = parse_csv(csv_text)
    check("all three rows parsed", len(events), 3)
    check("the course about cancellation is not cancelled",
          events[0]["cancelled"], False)
    check("...nor the one that only mentions it", events[1]["cancelled"], False)
    check("...and the genuinely cancelled one is", events[2]["cancelled"], True)
    kept = [e for e in events
            if e.get("room") != "" and not e.get("cancelled")]
    check("both courses survive to storage", len(kept), 2)
    check("...and they are the right two",
          [e["title"] for e in kept],
          ["RSM2600 Cancellation Policy", "RSM2602 Ops"])


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
    test_twelve_hour_clock()
    test_rooms()
    test_cleanup()
    test_split_title()
    test_csv()
    test_is_report_csv()
    test_cancelled_spellings()
    test_a_mentioned_cancellation_does_not_drop_the_booking()
    test_all_day()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
