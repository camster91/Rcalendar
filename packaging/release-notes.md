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

### The calendar cannot be blanked by a chip any more

Two paths could leave zero rooms selected — a state the calendar itself
treats as unsayable, and the one roomClear's fallback exists to prevent.
Turning the last selected group off recomputed the room selection from
the now-empty group set and blanked the calendar while the All button
lit beside it; Ctrl-clicking the last selected room out did the same
through the accumulate path. Both now fall back to every room, and a
restored group link (rooms win, the group chip lit but inert) unclicks
without taking the rooms with it.

### Group buttons speak their pressed state, on both pages

The calendar's sidebar group buttons showed the selection with colour
alone. They carry `aria-pressed` now, like the room chips always did and
the list page's group buttons already did.

### A preset that names only a group selects that group's rooms

Applying a preset that named a group but no rooms lit the group over the
whole building — a state the page's own link writer cannot express, so
the URL the screen rewrote itself to narrowed to the group on reload,
and the List tab opened on a different calendar than the one behind it.
Presets now make the same rooms-from-group inference a link does.

### Commas are refused in group names

The comma is the shared-link vocabulary's join *and* its split, so a
group name containing one was silently dropped out of every link it
landed in — the group worked in-session and vanished from the URL.
Refused at the editor and at the API, with the reason named.

### Checks for updates, and installs them

The app now looks at the project's GitHub releases on startup and once
a day after that, plus any time you ask it to — the tray's **Check for
updates** item, or the Calendar sidebar's *Updates* section. When a
newer release is published, an amber banner appears in the toolbar and
the sidebar offers **Install update**: the app downloads the release's
installer, checks it against the checksum the release itself published,
and only then runs it — a download that does not match is never run.
The installer asks the app to close when it is ready to copy files, and
your data folder is untouched throughout.

Skipped a release on purpose? *Skip this version* hides it until
something strictly newer ships.

Because the repository is private, the check needs a read-only GitHub
token — paste it once into the sidebar's *GitHub token…* row and it is
stored encrypted (Windows DPAPI, unreadable outside this Windows
account), and never displayed again. Without a token the check fails
softly with one honest sentence instead of pretending to work.

### The binaries are signed

Both the installer and the app inside it are now Authenticode-signed
with the project's code-signing certificate. It is self-signed, so
"unknown publisher" prompts persist on machines that have not imported
the certificate — see Install below; two imports close that for good.

The test suite now stands at 1003 assertions across eight suites; each
fix was verified by breaking it on purpose first.

## Install (per-user, no UAC)

1. Download `RotmanLSMCalendar-Setup-1.1.2.exe` and run it. The binaries
   are Authenticode-signed with the project's self-signed code-signing
   certificate, so a machine that has never seen it still says "unknown
   publisher" — "More info → Run anyway" is the way past it, and the
   prompt stops for good once you import the shipped certificate (step
   3, optional, per-user, no admin).
2. First launch: use the tray item **Sign in to LSM** for the UofT SSO +
   Duo sign-in. The app never stores a password; the browser profile holds
   the session.
3. Optional, to stop the "unknown publisher" prompts: download
   `RotmanLSMCalendar-CodeSigning.cer` from the same release and import it
   into your own Trusted Root and Trusted Publishers stores, per-user and
   with no admin rights, in PowerShell:

       Import-Certificate -FilePath .\RotmanLSMCalendar-CodeSigning.cer `
         -CertStoreLocation Cert:\CurrentUser\Root
       Import-Certificate -FilePath .\RotmanLSMCalendar-CodeSigning.cer `
         -CertStoreLocation Cert:\CurrentUser\TrustedPublisher

   Windows asks for confirmation on the Root import, and it should: that
   is the real trust decision — "treat this certificate as a root I trust",
   for your user account only. Trusted Root alone is what makes Windows
   validate the signature; Trusted Publishers is what stops the run
   prompt. (An earlier draft of this step named Trusted People instead;
   that store does not do it — measured: the signature still reads as
   untrusted with the certificate in Trusted People, which is why this
   step names the two stores above.)

Your data folder (bookings database, groups, saved filters) carries over
from 1.1.1 untouched — the installer does not touch it.

This release was built, signed and self-tested locally — GitHub Actions
ran out of included minutes, and the Actions release path remains the
supported one; it will be used again once billing is restored.
`sha256.txt` beside the installer carries the hash below, and it is the
hash of the *signed* installer: the app verifies what it downloads
against this file, which is why it is taken after signing.

Note for managed machines: signing with a self-signed certificate gives
the binaries a stable publisher identity — one an endpoint agent can be
told to trust by certificate rather than by file path — but it is not
the reputation a CA-issued certificate carries. Whether this build
avoids the endpoint detections the unsigned builds collected is
re-measured, not assumed; check the quarantine count, not the threat
history.

SHA-256: `SHA256_PLACEHOLDER`