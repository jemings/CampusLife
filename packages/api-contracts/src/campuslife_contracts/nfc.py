"""Rule nfc: every member name and string on the wire is NFC (U2).

Only the string value of a member whose name ends in _raw keeps its original
form; objects and arrays under such a member are checked as usual.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator

from .jsonio import pointer
from .violations import Rule, RuleViolation

RAW_SUFFIX = "_raw"


def _is_nfc(text: str) -> bool:
    return unicodedata.is_normalized("NFC", text)


def nfc_violations(instance: object, path: tuple[object, ...] = ()) -> Iterator[RuleViolation]:
    match instance:
        case str() if not _is_nfc(instance):
            yield RuleViolation(pointer(path), Rule.NFC)
        case dict():
            for name, value in instance.items():
                member = (*path, name)
                if not _is_nfc(name):
                    yield RuleViolation(pointer(member), Rule.NFC)
                if not (name.endswith(RAW_SUFFIX) and isinstance(value, str)):
                    yield from nfc_violations(value, member)
        case list():
            for index, value in enumerate(instance):
                yield from nfc_violations(value, (*path, index))
