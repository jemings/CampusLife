"""identify (S9): the media type family a Blob's bytes show, read in the child.

Magic bytes at the start first, then a PDF header anywhere in the first 1024 bytes, as PDF readers take it, when the
bytes are no text: a note or code that mentions %PDF- is text. A ZIP is
opened through safe_zip for its member names to tell OOXML and HWPX from a plain archive, so an archive whose names
are dangerous ends identify with unsafe_input, while one too large to read is left to its extractor. An OLE compound
file is opened through cfb for the entries of its root storage (CFB_ROOTS), so a word in a stream's bytes says
nothing; a damaged one ends identify with corrupt. Bytes with no signature are text when a sample of them decodes,
and application/octet-stream otherwise. UTF-16 is known only by its BOM: without one it is not guessed and reads as
application/octet-stream. The version goes into the identify pipeline: a change to what this reports is a new
version (2: the root entries of a compound file, and the PDF header within 1024 bytes).
"""

from __future__ import annotations

import codecs
from pathlib import Path

from . import formats
from .cfb import open_cfb
from .limits import Limits
from .protocol import Refused, Status
from .safe_zip import open_zip

VERSION = "2"

HEAD_BYTES = 4096
TEXT_SAMPLE_BYTES = 64 * 1024
PDF_HEADER = b"%PDF-"
PDF_HEADER_WITHIN = 1024
MIMETYPE_MAX_BYTES = 256
"""An HWPX mimetype member says application/hwp+zip; a larger one is not read."""
BOMS = (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
"""A text BOM comes before every signature: FF FE would otherwise read as an MPEG audio frame."""
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENCRYPTED_ROOTS = frozenset({"EncryptedPackage", "\x06DataSpaces"})
"""An encrypted OOXML file ([MS-OFFCRYPTO]) is a compound file around the encrypted package. Checked before CFB_ROOTS:
an encrypted package says what the rest of the file is not readable."""
CFB_ROOTS = (
    ("WordDocument", "application/msword"),
    ("Workbook", "application/vnd.ms-excel"),
    ("Book", "application/vnd.ms-excel"),
    ("PowerPoint Document", "application/vnd.ms-powerpoint"),
    ("FileHeader", "application/x-hwp"),
)
"""The root entry each format's compound file has, in the order they are looked for; a file with none is a plain
compound file (formats.CFB)."""
FTYP_BRANDS = {b"M4A ": formats.M4A, b"M4B ": formats.M4A, b"qt  ": formats.MOV}
HEIF_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"})
OOXML_PARTS = (("word/document.xml", formats.DOCX), ("ppt/presentation.xml", formats.PPTX), ("xl/workbook.xml", formats.XLSX))


def _zip_family(path: Path, limits: Limits) -> str:
    with open_zip(path, limits, names_only=True) as archive:
        sizes = {member.name: member.size for member in archive.members}
        if sizes.get("mimetype", MIMETYPE_MAX_BYTES + 1) <= MIMETYPE_MAX_BYTES and archive.read("mimetype").strip() == formats.HWPX.encode():
            return formats.HWPX
        names = set(sizes)
        if "[Content_Types].xml" in names:
            for part, media_type in OOXML_PARTS:
                if part in names:
                    return media_type
        return formats.ZIP


def _cfb_family(path: Path) -> str:
    with open_cfb(path) as compound:
        roots = compound.root_entries()
    if roots & ENCRYPTED_ROOTS:
        raise Refused(Status.PASSWORD_REQUIRED, "an encrypted Office document (a compound file around an EncryptedPackage)")
    return next((media_type for entry, media_type in CFB_ROOTS if entry in roots), formats.CFB)


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
    if head.startswith(PDF_HEADER):
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
    if _is_text(sample, whole):
        return formats.TEXT
    return formats.PDF if PDF_HEADER in sample[:PDF_HEADER_WITHIN] else formats.UNKNOWN
