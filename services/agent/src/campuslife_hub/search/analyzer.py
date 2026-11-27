"""How text becomes searchable tokens, and how a typed query becomes a safe FTS5 search.

FTS5's `unicode61` tokenizer splits on spaces and punctuation, so "자료구조를" is one token and a
search for "자료구조" misses it. `hangul-bigram@1` rewrites every run of Hangul or CJK letters
into its overlapping two-letter pieces before FTS5 sees it ("자료구조를" → "자료 료구 구조 조를"),
and the same rewrite turns a query term into a phrase of those pieces, so a query matches any
document that holds it as a substring. Other text is left to `unicode61`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol


class TextAnalyzer(Protocol):
    """Turns text into what an FTS5 column under `fts5_tokenize` indexes; documents and queries alike."""

    @property
    def id(self) -> str:
        """`name@version`; an index built under another id is rebuilt from scratch."""

    @property
    def fts5_tokenize(self) -> str:
        """The FTS5 `tokenize` option the analyzed text is written for."""

    def analyze(self, text: str) -> str:
        """The text to store in the FTS5 column, NFC-normalized first."""

    def phrase(self, term: str) -> str | None:
        """The analyzed form of one NFC query term, or None when the index cannot find it.

        The caller then looks for the term as a substring of the original text instead.
        """


_CJK_LETTERS = (
    r"\u1100-\u11ff"  # Hangul Jamo
    r"\u3131-\u318e"  # Hangul Compatibility Jamo
    r"\ua960-\ua97f"  # Hangul Jamo Extended-A
    r"\uac00-\ud7a3"  # Hangul Syllables
    r"\ud7b0-\ud7ff"  # Hangul Jamo Extended-B
    r"\u3041-\u3096\u309d-\u309f"  # Hiragana letters
    r"\u30a1-\u30fa\u30fc-\u30ff"  # Katakana letters
    r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003ffff"  # CJK ideographs
)
_CJK_RUN = re.compile(f"[{_CJK_LETTERS}]+")


def _has_unicode61_token(term: str) -> bool:
    # unicode61's default token characters are categories L*, N* and Co; the rest separate tokens.
    return any(unicodedata.category(char)[0] in "LN" or unicodedata.category(char) == "Co" for char in term)


def _has_lone_letter(term: str) -> bool:
    """A run of one letter is indexed only where it stands alone; inside a longer run it is half of a pair."""
    return any(len(run.group()) == 1 for run in _CJK_RUN.finditer(term))


def _bigrams(run: str) -> str:
    if len(run) == 1:
        return run
    return " ".join(run[i : i + 2] for i in range(len(run) - 1))


@dataclass(frozen=True)
class HangulBigram:
    id: str = "hangul-bigram@1"
    fts5_tokenize: str = "unicode61"

    def analyze(self, text: str) -> str:
        nfc = unicodedata.normalize("NFC", text)
        return _CJK_RUN.sub(lambda run: f" {_bigrams(run.group())} ", nfc)

    def phrase(self, term: str) -> str | None:
        if not _has_unicode61_token(term) or _has_lone_letter(term):
            return None
        return self.analyze(term).strip()


HANGUL_BIGRAM = HangulBigram()

LIKE_ESCAPE = "\\"
MAX_QUERY_CHARS = 256
MAX_QUERY_TERMS = 16


class QueryTooLong(ValueError):
    """The query is longer than MAX_QUERY_CHARS (in NFC) or has more than MAX_QUERY_TERMS terms."""


@dataclass(frozen=True)
class SearchPlan:
    """A query made safe: an FTS5 MATCH expression, and LIKE patterns for the original text.

    A document matches when it matches `match` (if any) and every pattern in `like`, each
    used as `text LIKE ? ESCAPE LIKE_ESCAPE` against the document's NFC original.
    """

    match: str | None
    like: tuple[str, ...]


def _quoted(phrase: str) -> str:
    return '"' + phrase.replace('"', '""') + '"'


def _substring_pattern(term: str) -> str:
    escaped = term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")
    return f"%{escaped}%"


_UNBINDABLE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
"""Control characters (a NUL ends an FTS5 string early) and lone surrogates (SQLite cannot bind them)."""


def plan_query(query: str, analyzer: TextAnalyzer) -> SearchPlan | None:
    """The plan for a query typed by a user, or None when it holds nothing to search for."""
    text = unicodedata.normalize("NFC", _UNBINDABLE.sub(" ", query))
    if len(text) > MAX_QUERY_CHARS:
        raise QueryTooLong(f"a query holds at most {MAX_QUERY_CHARS} characters")
    terms = text.split()
    if len(terms) > MAX_QUERY_TERMS:
        raise QueryTooLong(f"a query holds at most {MAX_QUERY_TERMS} terms")
    if not terms:
        return None
    matched, scanned = [], []
    for term in terms:
        phrase = analyzer.phrase(term)
        if phrase is None:
            scanned.append(_substring_pattern(term))
        else:
            matched.append(_quoted(phrase))
    return SearchPlan(" AND ".join(matched) or None, tuple(scanned))
