"""The text extractor (S11): plain text and Markdown, with the standard library only.

The bytes decode as UTF-8 or UTF-16 with a BOM, UTF-8, or CP949, in that order; what none decodes is read as
UTF-8 with U+FFFD in place of the bad bytes and a replaced_characters warning. A block is a paragraph, the lines
between blank lines. In Markdown a line of 1 to 6 # and a space is a heading block, and the headings above a block
make its doc_path; a fenced code block is one block, with no headings inside it. Where the blocks go, cutting long
paragraphs and stopping at the limits are document.Outline's; a file longer than max_text_chars characters can take
is read only that far, partial, with a limit_reached warning.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from .document import LIMIT_REACHED, Outline
from .extracted import Extracted
from .limits import Limits

VERSION = "1"
"""1 still: Outline cuts a heading longer than max_block_text_chars, which v1 emitted whole and the Hub refused to
store; every output v1 could store is unchanged (#81 re-review, a differential run over 4,000 random inputs)."""

MAX_BYTES_PER_CHAR = 4
"""Reading max_text_chars times this many bytes reads at least max_text_chars characters in any of the encodings."""

HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.*)")
"""The heading's text is cut by _heading, not here: a lazy text before an optional closing # sequence backtracks
over every blank of a long line, quadratic in its length."""
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


def _heading(line: str) -> tuple[int, str] | None:
    """The level and text of a Markdown heading line, without a closing sequence of # after a blank; None for any
    other line or a heading with no text."""
    found = HEADING.match(line)
    if found is None:
        return None
    text = found.group(2).strip()
    closed = text.rstrip("#")
    if closed != text and closed[-1:] in (" ", "\t"):
        text = closed.strip()
    return (len(found.group(1)), text) if text else None


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
        heading = _heading(line) if markdown else None
        if opening or heading or not line.strip():
            if paragraph:
                yield None, "\n".join(paragraph)
                paragraph = []
        if opening:
            fence = opening.group(1)[0] * 3
            paragraph = [line]
        elif heading:
            yield heading
        elif line.strip():
            paragraph.append(line)
    if paragraph:
        yield None, "\n".join(paragraph)


def extract(path: Path, params: Mapping[str, object], limits: Limits) -> Extracted:
    markdown = params.get("syntax") == "markdown"
    budget = limits.max_text_chars * MAX_BYTES_PER_CHAR
    with open(path, "rb") as source:
        data = source.read(budget + 1)
    text, replaced = decode(data[:budget], final=len(data) <= budget)
    outline = Outline(limits)
    result: Extracted = outline.result
    if replaced:
        result.warn("replaced_characters")
    if len(data) > budget:
        result.warn(LIMIT_REACHED)
        result.partial = True
    for level, unit in _units(text.splitlines(), markdown):
        if not (outline.heading(level, unit) if level is not None else outline.block("paragraph", unit)):
            break
    return result
