"""Outline (the extractor issues' common decision): where the blocks of a document go and when reading stops.

An extractor hands Outline its headings and blocks in reading order, and Outline owns what every document extractor
would otherwise repeat:
- the heading stack and each block's doc_path: a heading closes the headings of its level and deeper, and a block's
  block_index counts the blocks under the last heading of its heading_path, from 0. A block that names its own ref
  (a PDF page, a slide) is not counted there;
- cutting a block's text into blocks of at most max_block_text_chars;
- stopping at max_blocks blocks or max_text_chars characters: the result is partial with a limit_reached warning, and
  every later call adds nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from .extracted import Extracted
from .limits import Limits

LIMIT_REACHED = "limit_reached"


def _pieces(text: str, size: int) -> list[str]:
    return [text[start:start + size] for start in range(0, len(text), size)] or [text]


class Outline:
    def __init__(self, limits: Limits):
        self.result = Extracted()
        self._limits = limits
        self._left = limits.max_text_chars
        self._headings: list[tuple[int, str]] = []
        self._counts = [0]
        """_counts[0]: blocks before the first heading; _counts[i + 1]: blocks under _headings[i]."""
        self.stopped = False

    def heading(self, level: int, text: str) -> bool:
        """A heading of level (1 the outermost) and its text, whole: it is not cut into pieces. False once reading
        stopped."""
        while self._headings and self._headings[-1][0] >= level:
            self._headings.pop()
            self._counts.pop()
        if not self._add("heading", text, {"level": level}, None):
            return False
        self._headings.append((level, text))
        self._counts.append(0)
        return True

    def block(self, block_type: str, text: str | None, *, payload: Mapping[str, object] | None = None, ref: Mapping[str, object] | None = None) -> bool:
        """A block, cut into pieces of max_block_text_chars, each with the payload; ref is its Locator without v and
        blob, its doc_path when None. A block without text is one block. False once reading stopped."""
        pieces = [None] if text is None else _pieces(text, self._limits.max_block_text_chars)
        return all(self._add(block_type, piece, payload, ref) for piece in pieces)

    def _add(self, block_type: str, text: str | None, payload: Mapping[str, object] | None, ref: Mapping[str, object] | None) -> bool:
        if self.stopped:
            return False
        if len(self.result.blocks) == self._limits.max_blocks or self._left == 0:
            return self._stop()
        cut = text is not None and len(text) > self._left
        block: dict = {"block_type": block_type}
        if text is not None:
            text = text[:self._left]
            self._left -= len(text)
            block["text"] = text
        if ref is None:
            ref = {"kind": "doc_path", "heading_path": [name for _, name in self._headings], "block_index": self._counts[-1]}
            self._counts[-1] += 1
        block["refs"] = [dict(ref)]
        if payload is not None:
            block["payload"] = dict(payload)
        self.result.blocks.append(block)
        return not cut or self._stop()

    def _stop(self) -> bool:
        self.stopped = True
        self.result.partial = True
        self.result.warn(LIMIT_REACHED)
        return False
