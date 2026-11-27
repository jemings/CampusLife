"""The text extractor (S11): plain text and Markdown, with the standard library only.

The bytes decode as UTF-8 or UTF-16 with a BOM, UTF-8, or CP949, in that order; what none decodes is read as
UTF-8 with U+FFFD in place of the bad bytes and a replaced_characters warning. A block is a paragraph, the lines
between blank lines. In Markdown a line of 1 to 6 # and a space is a heading block, and the headings above a block
make its doc_path; a fenced code block is one block, with no headings inside it. A paragraph longer than
max_block_text_chars is cut into blocks of that length. Past max_blocks blocks or max_text_chars characters the
extraction stops, partial, with a limit_reached warning; a file longer than those characters can take is read only
that far.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from .extracted import Extracted
from .limits import Limits

VERSION = "1"

MAX_BYTES_PER_CHAR = 4
"""Reading max_text_chars times this many bytes reads at least max_text_chars characters in any of the encodings."""

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def _decoded(data: bytes, encoding: str, final: bool, errors: str = "strict") -> str:
    return codecs.getincrementaldecoder(encoding)(errors).decode(data, final=final)


def decode(data: bytes, *, final: bool = True) -> tuple[str, bool]:
    """The text and whether characters were replaced. Without final, data is the start of a longer file, and a
    character cut at its end is left out."""
    for bom, encoding in BOMS:
        if data.startswith(bom):
            try:
                return _decoded(data, encoding, final), False
            except UnicodeDecodeError:
                return _decoded(data, encoding, final, "replace"), True
    for encoding in ("utf-8", "cp949"):
        try:
            return _decoded(data, encoding, final), False
        except UnicodeDecodeError:
            continue
    return _decoded(data, "utf-8", final, "replace"), True


def _units(lines: list[str], markdown: bool) -> Iterator[tuple[int | None, str]]:
    """(heading level, text) for a heading, (None, text) for a paragraph or a fenced block, in order."""
    paragraph: list[str] = []
    fence: str | None = None
    for line in lines:
        if fence is not None:
            paragraph.append(line)
            if line.strip().startswith(fence):
                yield None, "\n".join(paragraph)
                paragraph, fence = [], None
            continue
        opening = FENCE.match(line) if markdown else None
        heading = HEADING.match(line) if markdown else None
        if opening or (heading and heading.group(2).strip()) or not line.strip():
            if paragraph:
                yield None, "\n".join(paragraph)
                paragraph = []
        if opening:
            fence = opening.group(1)[0] * 3
            paragraph = [line]
        elif heading and heading.group(2).strip():
            yield len(heading.group(1)), heading.group(2).strip()
        elif line.strip():
            paragraph.append(line)
    if paragraph:
        yield None, "\n".join(paragraph)


def _pieces(text: str, size: int) -> list[str]:
    return [text[start:start + size] for start in range(0, len(text), size)] or [text]


def _stop(result: Extracted) -> Extracted:
    result.partial = True
    result.warn("limit_reached")
    return result


def extract(path: Path, params: Mapping[str, object], limits: Limits) -> Extracted:
    markdown = params.get("syntax") == "markdown"
    budget = limits.max_text_chars * MAX_BYTES_PER_CHAR
    with open(path, "rb") as source:
        data = source.read(budget + 1)
    text, replaced = decode(data[:budget], final=len(data) <= budget)
    result = Extracted()
    if replaced:
        result.warn("replaced_characters")
    if len(data) > budget:
        result.warn("limit_reached")
    left = limits.max_text_chars
    headings: list[tuple[int, str]] = []
    counts = [0]  # counts[0]: blocks before the first heading; counts[i + 1]: blocks under headings[i]
    for level, unit in _units(text.splitlines(), markdown):
        if level is not None:
            pieces, block_type, payload = [unit], "heading", {"level": level}
            while headings and headings[-1][0] >= level:
                headings.pop()
                counts.pop()
        else:
            pieces, block_type, payload = _pieces(unit, limits.max_block_text_chars), "paragraph", None
        for piece in pieces:
            if len(result.blocks) == limits.max_blocks or left == 0:
                return _stop(result)
            cut, piece = len(piece) > left, piece[:left]
            left -= len(piece)
            ref = {"kind": "doc_path", "heading_path": [name for _, name in headings], "block_index": counts[-1]}
            block = {"block_type": block_type, "text": piece, "refs": [ref]}
            if payload is not None:
                block["payload"] = payload
            result.blocks.append(block)
            counts[-1] += 1
            if cut:
                return _stop(result)
        if level is not None:
            headings.append((level, unit))
            counts.append(0)
    result.partial = len(data) > budget
    return result
