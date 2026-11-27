"""identify (S9): the media type family a Blob's bytes show, read in the child.

Magic bytes first; a ZIP is opened through safe_zip to tell OOXML and HWPX from a plain archive, so a dangerous
archive ends identify with unsafe_input; an OLE container holding an EncryptedPackage stream is an encrypted
OOXML file and ends it with password_required. Bytes with no signature are text when a sample of them decodes,
and application/octet-stream otherwise. UTF-16 is known only by its BOM: without one it is not guessed and reads as
application/octet-stream. The version goes into the identify pipeline: a change to what this
reports is a new version.
"""

from __future__ import annotations

import codecs
from pathlib import Path

from . import formats
from .limits import Limits
from .protocol import Refused, Status
from .safe_zip import open_zip

VERSION = "1"

HEAD_BYTES = 4096
TEXT_SAMPLE_BYTES = 64 * 1024
BOMS = (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
"""A text BOM comes before every signature: FF FE would otherwise read as an MPEG audio frame."""
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")
FTYP_BRANDS = {b"M4A ": formats.M4A, b"M4B ": formats.M4A, b"qt  ": formats.MOV}
HEIF_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"})
OOXML_PARTS = (("word/document.xml", formats.DOCX), ("ppt/presentation.xml", formats.PPTX), ("xl/workbook.xml", formats.XLSX))


def _zip_family(path: Path, limits: Limits) -> str:
    with open_zip(path, limits) as archive:
        names = {member.name for member in archive.members}
        if "mimetype" in names and archive.read("mimetype").strip() == formats.HWPX.encode():
            return formats.HWPX
        if "[Content_Types].xml" in names:
            for part, media_type in OOXML_PARTS:
                if part in names:
                    return media_type
        return formats.ZIP


def _cfb_family(path: Path) -> str:
    if ENCRYPTED_PACKAGE in path.read_bytes():
        raise Refused(Status.PASSWORD_REQUIRED, "an encrypted Office document (an OLE container with an EncryptedPackage)")
    return formats.CFB


def _is_text(sample: bytes, whole: bool) -> bool:
    if sample.startswith(BOMS):
        return True
    if b"\x00" in sample:
        return False
    for encoding in ("utf-8", "cp949"):
        decoder = codecs.getincrementaldecoder(encoding)()
        try:
            decoder.decode(sample, final=whole)
        except UnicodeDecodeError:
            continue
        return True
    return False


def _signature(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return formats.PDF
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return formats.ZIP
    if head.startswith(CFB_MAGIC):
        return formats.CFB
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return formats.PNG
    if head.startswith(b"\xff\xd8\xff"):
        return formats.JPEG
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return formats.WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return formats.WAV
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return formats.HEIC if brand in HEIF_BRANDS else FTYP_BRANDS.get(brand, formats.MP4)
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return formats.MP3
    return None


def identify(path: Path, limits: Limits) -> str:
    with open(path, "rb") as source:
        sample = source.read(TEXT_SAMPLE_BYTES)
        whole = not source.read(1)
    family = None if sample.startswith(BOMS) else _signature(sample[:HEAD_BYTES])
    if family == formats.ZIP:
        return _zip_family(path, limits)
    if family == formats.CFB:
        return _cfb_family(path)
    if family is not None:
        return family
    return formats.TEXT if _is_text(sample, whole) else formats.UNKNOWN
