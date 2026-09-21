# Allow-list request — draft

A request to UofT IT to review the packaged build. **Everything below the rule
is the message itself**; copy it from there.

Fill in before sending — the bracketed fields are placeholders, not prose:

- `[YOUR NAME]`, `[DEPT]`, `[PHONE/EMAIL]`
- `[N USERS]` — say the real number; "just me" is a strong and honest answer if
  it is true, and a number you cannot stand behind is worse than a small one
- `[ASSET TAG]` if the machine has one

Send it **only if the exe is actually needed as a shipped artefact.** If it is
not, the request is unnecessary: the venv path is now the default, it compiles
nothing, and it triggers no detection. Saying so in the request is deliberate —
it gives the reviewer an easy answer and makes the ask smaller, which is what
gets it approved.

---

**Subject:** Allow-list request — RotmanLSMCalendar.exe, unsigned internal tool (reputation-based detection)

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
  Playwright, signed in by me with Duo. No other endpoints.
- **Credentials:** the sign-in happens in a normal browser window with my own
  credentials. The application never sees or stores my password. It keeps the
  resulting session cookie locally so it does not have to re-authenticate daily.
- **No telemetry, no update check, no analytics, no third-party services.**
- **Installs nothing.** No admin rights, no service, no driver, no scheduled
  task. Login startup is a shortcut in my own Startup folder.

**Why it is reported**

Unsigned, freshly compiled, low prevalence — the detection is reputation-based,
not behavioural. The console's own label is "Suspicious Activity · Detected
suspicious file", which is what an unknown unsigned binary looks like on first
sight.

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
| Signature | Not signed |
| Packaging | PyInstaller `--onedir`: a folder of 916 files (~145 MB), **not** a single-file self-extracting build |

**Detection record, 2026-09-21**

- Two console entries, both "Suspicious Activity · Detected suspicious file":
  the exe under `build\` and again under `dist\`, one second apart.
- **Quarantined files: 0.**
- The exe was intact at full size afterwards, and a 90-second dwell check found
  all 916 files unchanged — no partial quarantine. The app then launched
  normally with its window, its web UI and Chromium.

**What I am asking for**

1. Review the binary and, if you are satisfied, allow-list it keyed on path or
   publisher so it survives a rebuild; **or**
2. tell me signing is required, and whether a certificate from UofT's own PKI
   would be acceptable rather than a commercial one; **or**
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
