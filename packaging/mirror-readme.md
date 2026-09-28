# Rotman LSM Calendar

A Windows app that shows what's booked in Rotman rooms, refreshed from LSM
every morning. It is **read-only**: it never books, changes or cancels
anything. It never stores your password -- you sign in with your own UTORid
and Duo, and the app keeps the session.

## Install

No admin rights needed. Nothing else to install first.

1. Open **[the latest release](https://github.com/camster91/rotman-lsm-calendar-releases/releases/latest)**
   and download `RotmanLSMCalendar-Setup-<version>.exe`.
2. Run it. Windows SmartScreen will say **"Windows protected your PC"** --
   the installer is signed with the project's own certificate, not a
   purchased one. Choose **More info**, then **Run anyway**. On a managed
   PC the security software may also note it; that is expected.
3. Click through the installer. It installs just for you, in
   `%LOCALAPPDATA%\Programs\Rotman LSM Calendar`, and by default starts the
   app in the notification area (tray) when you sign in to Windows, so the
   6 AM refresh keeps happening.
4. When the app opens, click **Sign in to LSM**. A browser window opens:
   sign in with **your UTORid** and approve Duo. The app then fetches about
   a year of bookings -- the first run takes a few minutes.

The app uses your PC's own Microsoft Edge to talk to LSM, so there is no
separate browser to download.

## If something looks wrong

- **"This account can't open LSM's report"** -- you are signed in to UofT,
  but your UTORid has no access to LSM's Rotman bookings report. Signing in
  again will not fix it: ask the LSM administrator for access. (If you used
  the wrong UTORid, choose **Sign out** in the sidebar and sign in again.)
  The app checks once a day and starts working by itself once access is
  granted.
- **"Session expired"** -- the UofT sign-in lapsed. Click **Sign in to LSM**
  and approve Duo.
- **The window is gone** -- the app keeps running in the tray. Click its
  icon in the notification area (it may be under the ^ arrow), or open it
  from the Start menu.
- **Two people, one PC** -- each Windows account gets its own copy, its own
  sign-in and its own calendar; neither can see the other's.

## Updates

The app checks this page once a day and on request (tray: **Check for
updates**). An update is downloaded, checked against the release's
`sha256.txt` **and** its code-signing signature, and installed silently --
then the app starts again by itself. Your bookings and settings are kept.

## Optional: stop the "unknown publisher" prompt

Download `RotmanLSMCalendar-CodeSigning.cer` from the release and, in
PowerShell (no admin needed; it affects only your account):

    Import-Certificate -FilePath .\RotmanLSMCalendar-CodeSigning.cer -CertStoreLocation Cert:\CurrentUser\Root
    Import-Certificate -FilePath .\RotmanLSMCalendar-CodeSigning.cer -CertStoreLocation Cert:\CurrentUser\TrustedPublisher

Windows asks you to confirm the first one: that is the trust decision, for
your account only. A managed PC's policy may block it; skipping this step
changes nothing but the prompt.

## About this repository

This is an **artifacts-only mirror**: the installer, its `sha256.txt`
checksum and the code-signing certificate, nothing else. The app's source
and issue tracker are private, and GitHub cannot serve a release publicly
while its repository is not, so each release is published twice -- there and
here. The issue tracker is disabled on purpose: questions go to whoever gave
you this link.
