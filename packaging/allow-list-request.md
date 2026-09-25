# Allow-list request — draft

A request to UofT IT to review the packaged build. **Everything below the rule
is the message itself**; copy it from there.

Fill in before sending — the bracketed fields are placeholders, not prose:

- `[YOUR NAME]`, `[DEPT]`, `[PHONE/EMAIL]`
- `[N USERS]` — say the real number; "just me" is a strong and honest answer if
  it is true, and a number you cannot stand behind is worse than a small one
- `[ASSET TAG]` if the machine has one
- `[SETUP NAME]` and `[SETUP SIZE]` — the installer's filename and size, which
  change with every version: `RotmanLSMCalendar-Setup-<APP_VERSION>.exe`, where
  `APP_VERSION` is the one place the version lives (`app/config.py`). For 1.0.0
  these were `RotmanLSMCalendar-Setup-1.0.0.exe` and 42.3 MB. Asking for a
  filename no build produces is how this request gets a yes that fixes nothing.

Send it **only if an exe is actually needed as a shipped artefact.** If it is
not, the request is unnecessary: the venv path is now the default, it compiles
nothing, and it triggers no detection. Saying so in the request is deliberate —
it gives the reviewer an easy answer and makes the ask smaller, which is what
gets it approved.

Two artefacts are listed below, because there are two binaries now. If
only one of them is ever going to leave this machine, delete the other's section
rather than leaving the reviewer to work out which one you mean.

Since v1.1.2 both binaries are Authenticode-signed with the project's
self-signed code-signing certificate, so the reviewer can key an exclusion on
**publisher** — the stable thing across rebuilds — rather than on a path or a
hash: subject `CN=Rotman LSM Calendar (self-signed code signing)`, thumbprint
`E3806812DB2DD17AAE80283E6353C5ACEA157124`. The certificate is created once
and reused for every build, so the thumbprint does not change from release to
release. The size and SHA-256 below are of the 2026-09-21 build the detection
record describes; they change with every rebuild, which is the whole reason the
request is not keyed on them.

---

**Subject:** Allow-list request — Rotman LSM Calendar, self-signed internal tool (reputation-based detection)

Hello,

I maintain a small internal tool that I run on my UofT-managed Windows machine,
and building it produces entries in the SentinelOne console. I would like it
reviewed and, if you agree it is benign, excluded — keyed on **path or
publisher rather than hash**, since every rebuild changes the hash and a
hash-keyed exclusion would expire with the next build.

To be clear, I am not asking to bypass a control. If your answer is that the exe
should not exist, that is a workable answer: the tool runs from a Python
virtualenv, which needs no exclusion at all, and I have made that the default
since this came up. The request covers only the case where it has to exist as a
shipped folder.

**What the software is**

- **Name:** Rotman LSM Calendar
- **What it does:** read-only viewer for room bookings from the Rotman LSM
  room-booking report (`lsm.utoronto.ca`). It reads a report I can already open
  with my own account and renders it as a calendar, so answering "is 134A free
  at 2?" does not mean re-running the report each time.
- **Origin:** written by me, with Claude Code assistance. Source lives at
  `C:\Users\ashleyc2\rotman-lsm-calendar`. Internal, not published, no
  commercial purpose.
- **Users:** [N USERS]

**Network behaviour — this is the whole surface**

- **Inbound:** none. It binds `127.0.0.1:8765` (loopback) to serve its own UI.
  It is not reachable from the network and has no authentication because it has
  no network surface. There is deliberately no host override in its config.
- **Outbound:** HTTPS to `lsm.utoronto.ca`, in a real Chromium window driven by
  Playwright, signed in by me with Duo. Since v1.1.2 it also checks the
  project's own GitHub releases for updates — HTTPS to `api.github.com`, and
  the release's installer download through the same API when an update is
  accepted (since v1.2.0 those requests name a public releases mirror
  repository, not the private source repository). No other endpoints.
- **Credentials:** the sign-in happens in a normal browser window with my own
  credentials. The application never sees or stores my password. It keeps the
  resulting session cookie locally so it does not have to re-authenticate daily.
  The optional GitHub token the update check can use (a fallback — the
  releases mirror is public) is stored encrypted with Windows DPAPI and sent
  only to `api.github.com`.
- **No telemetry, no analytics, no third-party services.** The update check is
  the one thing the app fetches beyond LSM, and it fetches it from the
  project's own release page.
- **Installs nothing.** No admin rights, no service, no driver, no scheduled
  task. Login startup is a shortcut in my own Startup folder.

**Why it is reported**

Self-signed, freshly compiled, low prevalence — the detection is
reputation-based, not behavioural, and a self-signed publisher is not a
CA-backed one, so the binaries score much like unknown ones on first sight.
The console's own label is "Suspicious Activity · Detected suspicious file",
which is what a low-prevalence binary looks like before it has any history.

The detections recorded below predate signing — those builds were unsigned.
Signing with a stable self-signed certificate is what makes a publisher-keyed
exclusion possible at all; whether it also reduces first-sight detection is
re-measured, not assumed.

The detection also fires when the file is **written**, not when it runs: the two
entries are timestamped 08:52:21 and 08:52:22, and the exe was written at
08:52:17. It was reported before anything executed it.

**The artefact**

| | |
|---|---|
| Name | `RotmanLSMCalendar.exe` |
| Path | `C:\Users\ashleyc2\rotman-lsm-calendar\dist\RotmanLSMCalendar\RotmanLSMCalendar.exe` |
| Size | 7,952,049 bytes |
| SHA-256 | `5B2237E7847B97F19F5DD0F9559947EFA7EED402F83DACA1C5CED7543BE42870` |
| Built | 2026-09-21 08:52:17 |
| Signature | Self-signed (Authenticode) since v1.1.2 — subject `CN=Rotman LSM Calendar (self-signed code signing)`, thumbprint `E3806812DB2DD17AAE80283E6353C5ACEA157124`; the 2026-09-21 build this row's size and hash describe was unsigned |
| Packaging | PyInstaller `--onedir`: a folder of 916 files (~145 MB), **not** a single-file self-extracting build |

**A second, newer artefact: the installer**

Since the report above, the tool has been given an installer, so there are now
two binaries rather than one — and a request that names only the exe
would be answered on the wrong file. This one was **not** flagged on this
machine. That is one observation and not a verdict, but it is the observation
that matters to a reputation scanner, because it survived being executed: the
single-file PyInstaller build was removed on execution here, and this one was
not.

| | |
|---|---|
| Name | `[SETUP NAME]` (for 1.0.0: `RotmanLSMCalendar-Setup-1.0.0.exe`) |
| Size | `[SETUP SIZE]` (for 1.0.0: 42.3 MB) |
| Signature | Self-signed (Authenticode) since v1.1.2 — same subject and thumbprint as the exe above |
| Packaging | Inno Setup 6.7.3, `PrivilegesRequired=lowest` — installs per-user under `%LOCALAPPDATA%\Programs`, so it raises no UAC prompt |

What it installs is a per-user copy of the app plus a Desktop shortcut and one
in the Startup folder. No service, no driver, no scheduled task, no machine-wide
change, and nothing written outside the user's own profile. The uninstaller asks
separately before deleting the data directory and defaults to keeping it. Its
only network activity on install is fetching Chromium into the per-user
Playwright cache, which is the same download the app would otherwise do on first
run.

**Detection record, 2026-09-21**

- Two console entries, both "Suspicious Activity · Detected suspicious file":
  the exe under `build\` and again under `dist\`, one second apart.
- **Quarantined files: 0.**
- The exe was intact at full size afterwards, and a 90-second dwell check found
  all 916 files unchanged — no partial quarantine. The app then launched
  normally with its window, its web UI and Chromium.

**What I am asking for**

1. Review the binaries and, if you are satisfied, allow-list them keyed on
   publisher (or path) so they survive a rebuild; the signing certificate's
   thumbprint above is the stable key for that; **or**
2. tell me a CA-issued certificate is required — the current self-signed one is
   deliberately not that, and I would like to know whether a certificate from
   UofT's own PKI is acceptable rather than a commercial one; **or**
3. tell me the exe is not needed and I will drop it — the venv path is what I
   use day to day and it triggers nothing.

I am happy to provide the source, a build log, or to unpack and run it on a
machine you nominate while someone watches the console.

Thank you,

[YOUR NAME]
[DEPT] · [PHONE/EMAIL]
[ASSET TAG]

---

## Note on what this request does not claim

It does not claim the agent is wrong, and it does not claim the build is proven
safe anywhere but on this machine. Detection here is reputation-driven, so the
folder shape rules out the *dropper* pattern, not the first impression. The
console is visible to me but the policy behind it is not, so the request reports
what the console showed and asks for a decision rather than arguing one.

Do not send anything that proposes repacking, padding, renaming, obfuscating or
delaying the binary to change how it is scored. That is evasion, on a managed
machine it escalates rather than resolves, and it would poison this request.
