# Rotman LSM Calendar v1.1.2

A read-only viewer for Rotman LSM room bookings. This release fixes what a
shared link means when it names both rooms and groups — and, on the way, a
link's `groups=` parameter that had never worked on the list page at all.

## What's new

### One meaning for a link, on both pages

The calendar and the list page model a room group differently — the
calendar treats a group as shorthand for a room selection, the list treats
it as a filter of its own. A link carrying both `rooms=` and `groups=`
therefore meant different things depending on which page opened it: the
same URL could show a room's bookings on the calendar and nothing at all
on the list, or three bookings there and two here.

Both directions now agree. The list writes the combined result into
`rooms=` instead of a pair of parameters each page would read its own way,
and a link naming both is read the same way by both pages: the room list
is the more specific statement, and the group returns as a live filter
wherever it cannot change the answer.

### `groups=` links, fixed on the list page

Every link that named a group silently lost it on the list page — the
group was validated against the wrong list of names and dropped, so a
shared `?groups=North` link listed the whole building there while the
calendar showed North's rooms. Group links now mean the group on both
pages.

The test suite now stands at 799 assertions across seven suites; each fix
was verified by breaking it on purpose first.

## Install (per-user, no UAC)

1. Download `RotmanLSMCalendar-Setup-1.1.2.exe` and run it. Windows may
   warn that the publisher is unknown — the installer is unsigned. "More
   info → Run anyway" is the way past it.
2. First launch: use the tray item **Sign in to LSM** for the UofT SSO +
   Duo sign-in. The app never stores a password; the browser profile holds
   the session.

Your data folder (bookings database, groups, saved filters) carries over
from 1.1.1 untouched — the installer does not touch it.

This release was built and self-tested locally — GitHub Actions ran out of
included minutes, and the Actions release path remains the supported one;
it will be used again once billing is restored. `sha256.txt` beside the
installer carries the hash below.

SHA-256: `SHA256_PLACEHOLDER`