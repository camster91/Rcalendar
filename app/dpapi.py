"""
Windows DPAPI wrapper — encrypts data at rest under the current user account.

Used to hold the LSM session-cookie snapshot. A Shibboleth session cookie
*is* a credential: anything that has it can read the LSM portal without
MFA. Storing it with DPAPI means the bytes on disk are only decryptable by
this Windows account on this machine, so a copied file or a synced backup
is useless to anyone else.

crypt32 is called directly through ctypes — no pywin32 dependency, which
keeps the PyInstaller bundle small.

On non-Windows (dev/CI) there is no cipher to use, and what happens then is
deliberately *not* a passthrough: `protect` refuses, and `unprotect` says
loudly that it is handing back bytes it did not decrypt. See both for why.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from app.config import log

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    class _BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL

    # Extra entropy — binds the ciphertext to this application.
    _ENTROPY = b"RotmanLSMCalendar/v1"


def _blob(data: bytes) -> "_BLOB":
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _entropy_blob() -> "_BLOB":
    return _blob(_ENTROPY)


def protect(data: bytes) -> bytes:
    """Encrypt for the current Windows user. Refuses without DPAPI.

    This used to return `data` unchanged when DPAPI was missing, which put the
    Shibboleth cookie on disk in cleartext — into a file whose entire purpose
    is to be unreadable to anyone but this Windows account — and the caller
    could not tell the two outcomes apart. `_save_cookies` logs "session
    snapshot saved (%d cookies, %d shibboleth)" either way, so a cleartext live
    credential was indistinguishable from an encrypted one in the log, in the
    UI, and in any backup that picked the file up.

    Refusing is the smaller loss by a wide margin. A snapshot is a convenience:
    without it the user signs in again on the next launch. A cleartext live
    credential is a credential — anything holding it reads the LSM portal with
    no MFA — and it outlives the run that wrote it.

    Raising is also what makes the caller's existing `except Exception` path
    the correct one rather than an accident: it logs a warning and writes
    nothing, which is exactly the desired outcome.
    """
    if not _IS_WINDOWS:
        raise OSError(
            "DPAPI is unavailable on this platform, and the session snapshot "
            "holds a live cookie; refusing to write it to disk unencrypted. "
            "The session will not be remembered between runs."
        )

    blob_in = _blob(data)
    blob_entropy = _entropy_blob()
    blob_out = _BLOB()

    if not _crypt32.CryptProtectData(
        ctypes.byref(blob_in), "RotmanLSMCalendar", ctypes.byref(blob_entropy),
        None, None, 0, ctypes.byref(blob_out),
    ):
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def unprotect(data: bytes) -> bytes:
    """Decrypt. Raises OSError if the blob belongs to another user/machine.

    Off-Windows there is nothing to decrypt, so the bytes come back as they
    are — a snapshot left by an older build, or by a run where sys.platform
    did not report Windows. That is not a new leak: the file is already on
    disk in the clear, and refusing to read it would only cost the user their
    session while the credential sat there regardless. What it does need is
    saying, because the actionable fact is that a live cookie is readable in
    cleartext and signing out is what clears it. Silence here would leave the
    one party who can act on it — the user — the only one not told.
    """
    if not _IS_WINDOWS:
        log.warning(
            "DPAPI unavailable: handing back %d bytes read from the session "
            "snapshot without decrypting them, so a live session cookie is "
            "stored in cleartext. Signing out clears it.",
            len(data),
        )
        return data

    blob_in = _blob(data)
    blob_entropy = _entropy_blob()
    blob_out = _BLOB()

    if not _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, ctypes.byref(blob_entropy),
        None, None, 0, ctypes.byref(blob_out),
    ):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def available() -> bool:
    return _IS_WINDOWS
