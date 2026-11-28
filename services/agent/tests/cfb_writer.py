"""A writer of Compound File Binary files ([MS-CFB] version 3) for synthetic test inputs: .doc, .xls, .ppt and .hwp
containers, and the encrypted OOXML wrapper.

It writes 512-byte sectors: the FAT sectors (listed in the header's DIFAT, so at most 109 of them), then the
directory, then each stream in sectors of its own. Every stream is written to the regular FAT, so each is padded to
at least the 4096-byte mini stream cutoff and no mini stream is needed. Each storage's children form a red-black tree
in the spec's order (shorter names first, then by uppercase). trailing appends bytes no sector holds. test_cfb_writer.py
reads what it writes back with olefile.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SECTOR = 512
MINI_CUTOFF = 4096
FREESECT, ENDOFCHAIN, FATSECT, NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF
STORAGE, STREAM, ROOT = 1, 2, 5
RED, BLACK = 0, 1
DIFAT_IN_HEADER = 109

type Tree = dict[str, "bytes | Tree"]


@dataclass
class _Entry:
    name: str
    kind: int
    data: bytes = b""
    children: list[int] = field(default_factory=list)
    left: int = NOSTREAM
    right: int = NOSTREAM
    child: int = NOSTREAM
    color: int = BLACK
    start: int = ENDOFCHAIN


def _key(name: str) -> tuple[int, str]:
    return len(name), name.upper()


def _flatten(tree: Tree, entries: list[_Entry], parent: _Entry) -> None:
    for name, value in tree.items():
        if len(name) > 31:
            raise ValueError(f"a CFB name holds at most 31 characters, not {name!r}")
        entry = _Entry(name, STORAGE if isinstance(value, dict) else STREAM, b"" if isinstance(value, dict) else bytes(value))
        entries.append(entry)
        parent.children.append(len(entries) - 1)
        if isinstance(value, dict):
            _flatten(value, entries, entry)


def _balance(entries: list[_Entry], ids: list[int], depth: int, deepest: int, full: bool) -> int:
    """The root of a balanced tree of ids (sorted); nodes on an incomplete last level are red."""
    if not ids:
        return NOSTREAM
    middle = len(ids) // 2
    node = entries[ids[middle]]
    node.color = RED if depth == deepest and not full else BLACK
    node.left = _balance(entries, ids[:middle], depth + 1, deepest, full)
    node.right = _balance(entries, ids[middle + 1:], depth + 1, deepest, full)
    return ids[middle]


def _tree(entries: list[_Entry], parent: _Entry) -> None:
    ids = sorted(parent.children, key=lambda index: _key(entries[index].name))
    deepest = max(len(ids).bit_length() - 1, 0)
    parent.child = _balance(entries, ids, 0, deepest, len(ids) == (1 << (deepest + 1)) - 1)
    for index in ids:
        if entries[index].kind == STORAGE:
            _tree(entries, entries[index])


def _sectors(size: int) -> int:
    return -(-size // SECTOR)


ENTRY = struct.Struct("<64sHBBIII16sIQQIQ")
FREE_ENTRY = ENTRY.pack(bytes(64), 0, 0, 0, NOSTREAM, NOSTREAM, NOSTREAM, bytes(16), 0, 0, 0, 0, 0)


def _directory_entry(entry: _Entry) -> bytes:
    name = (entry.name.encode("utf-16-le") + b"\0\0").ljust(64, b"\0")
    name_length = len(entry.name.encode("utf-16-le")) + 2
    size = len(entry.data) if entry.kind == STREAM else 0
    return ENTRY.pack(
        name, name_length, entry.kind, entry.color, entry.left, entry.right, entry.child,
        bytes(16), 0, 0, 0, entry.start if entry.kind == STREAM else (ENDOFCHAIN if entry.kind == ROOT else 0), size,
    )


def write(path: Path, tree: Tree, *, trailing: int = 0) -> Path:
    """Write the compound file of tree, {name: stream bytes | {child tree}} under the root, to path."""
    root = _Entry("Root Entry", ROOT)
    entries = [root]
    _flatten(tree, entries, root)
    _tree(entries, root)
    for entry in entries:
        if entry.kind == STREAM and len(entry.data) < MINI_CUTOFF:
            entry.data = entry.data.ljust(MINI_CUTOFF, b"\0")
    directory_sectors = _sectors(len(entries) * 128)
    data_sectors = sum(_sectors(len(entry.data)) for entry in entries if entry.kind == STREAM)
    fat_sectors = 1
    while fat_sectors * (SECTOR // 4) < fat_sectors + directory_sectors + data_sectors:
        fat_sectors += 1
    if fat_sectors > DIFAT_IN_HEADER:
        raise ValueError("this writer lists every FAT sector in the header, at most 109")
    fat = [FATSECT] * fat_sectors
    first_directory = len(fat)
    fat += [*range(first_directory + 1, first_directory + directory_sectors), ENDOFCHAIN]
    body = bytearray()
    for entry in entries:
        if entry.kind != STREAM:
            continue
        count = _sectors(len(entry.data))
        entry.start = len(fat)
        fat += [*range(entry.start + 1, entry.start + count), ENDOFCHAIN]
        body += entry.data.ljust(count * SECTOR, b"\0")
    fat_bytes = struct.pack(f"<{len(fat)}I", *fat).ljust(fat_sectors * SECTOR, b"\xff")
    free = directory_sectors * SECTOR // 128 - len(entries)
    directory = b"".join(_directory_entry(entry) for entry in entries) + FREE_ENTRY * free
    difat = [*range(fat_sectors), *[FREESECT] * (DIFAT_IN_HEADER - fat_sectors)]
    header = struct.pack(
        "<8s16sHHHHH6sIIIIIIIII", MAGIC, bytes(16), 0x003E, 0x0003, 0xFFFE, 9, 6, bytes(6), 0, fat_sectors,
        first_directory, 0, MINI_CUTOFF, ENDOFCHAIN, 0, ENDOFCHAIN, 0,
    ) + struct.pack(f"<{DIFAT_IN_HEADER}I", *difat)
    with path.open("wb") as handle:
        handle.write(header + fat_bytes + directory + bytes(body))
        if trailing:
            handle.truncate(handle.tell() + trailing)
    return path
