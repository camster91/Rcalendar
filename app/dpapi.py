"""
Windows DPAPI wrapper — encrypts data at rest under the current user account.

Used to hold the LSM session-cookie snapshot. A Shibboleth session cookie
*is* a credential: anything that has it can read the LSM portal without
MFA. Storing it with DPAPI means the bytes on disk are only decryptable by
this Windows account on this machine, so a copied file or a synced backup
is useless to anyone else.

crypt32 is called directly through ctypes — no pywin32 dependency, which
keeps the PyInstaller bundle small.

On non-Windows (dev/CI) this degrades to a passthrough and says so.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

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
    """Encrypt for the current Windows user. Passthrough off-Windows."""
    if not _IS_WINDOWS:
        return data

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
    """Decrypt. Raises OSError if the blob belongs to another user/machine."""
    if not _IS_WINDOWS:
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
