# Rotman LSM Calendar v1.3.0

A read-only viewer for Rotman LSM room bookings. This release is the one
for the whole office: anyone can install it on their own PC and sign in
with their own UTORid, two people can share a PC without seeing each
other's calendar, and updates install themselves quietly — and only if
this app's publisher signed them.

## What's new

### Every office account, on every PC

- **Two people, one PC.** Each Windows account's copy now has its own
  local web address and a per-launch key. Before, a second person's copy
  on a shared PC could open its window on the *first* person's calendar
  and live LSM session. Now each copy answers only its own window.
- **Uses the PC's own Microsoft Edge.** No more ~150 MB browser download
  per person at install time — the step most likely to fail on a managed
  PC. Where Edge is missing, the app still fetches Chromium as before.
  Existing installs move to Edge without signing in again.
- **"No access" is said plainly.** A UTORid that signs in to UofT but has
  no access to LSM's Rotman report used to loop through "session expired"
  or show a raw error every morning. It now says what is wrong and what to
  do (ask the LSM administrator), and starts working by itself the day
  access is granted.

### Updates install themselves, and only trusted ones

- An update is now checked against the release's checksum **and** its
  code-signing signature — the signer must be this project's pinned
  certificate — before anything runs.
- It then installs **silently** (a progress bar, no wizard pages, your
  previous choices kept) and starts the app again when it is done.
- The signature check asks Windows itself whether the signature is
  intact, not just whose certificate it names, and the installer's own
  signed version must match the release: an old installer can no longer
  be passed off as a new one. Both checks run again right before it starts.
- A GitHub token, if you set one, is only ever sent to GitHub's API, never
  to the download servers it redirects to.
- Upgrades now clear out the previous version's program files, and turning
  off "start at sign-in" during a reinstall really removes it.

### Installer

- Starts the app **in the tray** at sign-in by default (the 06:00 refresh
  only happens while the app runs); the desktop shortcut is now opt-in.
- Add/Remove Programs links to the public releases page instead of a
  private repository nobody else can open.

### Bookings are read more carefully

- **No more lost bookings from one quoted comment.** A comment written in
  single quotes near the top of the report could make the app misread
  every later booking with a comma in it, drop those rows, and list them
  as cancelled. Fixed.
- **Late bookings keep their real end.** A booking that runs to or past
  midnight used to be stored as one hour long; **Free now** after midnight
  now sees it too.
- Numbers in comments such as "MBA 2026-2027" are no longer read as times.
- The **Changes** feed no longer lists a whole new month as "added" on the
  1st, and a booking the report marks cancelled is removed even when only
  part of the report could be read.

### The morning refresh

- A refresh that fails (no network yet, LSM down) is retried later that
  day as soon as the session answers, instead of waiting until tomorrow.
- Only a refresh at or after 6 AM counts as the day's, so an overnight
  sign-in no longer cancels it, and restarting the app does not run it
  twice.

### Window and tray

- The app no longer holds up Windows sign-out or shutdown.
- Opening it from the Start menu while it is already running shows the
  calendar instead of an "already running" message.
- The tray's status line now updates.
- Starts on PCs that use a network proxy.
- An open calendar tab left over from before a restart or update says so
  plainly, instead of asking you to sign in.

### Calendar and list

- **Week view:** bookings show their full length, bookings at the same
  time sit side by side instead of hiding each other, and late-evening
  bookings appear.
- An old link or saved filter naming rooms that no longer exist shows
  every room instead of a blank page.
- Clearing the search box clears the search; a failed "free at" check no
  longer blanks the calendar; the Changes view follows a background
  refresh; the copy button and the filter panel work from the keyboard.

### Also fixed

- An empty report is only believed when the report itself says so: a
  "no data found" phrase elsewhere on the page, or an error page saved as
  the download, can no longer turn a day's bookings into "nothing booked".
- The first-run card names what it is doing ("Checking your LSM session"
  vs "Fetching bookings"); a missing browser gets a sentence a person can
  act on.
- The calendar's filters apply through one path (editing a group's rooms
  now re-filters the calendar straight away), and the list page shares the
  calendar's room and group rules, so a filter means the same thing on
  both pages.

The test suite now stands at more than 1,450 assertions across eight
suites; each fix was verified by breaking it on purpose first.

## Install (per-user, no admin)

1. Download `RotmanLSMCalendar-Setup-1.3.0.exe` and run it. Windows
   SmartScreen says **"Windows protected your PC"**, because the installer
   is signed with the project's own self-signed certificate rather than a
   purchased one: choose **More info → Run anyway**. On a managed PC the
   endpoint agent may also report it; nothing is removed.
2. The app opens: click **Sign in to LSM**, sign in with **your own
   UTORid** and approve Duo. The app never stores a password; the browser
   profile holds the session. The first fetch takes a few minutes.
3. Optional, to stop the "unknown publisher" prompt: download
   `RotmanLSMCalendar-CodeSigning.cer` from this release and import it
   into your own Trusted Root and Trusted Publishers stores, per-user and
   with no admin rights, in PowerShell:

       Import-Certificate -FilePath .\RotmanLSMCalendar-CodeSigning.cer `
         -CertStoreLocation Cert:\CurrentUser\Root
       Import-Certificate -FilePath .\RotmanLSMCalendar-CodeSigning.cer `
         -CertStoreLocation Cert:\CurrentUser\TrustedPublisher

   Windows asks for confirmation on the Root import, and it should: that
   is the real trust decision, for your user account only. A managed PC's
   policy may block it; skipping it changes nothing but the prompt.

Your data folder (bookings, groups, saved filters) carries over untouched.
Copies on v1.2.0 update from the app itself (**Check for updates** in the
tray); that one update still shows the installer's pages, because it is
v1.2.0's updater that runs it. From v1.3.0 on, updates are silent.

This release was built, signed and self-tested locally — signing needs the
project's code-signing certificate, which lives in the build machine's user
store. `sha256.txt` beside the installer carries the hash below, taken
*after* signing: the app verifies what it downloads against this file, then
checks the signature itself.

SHA-256: `SHA256_PLACEHOLDER`
