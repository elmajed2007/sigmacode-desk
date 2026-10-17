"""API keys at rest: Windows DPAPI (bound to the Windows user), plain elsewhere.

Stored as "dpapi:<base64>" on Windows; other platforms (development) keep the value
as given. Decryption failures return "" (the key must be entered again).
"""
from __future__ import annotations

import base64
import os

PREFIX = "dpapi:"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out))  # UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(value: str) -> str:
    if not value or os.name != "nt" or value.startswith(PREFIX):
        return value
    return PREFIX + base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")


def reveal(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value or ""
    if os.name != "nt":
        return ""
    try:
        return _dpapi(base64.b64decode(value[len(PREFIX):]), False).decode("utf-8")
    except (OSError, ValueError):
        return ""
