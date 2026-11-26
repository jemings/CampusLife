"""ECMA-262 regular expressions, the dialect JSON Schema `pattern` is written in."""

from __future__ import annotations

from functools import lru_cache

import regress


@lru_cache(maxsize=None)
def _ecma_regex(pattern: str) -> regress.Regex:
    return regress.Regex(pattern)


def ecma_search(pattern: str, text: str) -> bool:
    # Python `re` lets `$` match before a trailing newline and `\d` match
    # non-ASCII digits; ECMA-262 does neither.
    return _ecma_regex(pattern).find(text) is not None
