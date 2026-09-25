"""
The updater: version comparison, checksum verification, the download's
staging discipline, the token's encryption, the 24-hour cadence, and the
worker's four check outcomes plus the install path.

    python tests/test_update.py

Nothing here talks to GitHub. The one network touchpoint the app has
(updater._fetch) is swapped for fakes, and the one level below it
(urllib.request.urlopen) is swapped where the headers themselves are the
question. The app is otherwise read-only toward LSM; the updater is the
only place it reaches outward, so this suite is the record of what that
outward reach is allowed to do — and, in the install test, of what it
must refuse to run.

The install path is driven with fakes at every step except the hash check:
download_setup writes a real file, fetch_sha256 names a real hash, and
verify_sha256 reads the real bytes — because that check is the one that
decides whether an executable gets run, and its test must not fake the
part that matters.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Before app.config is imported: it opens the log file on import, and a test
# run must not write into the real data dir, the real token, or the real
# staging directory.
os.environ["LSM_DATA_DIR"] = tempfile.mkdtemp(prefix="lsm-update-")

from app import dpapi, scheduler, store, updater  # noqa: E402
from app.config import APP_VERSION  # noqa: E402
from app.server import create_app  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")


def ok(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


@contextlib.contextmanager
def patched(module, **overrides):
    """Swap attributes on a module and restore them after, like test_worker."""
    saved = {name: getattr(module, name) for name in overrides}
    for name, value in overrides.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


# ── 1. Version comparison ──────────────────────────────────────────────────

def test_versions() -> None:
    print("\nversion comparison")
    check("plain tag", updater.parse_version("v1.1.2"), (1, 1, 2))
    check("capital V is the same tag", updater.parse_version("V2.0"), (2, 0))
    check("no leading v", updater.parse_version("1.2"), (1, 2))
    check("two-digit part", updater.parse_version("v1.1.10"), (1, 1, 10))
    check("prerelease is ignored, not half-parsed",
          updater.parse_version("1.2.0-rc1"), None)
    check("garbage is ignored", updater.parse_version("latest"), None)
    check("empty is ignored", updater.parse_version(""), None)

    check("a newer patch is newer", updater.is_newer("v1.1.10", "v1.1.9"), True)
    check("a newer minor is newer", updater.is_newer("v1.2.0", "v1.1.9"), True)
    check("an equal version is not newer",
          updater.is_newer("v1.1.2", "v1.1.2"), False)
    # 1.1 and 1.1.0 are two spellings of one version, not a free upgrade.
    check("zero-padding makes 1.1 equal 1.1.0",
          updater.is_newer("v1.1", "v1.1.0"), False)
    check("...in the other direction too",
          updater.is_newer("v1.1.0", "v1.1"), False)
    check("an older version is not newer",
          updater.is_newer("v1.1.1", "v1.1.2"), False)
    check("an unparsable candidate is never an update",
          updater.is_newer("1.2.0-rc1", "v1.1.2"), False)
    check("...and neither is an unparsable current",
          updater.is_newer("v2.0.0", "oops"), False)


# ── 2. Checksum verification ───────────────────────────────────────────────

def test_sha256() -> None:
    print("\nsha256 verification")
    body = b"these would be installer bytes"
    path = Path(tempfile.mkdtemp(prefix="lsm-hash-")) / "setup.exe"
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()

    check("the file's own hash verifies", updater.verify_sha256(path, digest),
          True)
    check("upper-case verifies too",
          updater.verify_sha256(path, digest.upper()), True)
    check("a different hash does not",
          updater.verify_sha256(path, "0" * 64), False)
    check("a truncated hash does not",
          updater.verify_sha256(path, digest[:32]), False)


# ── 3. The download and its staging discipline ─────────────────────────────

RELEASE = {
    "tag": "v1.2.0",
    "name": "v1.2.0",
    "notes_url": "https://github.com/camster91/rotman-lsm-calendar/releases/tag/v1.2.0",
    "assets": [
        {"id": 101, "name": "sha256.txt", "size": 90},
        {"id": 102, "name": "RotmanLSMCalendar-Setup-1.2.0.exe", "size": 4},
        {"id": 103, "name": "release_notes.md", "size": 10},
    ],
}


class FakeFetch:
    """Stands in for updater._fetch: records the call, serves canned bytes.

    Serves the body in fixed chunks through on_data so download_setup's
    progress callback can be observed doing its one job.
    """

    def __init__(self, body: bytes = b"", error: Exception | None = None):
        self.calls: list[dict] = []
        self.body = body
        self.error = error

    def __call__(self, url, token=None, accept="application/json",
                 timeout=20.0, on_data=None):
        self.calls.append({"url": url, "token": token, "accept": accept})
        if self.error is not None:
            raise self.error
        if on_data is None:
            return self.body
        view = memoryview(self.body)
        for i in range(0, len(self.body), 2):
            on_data(view[i:i + 2].tobytes(), len(self.body))
        return self.body


def test_download() -> None:
    print("\ndownload and staging")

    # The setup asset is found by prefix among unrelated release assets, and
    # the bytes come from the API asset endpoint with the octet-stream Accept
    # that makes GitHub serve the file rather than its JSON description.
    ff = FakeFetch(body=b"setup")
    with patched(updater, _fetch=ff):
        path = updater.download_setup(RELEASE, "tok",
                                      lambda done, total: None)
    check("the staged file carries the asset's name", path.name,
          "RotmanLSMCalendar-Setup-1.2.0.exe")
    check("the download went through the asset endpoint",
          ff.calls[0]["url"],
          f"{updater.GITHUB_API}/repos/camster91/rotman-lsm-calendar"
          "/releases/assets/102")
    check("with the octet-stream Accept", ff.calls[0]["accept"],
          "application/octet-stream")
    check("...and nothing else was fetched", len(ff.calls), 1)

    # Progress reports move strictly forward: the callback is the only way
    # the sidebar learns how far along a multi-megabyte download is.
    seen: list[int] = []
    with patched(updater, _fetch=FakeFetch(body=b"0123456789")):
        updater.download_setup(RELEASE, None,
                               lambda done, total: seen.append(done))
    ok("progress moves forward", seen == [2, 4, 6, 8, 10])
    ok("...and lands at the full size", (seen[-1] if seen else 0) == 10)

    # An interrupted download leaves no file under the real name. A
    # half-installer that *looks* like the thing the next click would run
    # is the one outcome worse than no file at all.
    updater.purge_staging()
    boom = FakeFetch(error=updater.UpdateError("Could not reach GitHub (down)."))
    with patched(updater, _fetch=boom):
        try:
            updater.download_setup(RELEASE, None, lambda d, t: None)
            ok("an interrupted download raises", False)
        except updater.UpdateError:
            ok("an interrupted download raises", True)
    final = updater.STAGE_DIR / "RotmanLSMCalendar-Setup-1.2.0.exe"
    ok("the interrupted download left no final file", not final.exists())
    ok("...its partial bytes are still there for the purge",
       final.with_suffix(".exe.part").exists())
    updater.purge_staging()
    ok("purge empties the staging directory", not updater.STAGE_DIR.exists())
    updater.purge_staging()  # and is safe on a directory that is already gone

    # A release whose setup asset is not an installer is refused before a
    # byte is fetched.
    zip_release = {**RELEASE, "assets": [
        {"id": 201, "name": "RotmanLSMCalendar-Setup-1.2.0.zip", "size": 4}]}
    with patched(updater, _fetch=FakeFetch()):
        try:
            updater.download_setup(zip_release, None, lambda d, t: None)
            ok("a non-exe setup asset is refused", False)
        except updater.UpdateError as exc:
            ok("a non-exe setup asset is refused", "exe" in str(exc))

    # And a release without the setup asset at all says which file is
    # missing, rather than failing later with nothing to compare against.
    empty_release = {**RELEASE, "assets": [{"id": 301, "name": "sha256.txt",
                                             "size": 90}]}
    with patched(updater, _fetch=FakeFetch()):
        try:
            updater.download_setup(empty_release, None, lambda d, t: None)
            ok("a missing setup asset is named", False)
        except updater.UpdateError as exc:
            ok("a missing setup asset is named",
               "RotmanLSMCalendar-Setup-" in str(exc))


def test_sha256_asset() -> None:
    print("\nthe release's own checksum sidecar")
    ff = FakeFetch(body=("ab12cd34" * 8).encode()
                   + b"  RotmanLSMCalendar-Setup-1.2.0.exe\n")
    with patched(updater, _fetch=ff):
        check("the hash is read from the sidecar's first line",
              updater.fetch_sha256(RELEASE, "tok"), "ab12cd34" * 8)
    check("the sidecar asset was found by name", ff.calls[0]["url"],
          f"{updater.GITHUB_API}/repos/camster91/rotman-lsm-calendar"
          "/releases/assets/101")

    for label, body in (
        ("no hash at all", b""),
        ("not a hash", b"hello world\n"),
        ("a short hash", b"ab12cd\n"),
    ):
        with patched(updater, _fetch=FakeFetch(body=body)):
            try:
                updater.fetch_sha256(RELEASE, None)
                ok(f"{label} is refused", False)
            except updater.UpdateError:
                ok(f"{label} is refused", True)


# ── 4. _fetch itself: headers and error sentences ──────────────────────────

class FakeResponse:
    """The slice of a urlopen result that _fetch reads."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self._pos = 0
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            out, self._pos = self._body[self._pos:], len(self._body)
            return out
        out = self._body[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_fetch_headers() -> None:
    """The real _fetch, against a fake urlopen.

    The Bearer header is the credential's only trip out of this machine, so
    its presence and absence are both asserted — with a token set it travels,
    and without one nothing that could be mistaken for it is sent.
    """
    print("\n_fetch -- headers and errors")

    requests: list[urllib.request.Request] = []

    def fake_urlopen(req, timeout=None):
        requests.append(req)
        return FakeResponse(b"{}")

    with patched(urllib.request, urlopen=fake_urlopen):
        updater._fetch("https://api.github.com/x", token="ghp_secret")
        updater._fetch("https://api.github.com/y")

    check("with a token, the Bearer header is sent",
          requests[0].get_header("Authorization"), "Bearer ghp_secret")
    check("without a token, no Authorization header is sent",
          requests[1].get_header("Authorization"), None)
    check("the Accept travels", requests[0].get_header("Accept"),
          "application/json")
    check("the app names itself",
          requests[0].get_header("User-agent"),
          f"RotmanLSMCalendar/{APP_VERSION}")

    # The chunked path: on_data receives each chunk with the running total
    # when the server does not name a size.
    def streaming_urlopen(req, timeout=None):
        return FakeResponse(b"0123456789")

    got: list[tuple[int, int | None]] = []
    with patched(urllib.request, urlopen=streaming_urlopen):
        body = updater._fetch("https://api.github.com/z", on_data=lambda chunk,
                              total: got.append((len(chunk), total)))
    check("a streamed body arrives whole", body, b"0123456789")
    check("...with the running total when no size was named",
          got, [(10, 10)])

    # And the errors are sentences, not codes: '404' reads as a bug where
    # 'the repo is private and no token is set' reads as an instruction.
    def raise_status(code):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "err", None, None)
        return fake

    with patched(urllib.request, urlopen=raise_status(404)):
        try:
            updater._fetch("https://api.github.com/x")
            ok("a 404 raises", False)
        except updater.UpdateError as exc:
            ok("a 404 says the repo is private with no token",
               "private" in str(exc) and "token" in str(exc))

    with patched(urllib.request, urlopen=raise_status(403)):
        try:
            updater._fetch("https://api.github.com/x", token="bad")
            ok("a 403 raises", False)
        except updater.UpdateError as exc:
            ok("a 403 says the token was rejected", "rejected" in str(exc))

    def unreachable(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patched(urllib.request, urlopen=unreachable):
        try:
            updater._fetch("https://api.github.com/x")
            ok("an unreachable host raises", False)
        except updater.UpdateError as exc:
            ok("an unreachable host says so", "Could not reach GitHub" in str(exc))


def test_fetch_latest() -> None:
    print("\nfetch_latest")

    good = (b'{"tag_name": "v1.2.0", "name": "v1.2.0", '
            b'"html_url": "https://example/n", '
            b'"assets": [{"id": 7, "name": "sha256.txt", "size": 90}]}')
    ff = FakeFetch(body=good)
    with patched(updater, _fetch=ff):
        rel = updater.fetch_latest("tok")
    check("the release's tag", rel["tag"], "v1.2.0")
    check("...its notes url", rel["notes_url"], "https://example/n")
    check("...and its assets", rel["assets"],
          [{"id": 7, "name": "sha256.txt", "size": 90}])

    for label, body in (
        ("invalid JSON", b"<html>welcome to github</html>"),
        ("no release in the answer", b'{"message": "Not Found"}'),
    ):
        with patched(updater, _fetch=FakeFetch(body=body)):
            try:
                updater.fetch_latest(None)
                ok(f"{label} raises a sentence", False)
            except updater.UpdateError:
                ok(f"{label} raises a sentence", True)


# ── 5. The GitHub token ────────────────────────────────────────────────────

def test_token() -> None:
    print("\nthe github token")

    updater.clear_token()
    check("no token stored reads as empty", updater.load_token(), "")

    updater.save_token("ghp_readonly_123")
    ok("saving writes a file", updater.TOKEN_FILE.exists())
    ok("...which is not the token in cleartext",
       b"ghp_readonly_123" not in updater.TOKEN_FILE.read_bytes())
    check("and it round-trips", updater.load_token(), "ghp_readonly_123")

    for label, bad in (
        ("an empty token", ""),
        ("a token with a space", "ghp one"),
        ("a token with a newline", "ghp\ntwo"),
        ("a 201-character token", "x" * 201),
    ):
        try:
            updater.save_token(bad)
            ok(f"{label} is refused", False)
        except ValueError:
            ok(f"{label} is refused", True)
    check("...and a refused token writes nothing",
          updater.load_token(), "ghp_readonly_123")

    # A blob that does not decrypt is no token, not a crash — same rule as
    # the session snapshot: it belongs to another user or machine.
    updater.TOKEN_FILE.write_bytes(b"not a dpapi blob at all")
    check("an undecryptable blob reads as empty", updater.load_token(), "")

    updater.clear_token()
    ok("clearing removes the file", not updater.TOKEN_FILE.exists())
    ok("...and is safe when there is nothing to remove",
       updater.clear_token() is None)

    # The no-cleartext sentinel: without DPAPI there is no cipher, and the
    # one thing save_token must never do is fall back to writing the token
    # unencrypted. It must refuse, and leave nothing behind.
    with patched(dpapi, _IS_WINDOWS=False):
        try:
            updater.save_token("ghp_secret")
            ok("without DPAPI the save refuses", False)
        except OSError:
            ok("without DPAPI the save refuses", True)
    ok("...and wrote nothing", not updater.TOKEN_FILE.exists())


# ── 6. The 24-hour cadence and the four check outcomes ─────────────────────

def test_cadence() -> None:
    print("\nthe 24-hour cadence")
    store.init_db()
    orch = scheduler.Orchestrator()

    check("an unknown last check is due", orch._due_for_update_check(
        datetime(2026, 9, 24, 12, 0, 0)), True)

    now = datetime(2026, 9, 24, 12, 0, 0)
    store.save_update_state({"last_check": now.isoformat(), "skipped": None})
    check("23 hours later is not due", orch._due_for_update_check(
        now + timedelta(hours=23)), False)
    check("24 hours later is due", orch._due_for_update_check(
        now + timedelta(hours=24)), True)
    check("25 hours later is due", orch._due_for_update_check(
        now + timedelta(hours=25)), True)

    # A corrupt stamp reads as due: the cost is one check, not a stuck clock.
    store.save_update_state({"last_check": "yesterday-ish", "skipped": None})
    check("a corrupt stamp is due", orch._due_for_update_check(now), True)

    # The clock is the machine's, not the process's: a fresh orchestrator
    # over the same data dir must not think a day has passed.
    store.save_update_state({"last_check": now.isoformat(), "skipped": None})
    restarted = scheduler.Orchestrator()
    check("a restart does not reset the clock", restarted._due_for_update_check(
        now + timedelta(hours=1)), False)


def test_check_outcomes() -> None:
    print("\nthe check's four outcomes")
    store.init_db()

    def release(tag):
        return {"tag": tag, "name": tag, "notes_url": "https://example/n",
                "assets": []}

    notes: list[tuple] = []

    def run_check(tag, skipped=None):
        """One _do_check_update with the network and the token faked."""
        orch = scheduler.Orchestrator()
        notes.clear()
        orch.set_update_hooks(notify=lambda t, m: notes.append((t, m)))
        store.save_update_state({"last_check": None, "skipped": skipped})
        with patched(updater, load_token=lambda: "tok",
                     fetch_latest=lambda token: release(tag)):
            orch._do_check_update(trigger="manual")
        st = orch.status()["update"]
        return orch, st

    # Newer: available, with the toast that says where to act.
    orch, st = run_check("v99.0.0")
    check("a newer release is available", st["state"], "available")
    check("...and its version is recorded", st["latest_version"], "v99.0.0")
    check("...with a toast", notes,
          [("Update available",
            "v99.0.0 — see the calendar sidebar to install it")])
    ok("the check stamped the clock", bool(st["last_check"]))
    check("...so it is not immediately due again",
          orch._due_for_update_check(datetime.now()), False)
    ok("...and it left the worker not busy", not orch.status()["busy"])

    # Equal: latest, quietly.
    orch, st = run_check(APP_VERSION)
    check("the current version is 'latest'", st["state"], "latest")
    check("...with no toast", notes, [])

    # Skipped and nothing newer: still hidden, with the sentence that says so.
    orch, st = run_check("v99.0.0", skipped="v99.0.0")
    check("a skipped release stays hidden", st["state"], "latest")
    ok("...and the message says it was skipped",
       "Skipped v99.0.0" in st["message"])
    check("...with no toast", notes, [])

    # Something newer than the skipped one: offered again.
    orch, st = run_check("v99.0.0", skipped="v98.0.0")
    check("a release newer than the skipped one is offered",
          st["state"], "available")
    check("...with its toast", len(notes), 1)

    # A failure is a sentence in the sidebar, never a toast — a toast says
    # "something happened" and vanishes, leaving no instruction behind.
    orch = scheduler.Orchestrator()
    notes.clear()
    orch.set_update_hooks(notify=lambda t, m: notes.append((t, m)))
    boom = updater.UpdateError("Could not reach GitHub (offline).")
    with patched(updater, load_token=lambda: "",
                 fetch_latest=lambda token: (_ for _ in ()).throw(boom)):
        orch._do_check_update(trigger="auto")
    st = orch.status()["update"]
    check("an unreachable check is 'failed'", st["state"], "failed")
    check("...carrying the sentence", st["message"],
          "Could not reach GitHub (offline).")
    check("...and no toast", notes, [])
    ok("...and the worker is not left busy", not orch.status()["busy"])

    # The stamp is written before the check, so an unreachable GitHub waits
    # a day rather than turning the 20 s tick into a retry storm.
    check("a failed check still stamped the clock",
          orch._due_for_update_check(datetime.now()), False)

    # And the sidebar learns whether a token exists at all — that is the
    # fact its "GitHub token" row exists to fix.
    orch = scheduler.Orchestrator()
    with patched(updater, load_token=lambda: "",
                 fetch_latest=lambda token: release(APP_VERSION)):
        orch._do_check_update(trigger="auto")
    check("the status reports no token set",
          orch.status()["update"]["token_set"], False)


# ── 7. The install path ────────────────────────────────────────────────────

INSTALLER_BYTES = b"the bytes of a fake installer"


def test_install() -> None:
    print("\nthe install path")
    store.init_db()

    def good_release():
        return {"tag": "v99.0.0", "name": "v99.0.0",
                "notes_url": "https://example/n", "assets": []}

    def fake_download(release, token, on_progress):
        updater.STAGE_DIR.mkdir(parents=True, exist_ok=True)
        path = updater.STAGE_DIR / "RotmanLSMCalendar-Setup-99.0.0.exe"
        path.write_bytes(INSTALLER_BYTES)
        on_progress(len(INSTALLER_BYTES), len(INSTALLER_BYTES))
        return path

    launched: list[Path] = []
    quits: list[bool] = []

    def run_install(download=fake_download,
                    sha=lambda release, token: hashlib.sha256(
                        INSTALLER_BYTES).hexdigest(),
                    tag="v99.0.0", staged=None, state=None):
        orch = scheduler.Orchestrator()
        launched.clear()
        quits.clear()
        orch.set_update_hooks(quit=lambda: quits.append(True))
        if staged is not None:
            orch._set_update(state=state or "ready", staged=str(staged))
        with patched(updater, load_token=lambda: "tok",
                     fetch_latest=lambda token: good_release() | {"tag": tag},
                     download_setup=download, fetch_sha256=sha,
                     launch_installer=lambda p: launched.append(p)):
            orch._do_install_update()
        return orch, orch.status()["update"]

    # The full ride: download, verify against the release's own hash, run.
    orch, st = run_install()
    check("the download reported progress", st["progress"], "")
    ok("a verified installer was launched", len(launched) == 1)
    check("...the staged file itself", launched[0].name,
          "RotmanLSMCalendar-Setup-99.0.0.exe")
    check("...and the state says installing", st["state"], "installing")
    ok("...with the quit hook fired", quits == [True])
    ok("...and the worker not left busy", not orch.status()["busy"])

    # The hash is the load-bearing step: a download that does not match what
    # the release published is never run, whatever went wrong with it.
    orch, st = run_install(sha=lambda release, token: "0" * 64)
    check("a mismatched checksum is 'failed'", st["state"], "failed")
    ok("...with the sentence that says it will not be run",
       "checksum" in st["message"])
    ok("...and the installer was not launched", launched == [])
    ok("...and the process was not quit", quits == [])

    # The ready shortcut: a staged, verified installer from an earlier click
    # is launched without re-downloading anything.
    existing = Path(tempfile.mkdtemp(prefix="lsm-staged-")) / "setup.exe"
    existing.write_bytes(INSTALLER_BYTES)

    def must_not_download(release, token, on_progress):
        raise AssertionError("a ready install must not download again")

    def must_not_fetch(token):
        raise AssertionError("a ready install must not re-check GitHub")

    orch = scheduler.Orchestrator()
    launched.clear()
    quits.clear()
    orch.set_update_hooks(quit=lambda: quits.append(True))
    orch._set_update(state="ready", staged=str(existing))
    with patched(updater, load_token=lambda: "tok",
                 fetch_latest=must_not_fetch, download_setup=must_not_download,
                 fetch_sha256=lambda r, t: "",
                 launch_installer=lambda p: launched.append(p)):
        orch._do_install_update()
    ok("a ready install launches straight away", len(launched) == 1)
    check("...the previously staged file", launched[0], existing)
    ok("...and still quits the app", quits == [True])

    # Nothing to install: the release is not newer, so nothing is fetched
    # beyond the listing, nothing runs, and the sidebar says why.
    orch, st = run_install(tag=APP_VERSION)
    check("an up-to-date install is 'latest'", st["state"], "latest")
    ok("...with the honest message", "up to date" in st["message"])
    ok("...and nothing was launched", launched == [])
    ok("...and no quit", quits == [])

    # A launch that fails is reported, not mistaken for success.
    def exploding_launch(path):
        raise OSError("it did not start")

    def run_with_dead_launcher():
        orch = scheduler.Orchestrator()
        quits.clear()
        orch.set_update_hooks(quit=lambda: quits.append(True))
        with patched(updater, load_token=lambda: "tok",
                     fetch_latest=lambda token: good_release(),
                     download_setup=fake_download,
                     fetch_sha256=lambda r, t: hashlib.sha256(
                         INSTALLER_BYTES).hexdigest(),
                     launch_installer=exploding_launch):
            orch._do_install_update()
        return orch.status()["update"]

    st = run_with_dead_launcher()
    check("a failed launch is 'failed'", st["state"], "failed")
    check("...and says the installer could not start",
          st["message"], "The installer could not be started.")
    ok("...with no quit", quits == [])


# ── 8. The endpoints ────────────────────────────────────────────────────────

class UpdateOrch:
    """The slice of the orchestrator the update endpoints touch."""

    def __init__(self, busy: bool = False) -> None:
        self._busy = busy
        self.calls: list[str] = []
        self.update = {"state": "idle", "current_version": APP_VERSION,
                       "latest_version": "", "notes_url": "", "message": "",
                       "last_check": None, "progress": "",
                       "token_set": False, "staged": ""}

    def is_busy(self) -> bool:
        return self._busy

    def status(self) -> dict:
        return {"busy": self._busy, "update": self.update}

    def request_update_check(self, trigger: str = "manual") -> None:
        self.calls.append("check")

    def request_update_install(self) -> None:
        self.calls.append("install")


def test_endpoints() -> None:
    print("\nthe update endpoints")
    store.init_db()
    updater.clear_token()

    orch = UpdateOrch()
    client = create_app(orch).test_client()

    r = client.post("/api/update/check")
    check("a check request is accepted", r.status_code, 200)
    check("...and handed to the worker", orch.calls, ["check"])
    check("...and the reply says started, not done",
          r.get_json().get("status"), "started")

    orch.calls.clear()
    orch._busy = True
    check("a busy worker refuses the check",
          client.post("/api/update/check").status_code, 409)
    check("...and refuses the install",
          client.post("/api/update/install").status_code, 409)
    check("...without queueing either", orch.calls, [])
    orch._busy = False

    # Skip takes its version from the orchestrator, not the request: the
    # server is the one party that knows what was actually offered, and a
    # client dictating what to skip could hide a release it was never shown.
    r = client.post("/api/update/skip")
    check("skip is refused when nothing is available", r.status_code, 400)
    orch.update["state"] = "available"
    orch.update["latest_version"] = "v1.1.9"
    r = client.post("/api/update/skip", json={"version": "v0.0.1"})
    check("an available release can be skipped", r.status_code, 200)
    check("...recording what was offered, not what was posted",
          store.load_update_state().get("skipped"), "v1.1.9")

    # The token write: stored encrypted, replied about only as a boolean,
    # and refused with a sentence when it does not look like a token.
    r = client.post("/api/update/token", json={"token": "ghp_secret"})
    check("a token is accepted", r.status_code, 200)
    check("...and the reply never says what it is",
          r.get_json(), {"status": "ok", "token_set": True})
    ok("...and the file is not the cleartext token",
       b"ghp_secret" not in updater.TOKEN_FILE.read_bytes())
    check("...and it loads back", updater.load_token(), "ghp_secret")

    raw = client.get("/api/status").get_data(as_text=True)
    ok("the status feed carries the update block", '"update"' in raw)
    ok("...and never the token itself", "ghp_secret" not in raw)

    for label, body in (
        ("no token key", {}),
        ("a non-string token", {"token": 123}),
        ("an empty token", {"token": ""}),
        ("a token with whitespace", {"token": "ghp one"}),
    ):
        r = client.post("/api/update/token", json=body)
        check(f"{label} is refused with a sentence", r.status_code, 400)
        ok(f"...({label}) and the refusal explains itself",
           bool(r.get_json().get("message")))

    r = client.delete("/api/update/token")
    check("clearing the token is accepted", r.status_code, 200)
    check("...and reports no token set", r.get_json().get("token_set"), False)
    ok("...and the file is gone", not updater.TOKEN_FILE.exists())


def main() -> int:
    print("=" * 60)
    print("  Rotman LSM Calendar — updater tests")
    print("=" * 60)

    test_versions()
    test_sha256()
    test_download()
    test_sha256_asset()
    test_fetch_headers()
    test_fetch_latest()
    test_token()
    test_cadence()
    test_check_outcomes()
    test_install()
    test_endpoints()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())