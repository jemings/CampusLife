"""Synthetic dangerous and odd inputs, written when a test runs (S16): nothing binary is committed.

Each function writes one file at path and returns it. A zip bomb is a few kilobytes of compressed zeros that
declare far more; none of them is ever inflated whole.
"""

from __future__ import annotations

import io
import stat
import warnings
import zipfile
from pathlib import Path

CONTENT_TYPES = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'


def _zip(path: Path, members: list[tuple[zipfile.ZipInfo | str, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in members:
            archive.writestr(info, data)
    return path


def plain_zip(path: Path) -> Path:
    return _zip(path, [("notes/week1.txt", "1주차 노트".encode()), ("notes/week2.txt", b"week 2")])


def compressible(path: Path) -> Path:
    """One member that deflates some tens of times, as text does."""
    return _zip(path, [("notes.txt", "자료구조 강의 노트 ".encode() * 200)])


def docx(path: Path) -> Path:
    return _zip(path, [("[Content_Types].xml", CONTENT_TYPES.encode()), ("word/document.xml", b"<w:document/>")])


def pptx(path: Path) -> Path:
    return _zip(path, [("[Content_Types].xml", CONTENT_TYPES.encode()), ("ppt/presentation.xml", b"<p:presentation/>")])


def xlsx(path: Path) -> Path:
    return _zip(path, [("[Content_Types].xml", CONTENT_TYPES.encode()), ("xl/workbook.xml", b"<workbook/>")])


def hwpx(path: Path) -> Path:
    stored = zipfile.ZipInfo("mimetype")
    return _zip(path, [(stored, b"application/hwp+zip"), ("Contents/section0.xml", b"<hs:sec/>")])


def traversal(path: Path) -> Path:
    return _zip(path, [("../../outside.txt", b"escaped")])


def backslash_traversal(path: Path) -> Path:
    """A member that climbs out with Windows separators, which zipfile leaves alone on POSIX."""
    return _zip(path, [("notes\\..\\..\\outside.txt", b"escaped")])


def absolute(path: Path) -> Path:
    return _zip(path, [("/etc/outside.txt", b"escaped")])


def drive_letter(path: Path) -> Path:
    return _zip(path, [("C:/Windows/outside.txt", b"escaped")])


def symlink(path: Path) -> Path:
    link = zipfile.ZipInfo("notes/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    return _zip(path, [(link, b"/etc/passwd")])


def encrypted_member(path: Path) -> Path:
    """A member whose header says it is encrypted; zipfile cannot write encrypted data, so only the flag is set."""
    _zip(path, [("secret.txt", b"secret")])
    data = bytearray(path.read_bytes())
    for signature, flag_at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = data.find(signature)
        data[at + flag_at] |= 0x1
    path.write_bytes(bytes(data))
    return path


def bomb(path: Path, *, size: int = 64 << 20) -> Path:
    """One member of size zero bytes, compressed about a thousand times."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive, archive.open("zeros.bin", "w", force_zip64=True) as member:
        chunk = bytes(1 << 20)
        for _ in range(size >> 20):
            member.write(chunk)
    return path


def many_members(path: Path, count: int) -> Path:
    return _zip(path, [(f"m{index}.txt", b"") for index in range(count)])


def nested(path: Path, depth: int) -> Path:
    """An archive holding an archive, depth levels in all."""
    data = b"innermost"
    name = "inner.txt"
    for _ in range(depth):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, data)
        data, name = buffer.getvalue(), "inner.zip"
    path.write_bytes(data)
    return path


def listed_twice(path: Path) -> Path:
    """A .docx whose main part is listed twice, so a reader could take either."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _zip(path, [("word/document.xml", b"<w:document/>"), ("word/document.xml", b"<w:document>other</w:document>")])


def _patched(data: bytearray, signature: bytes, offset: int, value: bytes) -> None:
    at = data.find(signature)
    data[at + offset:at + offset + len(value)] = value


def understated(path: Path) -> Path:
    """A member whose headers declare 10 of its 1,000 bytes."""
    _zip(path, [("a.txt", b"a" * 1000)])
    data = bytearray(path.read_bytes())
    _patched(data, b"PK\x01\x02", 24, (10).to_bytes(4, "little"))
    _patched(data, b"PK\x03\x04", 22, (10).to_bytes(4, "little"))
    path.write_bytes(bytes(data))
    return path


def nul_in_name(path: Path) -> Path:
    """A member named notes.txt, a NUL, then a path that climbs out."""
    _zip(path, [("notes.txt#../../x", b"notes")])
    path.write_bytes(path.read_bytes().replace(b"notes.txt#", b"notes.txt\x00"))
    return path


def encrypted_ooxml(path: Path) -> Path:
    """The shape of a password-protected .docx: an OLE compound file holding an EncryptedPackage stream."""
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)
    directory = "EncryptedPackage".encode("utf-16-le") + bytes(128 - 32)
    path.write_bytes(header + directory + bytes(4096))
    return path


def ole_document(path: Path) -> Path:
    """An OLE compound file without encryption, as a .doc or .hwp is."""
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504) + "WordDocument".encode("utf-16-le") + bytes(4096))
    return path


def pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
    return path
