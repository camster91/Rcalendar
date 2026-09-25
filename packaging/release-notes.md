# Rotman LSM Calendar v1.2.0

A read-only viewer for Rotman LSM room bookings. This release makes updating
itself effortless — tokenless, and with the download done before you click —
and hardens the updater, the pages' accessibility and the failure honesty
of everything in between.

## What's new

### Updates are tokenless: a public releases mirror

The app's own repository is private, and GitHub cannot serve a release
publicly while its repo is not — which, until now, meant every machine
needed a paste-once GitHub token just to see whether an update existed.
Releases are now also published to a public mirror repository that holds
nothing but release artifacts (the installer, its checksum, the signing
certificate) — and the app reads that mirror. Checks and downloads need no
token and no setup on any machine. A pasted token still works and is still
stored encrypted (Windows DPAPI) for the day the mirror is ever made
private; it is a fallback now, not a requirement.

### The update is downloaded before you click

A check that finds a newer release now downloads the installer and verifies
it against the checksum the release itself published, right then. When you
click **Install update**, the installer starts — no 42 MB wait. A
pre-download that fails (offline, a flaky link) changes nothing: the offer
still stands, the sidebar says why the click will take longer, and the
click downloads the old way.

### The updater survived a review round

A proxy-truncated download used to kill the update worker with a raw
traceback; it is now the honest "could not reach GitHub" sentence, like
every other network fault. A skipped version is withdrawn from the sidebar
the moment you skip it, not at the next poll. A failed check with a known
newer release offers **Try again**; a failed install relabels the button
immediately; the sidebar says whether a GitHub token is already saved and
only offers to clear one that exists.

### The pages' controls are real controls

The search dropdown no longer closes while you are still using it. Preset
chips are real buttons. Week-view bars are buttons reachable by keyboard,
with names a screen reader can speak; all-day blocks open their details
with Enter and close them with Escape; the update toast and the sidebar
announce themselves politely. A dead status fetch no longer leaves the last
session reading standing as if it were live — the line retracts to
"Session unknown" and says the app is unreachable.

### The list page follows the data

The list view now refreshes itself when the 06:00 scrape lands behind its
back, so an overnight cancellation does not wait for a reload to be
respected.

The test suite now stands at 1080 assertions across eight suites; each
fix was verified by breaking it on purpose first.

## Install (per-user, no UAC)

1. Download `RotmanLSMCalendar-Setup-1.2.0.exe` and run it. The binaries
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
from 1.1.1 and 1.1.2 untouched — the installer does not touch it. Machines
running v1.1.2 can update from the app itself: **Check for updates** in the
tray (that version needs a saved GitHub token; from this release on, none
is needed).

This release was built, signed and self-tested locally — signing needs the
project's code-signing certificate, which lives in the build machine's user
store; a cloud runner cannot hold it. `sha256.txt` beside the installer
carries the hash below, and it is the hash of the *signed* installer: the
app verifies what it downloads against this file, which is why it is taken
after signing.

Note for managed machines: signing with a self-signed certificate gives
the binaries a stable publisher identity — one an endpoint agent can be
told to trust by certificate rather than by file path — but it is not
the reputation a CA-issued certificate carries. Whether this build
avoids the endpoint detections the unsigned builds collected is
re-measured, not assumed; check the quarantine count, not the threat
history.

SHA-256: `SHA256_PLACEHOLDER`