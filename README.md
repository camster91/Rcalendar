# LSM Calendar

LSM Calendar is a read-only Windows app for viewing Rotman room bookings from the LSM report. It keeps a local calendar, shows what changed between refreshes, and helps staff find an available room. It cannot create, change, or cancel bookings.

## Install

Download the latest `RotmanLSMCalendar-Setup-<version>.exe` from the [public releases page](https://github.com/camster91/rotman-lsm-calendar-releases/releases/latest) and run it. The installer works per Windows user and does not require administrator access. Windows may show a SmartScreen warning because the installer uses the team's self-signed certificate.

On first launch, choose **Sign in to LSM**, then sign in with your own UTORid and approve Duo. The first calendar refresh can take a few minutes. The app uses your browser session; it does not store your password. If your account cannot open the Rotman Query report, ask the LSM administrator for access.

The app starts in the tray at sign-in by default and refreshes each morning. Use the tray or calendar sidebar to refresh, check your session, and install updates.

## What you can see

- Month, week, and day views of room bookings.
- Filters for room, group, floor, capacity, Panopto capture, and availability.
- A changes feed showing new, changed, and cancelled bookings.
- A **Free now** view for finding an available room.

Your calendar, browser session, and logs are stored per Windows account in `%LOCALAPPDATA%\RotmanLSMCalendar`. Open the calendar through the tray or Start menu; its local web address includes a session key and is not intended to be bookmarked.

## Developers and maintainers

The app is written in Python and serves its UI on the local machine. To set up a source checkout and start it:

```powershell
.\packaging\setup.ps1
.\.venv\Scripts\python.exe -m app.main
```

The source tree contains `app/` for the scraper, session handling, storage, updater, and local API; `web/` for the calendar UI; `tests/` for the Windows test suites; and `packaging/` for install and release scripts. GitHub Actions runs the test suites. Signed releases are built and published through `packaging/release-local.ps1`, which publishes the same installer to this private source repository and the public releases repository.

For the full operating, test, security, and release notes, see [the reference guide](docs/REFERENCE.md). The current source release version is in `app/config.py`.
