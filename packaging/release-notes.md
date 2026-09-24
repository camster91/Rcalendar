# Rotman LSM Calendar v1.1.1

A read-only viewer for Rotman LSM room bookings. This release cleans the
interface, sharpens what a booking is called, and makes the app measurably
faster on its hottest paths.

## What's new

### Titles, cleaned
LSM stores a booking as `208/CIBC.1/A.MAHAJAN` — an internal code, the event
name, and who booked it. Cards and list rows now print **CIBC.1 — A.MAHAJAN**:
the code is dropped, the booker stays inline on the card, and extra segments
travel with the booker (`007/PRE-TERM MTG/CHENG/D'ANGEL` → "PRE-TERM MTG —
CHENG · D'ANGEL"). The parse runs on the way out, not at ingest — the raw
title is what the change feed pairs bookings by — and search still matches
the full stored title, so searching for any part of it works as before.

### Exports, removed
The `.ics` and JSON downloads are gone, along with the code behind them. The
list view's shareable URLs cover the sharing cases the downloads did; the
`/download` endpoints no longer exist.

### Faster under the hood
- **Free-now drawer / free-at filter** no longer reads a rolling year of
  bookings every 60-second poll — it reads only the day (or days) the answer
  is about.
- **Search suggestions** no longer read the whole table on every keystroke;
  they're answered in SQL, and a suggestion now finds exactly what search
  finds.
- **The month grid** buckets bookings by day once per render instead of
  scanning the full list once per cell.
- **The list view** sorts with one collation, cached per sort key, instead of
  re-sorting every filtered render.

## Install (per-user, no UAC)

1. Download `RotmanLSMCalendar-Setup-1.1.1.exe` and run it. Windows may warn
   that the publisher is unknown — the installer is unsigned. "More info →
   Run anyway" is the way past it.
2. First launch: use the tray item **Sign in to LSM** for the UofT SSO + Duo
   sign-in. The app never stores a password; the browser profile holds the
   session.

Your data folder (bookings database, groups, saved filters) carries over from
1.1.0 untouched — the installer does not touch it.

This release was built and self-tested by GitHub Actions from tag v1.1.1;
`sha256.txt` beside the installer carries the hash below.

SHA-256: `SHA256_PLACEHOLDER`