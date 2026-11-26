"""The admin token (E18): the secret that opens the admin listener, readable only by the user who runs the Hub.

On Windows the file holds the token sealed with DPAPI for the current user. Elsewhere, where the
Hub runs only under test, it holds the token itself and only its owner may read it (0600); a file
others may read is refused.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import sys
from pathlib import Path

TOKEN_BYTES = 32
TOKEN = re.compile("[A-Za-z0-9_-]{43}")
"""What secrets.token_urlsafe(TOKEN_BYTES) makes."""


class AdminTokenUnusable(Exception):
    pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1
    for _name in ("CryptProtectData", "CryptUnprotectData"):
        _function = getattr(_crypt32, _name)
        _function.restype = wintypes.BOOL
        _function.argtypes = (
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.LocalFree.argtypes = (ctypes.c_void_p,)

    def _through(function: object, data: bytes) -> bytes:
        buffer = ctypes.create_string_buffer(data, len(data))
        given, made = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), _DataBlob()
        if not function(ctypes.byref(given), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(made)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(made.pbData, made.cbData)
        finally:
            _kernel32.LocalFree(made.pbData)

    def seal(data: bytes) -> bytes:
        return _through(_crypt32.CryptProtectData, data)

    def unseal(sealed: bytes) -> bytes:
        try:
            return _through(_crypt32.CryptUnprotectData, sealed)
        except OSError as error:
            raise AdminTokenUnusable(f"the admin token cannot be unsealed by this Windows user: {error}") from None

    def _refuse_shared(path: Path) -> None:
        """DPAPI binds the token to the user; the file's ACL adds nothing to check."""

else:

    def seal(data: bytes) -> bytes:
        return data

    def unseal(sealed: bytes) -> bytes:
        return sealed

    def _refuse_shared(path: Path) -> None:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise AdminTokenUnusable(f"{path} may be read by others (mode {mode:o}); only its owner may read it")


def create(path: Path) -> str:
    """A new token in a new file at path, whole or not at all; FileExistsError when one is there. Its caller holds
    the data root's hub.lock."""
    if path.exists():
        raise FileExistsError(f"{path} already holds an admin token")
    token = secrets.token_urlsafe(TOKEN_BYTES)
    sealed = seal(token.encode("ascii"))
    staged = path.with_name(f"{path.name}.new")
    with contextlib.suppress(FileNotFoundError):
        staged.unlink()
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, sealed)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(staged, path)
    return token


def read(path: Path) -> str:
    """The token in the file at path; FileNotFoundError when there is none."""
    _refuse_shared(path)
    try:
        token = unseal(path.read_bytes()).decode("ascii")
    except UnicodeDecodeError:
        token = None
    if token is None or not TOKEN.fullmatch(token):
        raise AdminTokenUnusable(f"{path} does not hold an admin token; delete it and start the Hub again to make a new one")
    return token


def load_or_create(path: Path) -> str:
    try:
        return read(path)
    except FileNotFoundError:
        try:
            return create(path)
        except FileExistsError:
            return read(path)
