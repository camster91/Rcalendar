"""
The in-app updater — checks GitHub for a newer release and installs it.

The app is otherwise read-only toward LSM; this module is the one place it
reaches outward to GitHub, so every network byte it moves goes through a
single function (`_fetch`) that tests can replace. Nothing here knows about
Flask, the tray, or the window: the scheduler drives it and decides what the
status dict says; this module only does the work and says why it could not.

Releases live on a public mirror repository (config.UPDATES_REPO) that
holds nothing but release artifacts — the app's own repository is private,
and GitHub cannot serve a release publicly while its repo is not — so the
listing and the asset downloads need no token. A token, when the user has
pasted one, is still sent on every request so the mirror keeps working the
day it is ever made private. Downloads go through the API's asset endpoint
(/repos/<repo>/releases/assets/<id> with Accept: application/octet-stream)
rather than browser_download_url on purpose: that endpoint answers 302 to a
pre-signed URL that needs no Authorization header, so the token only ever
travels to api.github.com, and whichever way urllib handles the
Authorization header across a cross-host redirect — recent Pythons strip it
— the outcome is the same. (See also dpapi.py for how the token itself is
held: never in cleartext, exactly like the session snapshot.)
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from app.config import (APP_VERSION, DATA_DIR, SIGNING_THUMBPRINTS,
                        UPDATES_REPO, log)
from app import dpapi

GITHUB_API = "https://api.github.com"
TOKEN_FILE = DATA_DIR / "github-token.bin"
STAGE_DIR = DATA_DIR / "update-staged"

# The release's sidecar and the installer it describes. Both names are
# written by packaging/release-local.ps1; matching on the prefix rather than
# the version keeps this from re-declaring what the release already knows.
SHA256_ASSET = "sha256.txt"
SETUP_PREFIX = "RotmanLSMCalendar-Setup-"

# A 64 KiB read is large enough that a 42 MB installer is not thousands of
# syscalls and small enough that progress reports land continuously.
_CHUNK = 65536


class UpdateError(Exception):
    """Something the user can act on. args[0] is a plain sentence, never a
    traceback fragment — the sidebar prints it as-is, and a message like
    'HTTP 404' is not something anyone can act on."""


# ── Version comparison ────────────────────────────────────────────────────

def parse_version(tag: str) -> tuple[int, ...] | None:
    """'v1.1.2' -> (1, 1, 2). Anything with a non-integer part -> None.

    A prerelease tag ('1.2.0-rc1') or plain garbage is not an update to
    install, and must not be *treated* as one either: None means 'ignore
    this release entirely' wherever it flows, so a mis-tagged release can
    never offer itself to the user half-parsed.
    """
    tag = tag.strip()
    if tag[:1] in ("v", "V"):
        tag = tag[1:]
    parts = tag.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_newer(candidate: str, current: str) -> bool:
    """True only if candidate is strictly newer than current.

    Tuples are zero-padded to the same length so 1.1 and 1.1.0 are the same
    version rather than an offer to 'upgrade' between spellings of it. None
    on either side is False — an unparsable tag is never an update.
    """
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# ── The one network touchpoint ─────────────────────────────────────────────

# Everything a socket can raise mid-transfer that is not an HTTP status:
# IncompleteRead (a proxy truncating the body — an HTTPException, not an
# OSError, which is why it is named here rather than inherited) belongs in
# the same sentence as a refused connection, because both say "the
# transfer died, try again".
_NET_ERRORS = (urllib.error.URLError, http.client.HTTPException,
               TimeoutError, OSError)


def _unreachable(exc: BaseException) -> UpdateError:
    return UpdateError(
        f"Could not reach GitHub ({exc}). Updates need outbound HTTPS to "
        "api.github.com."
    )


def _fetch(url: str, token: str | None = None,
           accept: str = "application/json", timeout: float = 20.0,
           on_data: Callable[[bytes, int | None], None] | None = None) -> bytes:
    """GET a URL and return the bytes. The only network call in the app's
    update path — tests monkeypatch this attribute to stay offline.

    `on_data(chunk, total)` is how a large download reports progress: when
    given, the body is read in chunks and handed over as it arrives, with
    `total` the Content-Length when the server names one and None when it
    does not (a chunked response is not an error, just an unknown size).

    Every failure the app can meet here is folded into an UpdateError with a
    sentence the sidebar can print: the HTTP code alone says nothing about
    what to do next, and '404' reads as a bug where 'the repo is private
    and no token is set' reads as an instruction.
    """
    headers = {
        "Accept": accept,
        "User-Agent": f"RotmanLSMCalendar/{APP_VERSION}",
    }
    if token:
        # Only ever sent to api.github.com (see the module docstring for why
        # asset downloads are routed so that it never needs to leave it).
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)

    # Each read is translated on its own, and the on_data callback is
    # deliberately called OUTSIDE every translation below it: the callback
    # writes the chunk to disk, and a disk-full write is not a network
    # failure — rewriting it as one would tell the user to check their
    # firewall while their disk is full. Its exceptions propagate raw.
    def _read(count: int | None) -> bytes:
        try:
            return resp.read(count)  # type: ignore[union-attr]
        except _NET_ERRORS as exc:
            raise _unreachable(exc) from exc

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 404:
            # The updates repo is public by design, so the same 404 means two
            # different things and the token in hand is the one fact that
            # separates them. Without one it most often means the repo has
            # no published release yet (or has been made private — the
            # sentence names that fix); with a token that just worked on the
            # listing, the 404 is the release's own doing — an asset the
            # notes promised but the release does not carry — and sending
            # that user to paste a token they demonstrably have is false
            # advice.
            if token:
                raise UpdateError(
                    "GitHub reports nothing found even with the token set — "
                    "the release is missing a file the updater needs."
                ) from exc
            raise UpdateError(
                "GitHub reports nothing found. The updates repository has "
                "no published releases yet — or it is private and needs a "
                "read-only GitHub token (paste one in the sidebar's "
                "Updates section)."
            ) from exc
        if code in (401, 403):
            raise UpdateError(
                "GitHub rejected the request — the token was refused or "
                "cannot read this repository. Check it is a fine-grained "
                "token with read access to the repo."
            ) from exc
        raise UpdateError(
            f"GitHub answered HTTP {code} and the update could not be read."
        ) from exc
    except _NET_ERRORS as exc:
        raise _unreachable(exc) from exc

    # No try around the body: _read has already translated the network
    # errors, and the on_data callback's exceptions are the point above.
    with resp:
        raw_total = resp.headers.get("Content-Length")
        total = (int(raw_total) if raw_total and raw_total.isdigit()
                 else None)
        if on_data is None:
            return _read(None)
        chunks: list[bytes] = []
        done = 0
        while True:
            chunk = _read(_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            done += len(chunk)
            on_data(chunk, total if total is not None else done)
        return b"".join(chunks)


def _asset(release: dict[str, Any], name: str,
           prefix: bool = False) -> dict[str, Any]:
    """Find one asset by exact name (or prefix) on a release dict."""
    for asset in release.get("assets") or []:
        if (asset.get("name", "").startswith(name) if prefix
                else asset.get("name") == name):
            return asset
    kind = "starting with" if prefix else "named"
    raise UpdateError(
        f"The release has no asset {kind} '{name}' — it was published "
        "without the file the updater needs."
    )


def _asset_url(asset: dict[str, Any]) -> str:
    return f"{GITHUB_API}/repos/{UPDATES_REPO}/releases/assets/{asset['id']}"


# ── Release listing, hash, download ───────────────────────────────────────

def fetch_latest(token: str | None) -> dict[str, Any]:
    """The latest published release, reduced to what the app needs:
    tag, name, notes URL and the asset list (id, name, size)."""
    body = _fetch(f"{GITHUB_API}/repos/{UPDATES_REPO}/releases/latest",
                  token=token, accept="application/vnd.github+json")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("GitHub's answer was not valid JSON.") from exc
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise UpdateError("GitHub's answer had no release in it.")
    return {
        "tag": str(data.get("tag_name", "")),
        "name": str(data.get("name", "")),
        "notes_url": str(data.get("html_url", "")),
        "assets": [
            {"id": a.get("id"), "name": str(a.get("name", "")),
             "size": a.get("size")}
            for a in data.get("assets") or []
        ],
    }


def select_setup(release: dict[str, Any]) -> dict[str, Any]:
    """The release's installer asset. One selection site, so the hash
    check and the download can never disagree about which file they mean."""
    asset = _asset(release, SETUP_PREFIX, prefix=True)
    if not str(asset.get("name", "")).endswith(".exe"):
        raise UpdateError(
            "The release's setup asset is not the installer — its name does "
            "not end in .exe."
        )
    return asset


def fetch_sha256(release: dict[str, Any], token: str | None,
                 setup_name: str) -> str:
    """The hash the release itself claims for its installer.

    Read straight from the release's sha256.txt sidecar, so the check below
    is not the app trusting its own download — it is the app checking its
    download against what the publisher published, which is the only
    version of the check worth having.

    The sidecar's second field names the file the hash describes; it is
    compared against `setup_name` because the installer is chosen by prefix
    and the sidecar is not — a release whose two assets disagree (a rebuilt
    Setup uploaded beside the old one, a sidecar left over from the last
    version) would otherwise hand every client a download that fails
    verification byte-identically, forever, under a message that calls a
    broken release a bad network.
    """
    asset = _asset(release, SHA256_ASSET)
    body = _fetch(_asset_url(asset), token=token,
                  accept="application/octet-stream")
    first = body.decode("utf-8", "replace").splitlines()[0].strip() if body.strip() else ""
    parts = first.split()
    if len(parts) < 2 or len(parts[0]) != 64:
        raise UpdateError(
            "sha256.txt does not hold a hash — the release's checksum file "
            "is malformed."
        )
    if parts[1] != setup_name:
        raise UpdateError(
            f"The release's checksum file describes '{parts[1]}' but the "
            f"installer is '{setup_name}' — the release is inconsistent. "
            "Nothing can be installed from it until it is republished."
        )
    return parts[0].lower()


def download_setup(release: dict[str, Any], token: str | None,
                   on_progress: Callable[[int, int | None], None]) -> Path:
    """Download the release's installer into the staging directory.

    The bytes land in '<name>.part' and are renamed only once the stream
    closed, so an interrupted download can never leave a half-installer
    under the real name — a file that *looks* like the thing the next click
    would run is worse than no file at all.
    """
    asset = select_setup(release)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    final = STAGE_DIR / str(asset["name"])
    part = final.with_suffix(".exe.part")

    done = 0
    with open(part, "wb") as fh:

        def _store(chunk: bytes, total: int | None) -> None:
            nonlocal done
            fh.write(chunk)
            done += len(chunk)
            on_progress(done, total)

        _fetch(_asset_url(asset), token=token,
               accept="application/octet-stream", timeout=60.0,
               on_data=_store)
    os.replace(part, final)
    return final


def verify_sha256(path: Path, expected: str) -> bool:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected.lower()


# How an update runs its installer. /SILENT is a progress bar and nothing
# else — the person already chose to update, so the wizard's pages (and its
# re-offer of the desktop shortcut) are noise; the task choices of the
# previous install are kept (Inno's UsePreviousTasks). /RELAUNCH=1 is this
# project's own switch: installer.iss starts the app again when it sees it,
# because a silent install shows no "Launch" checkbox to tick.
INSTALLER_ARGS = ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1")


def launch_installer(path: Path) -> None:
    """Start the installer detached and return immediately.

    The installer gates its file copy on this app closing (Inno's
    CloseApplications), so launching before the quit is not a race: the
    worst case is the installer *waiting* for the app's teardown, never the
    app running on top of replaced files. Detached, because the process
    that launched it is about to exit — the installer must survive that.
    """
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(path), *INSTALLER_ARGS], creationflags=flags,
                     close_fds=True)


# Read back with Windows' own Authenticode reader rather than parsed by hand.
# The path travels in an environment variable, never in the command text, so
# no file name can be read as PowerShell.
_SIGNATURE_PS = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:LSM_SIGNED_FILE; "
    "[pscustomobject]@{ status = [string]$s.Status; "
    "thumbprint = [string]$s.SignerCertificate.Thumbprint; "
    "timestamped = [bool]$s.TimeStamperCertificate } | ConvertTo-Json -Compress"
)


def _signature_facts(path: Path) -> dict[str, Any]:
    env = dict(os.environ, LSM_SIGNED_FILE=str(path))
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         _SIGNATURE_PS],
        capture_output=True, text=True, timeout=60, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    facts = json.loads(out.stdout.strip() or "{}")
    facts["trust"] = win_verify_trust(path)
    return facts


# WinVerifyTrust's answers that judge_signature accepts. S_OK is a chain the
# machine trusts. CERT_E_UNTRUSTEDROOT is the designed state of this app's
# self-signed certificate: the signature verifies, and the only complaint is
# that its root is in no trusted store. Get-AuthenticodeSignature folds that
# answer into UnknownError together with every other code it has no name for
# — a signer signature that does not verify among them — so the Status alone
# cannot tell "self-signed" from "forged". This code can.
TRUST_OK = 0
CERT_E_UNTRUSTEDROOT = 0x800B0109


def win_verify_trust(path: Path) -> int:
    """WinVerifyTrust's Authenticode verdict on a file, as an unsigned HRESULT.

    The generic-verify policy, no UI, no revocation check (a self-signed
    certificate has no CRL to ask). Raises off Windows; verify_signature
    reads that as a check that could not run, and refuses.
    """
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD),
                    ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE),
                    ("pgKnownSubject", ctypes.POINTER(GUID))]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD),
                    ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p),
                    ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD),
                    ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
                    ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE),
                    ("pwszURLReference", wintypes.LPWSTR),
                    ("dwProvFlags", wintypes.DWORD),
                    ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    # WINTRUST_ACTION_GENERIC_VERIFY_V2 {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                  (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0,
                                       0x4F, 0xC2, 0x95, 0xEE))
    file_info = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO),
                                   str(path), None, None)
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    data.dwUIChoice = 2            # WTD_UI_NONE
    data.fdwRevocationChecks = 0   # WTD_REVOKE_NONE
    data.dwUnionChoice = 1         # WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = 0         # WTD_STATEACTION_IGNORE: nothing to close

    wintrust = ctypes.WinDLL("wintrust")
    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID),
                                        ctypes.POINTER(WINTRUST_DATA)]
    wintrust.WinVerifyTrust.restype = wintypes.LONG
    result = wintrust.WinVerifyTrust(None, ctypes.byref(action),
                                     ctypes.byref(data))
    return result & 0xFFFFFFFF


def judge_signature(facts: dict[str, Any]) -> str | None:
    """None when the signature is one this app trusts, else why not.

    The gate is the one packaging/sign.ps1 applies to its own output, for
    the reason recorded there: Status is *not* the test. This certificate is
    self-signed, so its chain ends in itself and reads UnknownError on every
    machine that has not chosen to trust it — on purpose. What must hold is
    that the signature is intact (not HashMismatch, not NotSigned), that its
    signer is a pinned project certificate, and that it carries a timestamp
    (without one the signature dies with the certificate).

    "Intact" is WinVerifyTrust's own code (facts["trust"]), not the Status:
    UnknownError also covers a signer signature that does not verify, and a
    forged blob that merely embeds the public pinned certificate and a copied
    timestamp would show the right thumbprint and a timestamper all the same.
    """
    status = str(facts.get("status") or "")
    if status not in ("Valid", "UnknownError"):
        return (f"The downloaded installer's signature is {status or 'missing'}"
                f" — it will not be run.")
    if facts.get("trust") not in (TRUST_OK, CERT_E_UNTRUSTEDROOT):
        return ("The downloaded installer's signature does not verify — "
                "it will not be run.")
    thumb = str(facts.get("thumbprint") or "").upper()
    if thumb not in SIGNING_THUMBPRINTS:
        return ("The downloaded installer is not signed by this app's "
                "publisher — it will not be run.")
    if not facts.get("timestamped"):
        return ("The downloaded installer's signature has no timestamp — "
                "it will not be run.")
    return None


def verify_signature(path: Path) -> str | None:
    """Check a staged installer's Authenticode signature; None means trusted.

    Fails closed: a check that cannot run is a refusal, because the one thing
    this exists to stop is running an installer nobody vouched for.
    """
    try:
        facts = _signature_facts(path)
    except Exception as exc:
        log.error("could not read the installer's signature: %s", exc)
        return ("The installer's signature could not be checked — it will "
                "not be run.")
    problem = judge_signature(facts)
    if problem:
        log.error("installer signature refused: %s (%s)", problem, facts)
    return problem


def purge_staging() -> None:
    """Empty the staging directory at worker start.

    A staged installer left by a previous run belongs to a version that may
    already be behind the one now running, and 'ready' state from a dead
    process must not offer it as if it had been verified by this one.

    A file that cannot be deleted (locked by an installer still running
    from an interrupted update) is logged rather than skipped in silence:
    ignore_errors=True would make the except the caller wrote around this
    call dead code, and a purge that quietly failed leaves exactly the
    stale file it exists to remove.
    """
    if not STAGE_DIR.exists():
        return

    def _note(_func: Any, path: Any, exc: BaseException) -> None:
        log.warning("update staging purge could not remove %s (%s)",
                    path, exc)

    shutil.rmtree(STAGE_DIR, onexc=_note)


# ── The GitHub token ──────────────────────────────────────────────────────

def save_token(token: str) -> None:
    """Store the GitHub token, DPAPI-encrypted, like the session snapshot.

    Raises rather than swallowing: the write is *interactive* (the sidebar
    just asked for it), so silence would leave the user believing a token is
    set when nothing was written — the failure mode dpapi.protect exists to
    prevent. Whitespace is refused as a paste accident; a token with a
    newline in the middle is never what anyone meant to save.
    """
    if not token or len(token) > 200 or any(c.isspace() for c in token):
        raise ValueError(
            "A token is 1-200 characters with no whitespace — that does "
            "not look like a pasted token."
        )
    TOKEN_FILE.write_bytes(dpapi.protect(token.encode("utf-8")))
    log.info("github token stored (dpapi-encrypted)")


def load_token() -> str:
    """The stored token, or '' when there is nothing usable.

    Undecryptable is ignored, exactly like the session snapshot: a blob that
    belongs to another user or machine is not something to act on, and the
    honest state is 'no token', which the sidebar shows.
    """
    if not TOKEN_FILE.exists():
        return ""
    try:
        return dpapi.unprotect(TOKEN_FILE.read_bytes()).decode("utf-8")
    except OSError:
        log.warning("github token undecryptable, ignoring")
        return ""
    except UnicodeDecodeError:
        log.warning("github token not valid text, ignoring")
        return ""


def clear_token() -> None:
    TOKEN_FILE.unlink(missing_ok=True)
    log.info("github token cleared")