"""The format table (S9, FILE-INGESTION §3) and refine: which media type an Attachment is, and what reads it.

identify reads only a Blob's bytes and reports a family: a type the bytes prove (application/pdf, an OOXML word
document, a PNG) or, where the bytes cannot tell more, a family such as text/plain or the OLE container. refine
narrows that family for one Attachment with its file name and declared MIME, which the bytes outrank: a name or
MIME of another family is a mismatch, and the bytes win. md, csv and code are told apart here, not by identify.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

PDF = "application/pdf"
ZIP = "application/zip"
CFB = "application/x-cfb"
"""The OLE compound file of .doc, .ppt, .xls and .hwp, which its bytes do not tell apart."""
TEXT = "text/plain"
UNKNOWN = "application/octet-stream"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HWPX = "application/hwp+zip"
PNG = "image/png"
JPEG = "image/jpeg"
WEBP = "image/webp"
HEIC = "image/heic"
M4A = "audio/mp4"
MP3 = "audio/mpeg"
WAV = "audio/wav"
MP4 = "video/mp4"
MOV = "video/quicktime"


@dataclass(frozen=True)
class Format:
    media_type: str
    family: str
    """What identify reports for these bytes."""
    extensions: tuple[str, ...]


def _same(*types: tuple[str, tuple[str, ...]]) -> list[Format]:
    return [Format(media_type, media_type, extensions) for media_type, extensions in types]


def _within(family: str, *types: tuple[str, tuple[str, ...]]) -> list[Format]:
    return [Format(media_type, family, extensions) for media_type, extensions in types]


FORMATS: tuple[Format, ...] = (
    *_same(
        (PDF, (".pdf",)), (DOCX, (".docx",)), (PPTX, (".pptx",)), (XLSX, (".xlsx",)), (HWPX, (".hwpx",)), (ZIP, (".zip",)),
        (PNG, (".png",)), (JPEG, (".jpg", ".jpeg")), (WEBP, (".webp",)), (HEIC, (".heic", ".heif")),
        (M4A, (".m4a",)), (MP3, (".mp3",)), (WAV, (".wav",)), (MP4, (".mp4",)), (MOV, (".mov",)),
    ),
    *_within(
        CFB, ("application/msword", (".doc",)), ("application/vnd.ms-powerpoint", (".ppt",)),
        ("application/vnd.ms-excel", (".xls",)), ("application/x-hwp", (".hwp",)),
    ),
    *_within(
        TEXT,
        (TEXT, (".txt", ".text")), ("text/markdown", (".md", ".markdown")), ("text/html", (".html", ".htm")),
        ("text/csv", (".csv",)), ("text/tab-separated-values", (".tsv",)),
        ("application/x-subrip", (".srt",)), ("text/vtt", (".vtt",)),
        ("text/x-python", (".py",)), ("text/x-c", (".c", ".h")), ("text/x-c++", (".cpp", ".cc", ".hpp")),
        ("text/x-java", (".java",)), ("text/javascript", (".js", ".mjs")), ("text/x-typescript", (".ts",)),
        ("application/json", (".json",)), ("application/xml", (".xml",)), ("application/yaml", (".yaml", ".yml")),
        ("application/x-ipynb+json", (".ipynb",)),
    ),
)

BY_MEDIA_TYPE: Mapping[str, Format] = MappingProxyType({each.media_type: each for each in FORMATS})
BY_EXTENSION: Mapping[str, Format] = MappingProxyType({extension: each for each in FORMATS for extension in each.extensions})


@dataclass(frozen=True)
class Reader:
    """What an extractor descriptor tells refine: its job type and the params it takes for each media type."""

    job_type: str
    accepts: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class Refined:
    detected_mime: str
    jobs: tuple[tuple[str, Mapping[str, object]], ...]
    """The job type and params of each reader of detected_mime; empty when none reads it."""
    mismatch: bool
    """The name or the declared MIME says a type of another family than the bytes."""


def extension(filename: str | None) -> str | None:
    """The last suffix of filename, lowercased; None without one. The name is a client's string, so it is NFC'd
    first and only its last path part counts."""
    if not filename:
        return None
    name = unicodedata.normalize("NFC", filename).replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, suffix = name.rpartition(".")
    return f".{suffix.lower()}" if dot and stem and suffix else None


def refine(media_type: str, filename: str | None, declared_mime: str | None, readers: Iterable[Reader]) -> Refined:
    """The Attachment's media type and what reads it, from the family identify found and what the client said.

    A claim, the declared MIME first and then the extension, counts when the table knows it. One of the bytes'
    family narrows it, the first that does; one of another family is a mismatch and is ignored."""
    claims = [each for each in (BY_MEDIA_TYPE.get(declared_mime or ""), BY_EXTENSION.get(extension(filename) or "")) if each]
    mismatch = any(claim.family != media_type for claim in claims)
    narrowing = [claim.media_type for claim in claims if claim.family == media_type and claim.media_type != media_type]
    detected = narrowing[0] if narrowing else media_type
    jobs = tuple((reader.job_type, MappingProxyType(dict(reader.accepts[detected]))) for reader in readers if detected in reader.accepts)
    return Refined(detected, jobs, mismatch)
