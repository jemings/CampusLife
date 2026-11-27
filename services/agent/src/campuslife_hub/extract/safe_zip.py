"""The one way into a ZIP container (S8): OOXML, HWPX and ZIP files are opened here and nowhere else.

Opening checks every member the central directory lists before anything is read: its path stays inside the
archive, it is no link, it is not encrypted, and its declared size and compression ratio keep within the limits
table, as do the member count and their total size. Reading a member stops at its declared size, so a header that
lies about it cannot make a reader inflate more. An archive inside an archive opens one level deeper, up to
zip_max_depth. Only this module imports zipfile (tests/test_extract_boundary.py).
"""

from __future__ import annotations

import io
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .limits import Limits
from .protocol import Refused, Status

ENCRYPTED_FLAG = 0x1


@dataclass(frozen=True)
class Member:
    name: str
    size: int


def _unsafe(reason: str) -> Refused:
    return Refused(Status.UNSAFE_INPUT, reason)


def _path_escapes(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return (
        name.startswith(("/", "\\"))
        or (len(name) > 1 and name[1] == ":")
        or ".." in parts
        or "\x00" in name
    )


def _is_link(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _check(infos: list[zipfile.ZipInfo], limits: Limits) -> None:
    if len(infos) > limits.zip_max_members:
        raise _unsafe(f"{len(infos)} members, more than {limits.zip_max_members}")
    names: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if _path_escapes(name):
            raise _unsafe(f"member {name!r} reaches outside the archive")
        if name in names:
            raise _unsafe(f"member {name!r} is listed twice")
        names.add(name)
        if _is_link(info):
            raise _unsafe(f"member {name!r} is a link")
        if info.flag_bits & ENCRYPTED_FLAG:
            raise Refused(Status.PASSWORD_REQUIRED, f"member {name!r} is encrypted")
        if info.file_size > limits.zip_max_member_bytes:
            raise _unsafe(f"member {name!r} declares {info.file_size} bytes, more than {limits.zip_max_member_bytes}")
        if info.file_size > max(info.compress_size, 1) * limits.zip_max_ratio:
            raise _unsafe(f"member {name!r} inflates more than {limits.zip_max_ratio} times")
        total += info.file_size
    if total > limits.zip_max_total_bytes:
        raise _unsafe(f"the members declare {total} bytes together, more than {limits.zip_max_total_bytes}")


class SafeZip:
    """An archive whose members all passed the checks; read them by name."""

    def __init__(self, archive: zipfile.ZipFile, limits: Limits, depth: int):
        self._archive = archive
        self._limits = limits
        self.depth = depth
        self.members = tuple(Member(info.filename, info.file_size) for info in archive.infolist())

    def read(self, name: str) -> bytes:
        info = self._archive.getinfo(name)
        try:
            with self._archive.open(info) as member:
                data = member.read(info.file_size + 1)
        except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError) as error:
            raise Refused(Status.CORRUPT, f"member {name!r} cannot be read: {error}") from None
        if len(data) > info.file_size:
            raise _unsafe(f"member {name!r} inflates past the size it declares")
        return data

    @contextmanager
    def open_nested(self, name: str) -> Iterator[SafeZip]:
        """The archive held in member name, one level deeper."""
        with open_zip(io.BytesIO(self.read(name)), self._limits, depth=self.depth + 1) as nested:
            yield nested


@contextmanager
def open_zip(source: Path | BinaryIO, limits: Limits, *, depth: int = 1) -> Iterator[SafeZip]:
    """The archive at source, checked; Refused when it is not a ZIP (corrupt), holds an encrypted member
    (password_required) or breaks a check (unsafe_input)."""
    if depth > limits.zip_max_depth:
        raise _unsafe(f"an archive nested {depth} deep, more than {limits.zip_max_depth}")
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, EOFError, ValueError) as error:
        raise Refused(Status.CORRUPT, f"not a readable ZIP archive: {error}") from None
    with archive:
        _check(archive.infolist(), limits)
        yield SafeZip(archive, limits, depth)
