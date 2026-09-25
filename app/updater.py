"""
The in-app updater — checks GitHub for a newer release and installs it.

The app is otherwise read-only toward LSM; this module is the one place it
reaches outward to GitHub, so every network byte it moves goes through a
single function (`_fetch`) that tests can replace. Nothing here knows about
Flask, the tray, or the window: the scheduler drives it and decides what the
status dict says; this module only does the work and says why it could not.

The repository is private, so both the release listing and the asset
downloads need a token. Downloads go through the API's asset endpoint
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
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from app.config import APP_VERSION, DATA_DIR, GITHUB_REPO, log
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_total = resp.headers.get("Content-Length")
            total = int(raw_total) if raw_total and raw_total.isdigit() else None
            if on_data is None:
                return resp.read()
            chunks: list[bytes] = []
            done = 0
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
                done += len(chunk)
                on_data(chunk, total if total is not None else done)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 404:
            raise UpdateError(
                "GitHub reports nothing found. The repository is private and "
                "no token is set (or has no releases) — paste a read-only "
                "GitHub token in the sidebar's Updates section."
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
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(
            f"Could not reach GitHub ({exc}). Updates need outbound HTTPS to "
            "api.github.com."
        ) from exc


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
    return f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/assets/{asset['id']}"


# ── Release listing, hash, download ───────────────────────────────────────

def fetch_latest(token: str | None) -> dict[str, Any]:
    """The latest published release, reduced to what the app needs:
    tag, name, notes URL and the asset list (id, name, size)."""
    body = _fetch(f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/latest",
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


def fetch_sha256(release: dict[str, Any], token: str | None) -> str:
    """The hash the release itself claims for its installer.

    Read straight from the release's sha256.txt sidecar, so the check below
    is not the app trusting its own download — it is the app checking its
    download against what the publisher published, which is the only
    version of the check worth having.
    """
    asset = _asset(release, SHA256_ASSET)
    body = _fetch(_asset_url(asset), token=token,
                  accept="application/octet-stream")
    first = body.decode("utf-8", "replace").splitlines()[0].strip() if body.strip() else ""
    if len(first.split()) < 1 or len(first.split()[0]) != 64:
        raise UpdateError(
            "sha256.txt does not hold a hash — the release's checksum file "
            "is malformed."
        )
    return first.split()[0].lower()


def download_setup(release: dict[str, Any], token: str | None,
                   on_progress: Callable[[int, int | None], None]) -> Path:
    """Download the release's installer into the staging directory.

    The bytes land in '<name>.part' and are renamed only once the stream
    closed, so an interrupted download can never leave a half-installer
    under the real name — a file that *looks* like the thing the next click
    would run is worse than no file at all.
    """
    asset = _asset(release, SETUP_PREFIX, prefix=True)
    if not str(asset.get("name", "")).endswith(".exe"):
        raise UpdateError(
            "The release's setup asset is not the installer — its name does "
            "not end in .exe."
        )
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


def launch_installer(path: Path) -> None:
    """Start the installer detached and return immediately.

    The installer gates its file copy on this app closing (Inno's
    CloseApplications), so launching before the quit is not a race: the
    worst case is the installer *waiting* for the app's teardown, never the
    app running on top of replaced files. Detached, because the process
    that launched it is about to exit — the installer must survive that.
    """
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(path)], creationflags=flags, close_fds=True)


def purge_staging() -> None:
    """Empty the staging directory at worker start.

    A staged installer left by a previous run belongs to a version that may
    already be behind the one now running, and 'ready' state from a dead
    process must not offer it as if it had been verified by this one.
    """
    shutil.rmtree(STAGE_DIR, ignore_errors=True)


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