"""HTTP header names as the contract spells them.

Header names are case-insensitive (RFC 9110) and HTTP/2 sends them in
lowercase, while the *_headers schemas name each header one way. Respell
known names before validating a set of headers against those schemas.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path

from .jsonio import read_strict

HEADER_SCHEMA_SUFFIX = "_headers.json"


@cache
def _known_names(root: Path) -> dict[str, str]:
    names = {}
    for path in sorted((root / "schemas").rglob(f"*{HEADER_SCHEMA_SUFFIX}")):
        for name in read_strict(path).get("properties", {}):
            names[name.lower()] = name
    return names


def contract_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    """Headers keyed by the contract's spelling of each name it defines.

    A name the contract defines may appear once, in any case; other names pass
    through, and a repeated one has its values joined with ", " (RFC 9110 5.3).
    """
    from . import contract_root

    known = _known_names(contract_root())
    pairs = headers.items() if isinstance(headers, Mapping) else headers
    result: dict[str, str] = {}
    spelled: dict[str, str] = {}
    for name, value in pairs:
        folded = name.lower()
        if folded in known:
            if known[folded] in result:
                raise ValueError(f"{known[folded]} appears more than once")
            result[known[folded]] = value
        elif folded in spelled:
            result[spelled[folded]] += f", {value}"
        else:
            spelled[folded] = name
            result[name] = value
    return result
