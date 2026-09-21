; Rotman LSM Calendar — installer (Inno Setup 6).
;
; Built by packaging\build.ps1, which compiles the app with PyInstaller and then
; runs ISCC over this file. It can also be run by hand:
;
;   ISCC.exe packaging\installer.iss
;
; and it will compile, with the version defaulting to 0.0.0-dev. See the
; #ifndef below for why that default exists.
;
; PER-USER, ON PURPOSE. PrivilegesRequired=lowest installs into
; %LOCALAPPDATA%\Programs and never raises a UAC prompt, so a standard account
; can run this — which is the only kind of account this app has ever been
; built on, and half of what "install it on any computer" has to mean. It also
; keeps the installer out of the elevation path in a managed environment, where
; an unsigned exe asking for admin is the pattern that gets reported.
;
; What this deliberately does NOT do: bundle Chromium. The app's browser is
; ~150 MB and deliberately lives in the per-user Playwright cache rather than
; in the build (see the spec), so a machine that has never run this app has an
; empty cache — the app would install, start, serve the UI, and fail every
; scrape. The "browser" task below closes that gap by running the app's own
; --install-browser, and it is why the installer is not just a file copy.

#define AppName "Rotman LSM Calendar"
#define AppExeName "RotmanLSMCalendar.exe"
#define AppPublisher "camster91"
#define AppURL "https://github.com/camster91/rotman-lsm-calendar"

; build.ps1 reads APP_VERSION out of app/config.py and passes it here, so
; Add/Remove Programs shows the app's own version rather than a second one that
; drifts from it. The fallback is for a bare ISCC run, and says so rather than
; looking like a real release.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

; Windows' version resource wants four numeric parts and refuses anything else,
; which is why AppVersion above cannot simply be reused: "1.0.0" is three parts
; and "0.0.0-dev" is not numeric at all. Left as the default, ISCC compiles
; Setup.exe with a blank FileVersion -- ProductVersion is filled from
; AppVersion, so Add/Remove Programs is right either way, but Explorer's
; Details tab and anything that reads the resource directly would see nothing.
;
; Derived rather than declared, because a second hand-maintained version in
; this file is exactly the drift the AppVersion comment above exists to stop.
; The number of dots decides how many parts are still missing, so any plain
; x.y.z, x.y.z.w or shorter spells itself into a quad. The dev fallback is
; caught by its hyphen, and anything else unrecognised falls to a zero quad.
; Neither ISPP nor a malformed four-part string reaches ISCC: a version that is
; not numeric gets the zero quad rather than the compile error ISCC would raise,
; so this cannot become the reason a build stops working.
#define AppVersionDots Len(AppVersion) - Len(StringChange(AppVersion, '.', ''))
#if Pos('-', AppVersion) > 0
  #define AppVersionQuad "0.0.0.0"
#elif AppVersionDots == 3
  #define AppVersionQuad AppVersion
#elif AppVersionDots == 2
  #define AppVersionQuad AppVersion + ".0"
#elif AppVersionDots == 1
  #define AppVersionQuad AppVersion + ".0.0"
#elif AppVersionDots == 0
  #define AppVersionQuad AppVersion + ".0.0.0"
#else
  #define AppVersionQuad "0.0.0.0"
#endif

[Setup]
; Per-user, and this one line is the whole of it. Inno defaults to admin, so
; omitting it does not fall back to something merely different -- it resolves
; DefaultDirName below to Program Files and puts a UAC prompt in front of the
; install, which is the opposite of what the header of this file describes.
; ISCC says so rather than compiling quietly: with the default in force it
; warns that PrivilegesRequired "is set to admin" while per-user areas
; (userstartup) are used by the script. That warning is how the missing line
; was found -- the comment above had been describing a mode the script never
; installed in. Set to lowest, DefaultDirName resolves under
; LOCALAPPDATA\Programs instead.
PrivilegesRequired=lowest
; Never change this GUID: it is how Windows recognises an existing install and
; how the uninstaller is found. A different one installs a second copy beside
; the first instead of upgrading it.
AppId={{8F3A6C1E-5B47-4D2A-9E63-7C0B4A1D25F8}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersionQuad}
AppPublisher={#AppPublisher}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes
OutputDir=..\dist
OutputBaseFilename=RotmanLSMCalendar-Setup-{#AppVersion}
SetupIconFile=RotmanLSMCalendar.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; WebView2 (what pywebview renders in) needs Windows 10 1809 or later, so an
; older machine is refused here rather than left with a window that never
; appears.
MinVersion=10.0.17763
; x64compatible, not x64: the shorter spelling is deprecated in Inno Setup 6.3
; and gone in 7.x, so this one file compiles under both without an edit.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The app is a tray app: installing over a running copy would leave the exe
; locked. Restart Manager notices the lock and offers to close it, which is
; more reliable here than an AppMutex, because this app's single-instance lock
; is a file lock rather than a named mutex.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start {#AppName} when I sign in"; GroupDescription: "Options:"; Flags: unchecked
Name: "browser"; Description: "Download Chromium now (about 150 MB, needs internet)"; GroupDescription: "Options:"; Flags: checkedonce

[Files]
; The whole onedir build. _internal\ must travel with the exe — the exe alone
; is not the app — and it is one folder, not a single self-extracting file,
; which is the shape this machine's endpoint agents were measured to remove.
Source: "..\dist\RotmanLSMCalendar\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: autostart

[Run]
; The app's own browser fetch, so the download uses the Playwright driver that
; shipped inside this build rather than a second copy of node fetched from
; somewhere. runhidden because it is a long, chatty download and the wizard's
; own status line is the better progress indicator; waituntilterminated so the
; finish page is not shown while it is still running. A failure here is not a
; failed install — the app is on disk and --install-browser can be run again.
Filename: "{app}\{#AppExeName}"; Parameters: "--install-browser"; Tasks: browser; Flags: runhidden waituntilterminated; StatusMsg: "Downloading Chromium (about 150 MB)..."

Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: postinstall nowait skipifsilent

[Code]

const
  { The WebView2 runtime, as Microsoft documents it: a per-machine or per-user
    EdgeUpdate client key with a "pv" value. Checked so a machine without it
    gets told why the window will not open, rather than a silent nothing. }
  WebView2Machine = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2User = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function WebView2Present(): Boolean;
var
  Version: String;
begin
  { "pv" is set to the installed version; a key with no pv means a client that
    was registered and then removed. }
  Result :=
    (RegQueryStringValue(HKLM, WebView2Machine, 'pv', Version) and (Version <> '')) or
    (RegQueryStringValue(HKCU, WebView2User, 'pv', Version) and (Version <> ''));
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Present() then
    { A warning, not a refusal: the app also runs headless (--no-window), and
      the runtime may be installed by policy at first launch. }
    MsgBox('Windows''s WebView2 runtime was not found on this computer.' + #13#10 + #13#10 +
           'The app needs it to draw its window. Windows 11 and up-to-date Windows 10 ' +
           'installations already have it; if the window does not appear after installing, ' +
           'get it from Microsoft''s "WebView2 Runtime" page and run this app again.' + #13#10 + #13#10 +
           'Everything else — the local calendar, the scraping schedule — is set up regardless.',
           mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;

  { The bookings, the log, the Chromium profile holding the signed-in LSM
    session and the saved cookie snapshot live in the data directory, not in
    the install folder, so the uninstaller leaves them alone. Asked about
    rather than assumed: keeping them makes a reinstall seamless, and deleting
    them is a sign-out plus eleven months of history, which is not something to
    do to somebody without saying so.

    No brace characters below. In Inno's Pascal a brace comment does not nest,
    so writing the app-directory constant here would end the comment in the
    middle of a sentence -- which is what "Identifier expected" meant the first
    time this compiled. }
  DataDir := ExpandConstant('{localappdata}\RotmanLSMCalendar');
  if not DirExists(DataDir) then
    exit;

  if MsgBox('Also delete your saved bookings, settings and signed-in session?' + #13#10 + #13#10 +
            DataDir + #13#10 + #13#10 +
            'Choose No to keep them: reinstalling then picks up where you left off, ' +
            'with no need to sign in to LSM again.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    if not DelTree(DataDir, True, True, True) then
      MsgBox('Some files in that folder could not be removed. Delete the folder by hand if you want it gone:' + #13#10 + #13#10 + DataDir,
             mbError, MB_OK);
end;
