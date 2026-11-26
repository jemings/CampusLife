"""Wire JSON: strict reading and the canonical byte form."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path


def pointer(parts: Iterable[object]) -> str:
    """RFC 6901 JSON Pointer for a path of member names and array indexes."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


class NotStrictJson(ValueError):
    pass


def _reject_constant(name: str):
    raise NotStrictJson(f"{name} is not JSON")


def _unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    members: dict[str, object] = {}
    for name, value in pairs:
        if name in members:
            raise NotStrictJson(f"duplicate member {name!r}")
        members[name] = value
    return members


def _reject_lone_surrogates(value: object) -> None:
    match value:
        case str():
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise NotStrictJson(f"lone surrogate in {value!r}") from error
        case list():
            for item in value:
                _reject_lone_surrogates(item)
        case dict():
            for name, item in value.items():
                _reject_lone_surrogates(name)
                _reject_lone_surrogates(item)


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise NotStrictJson(f"{text} is beyond the range of a double")
    return number


def loads_strict(text: str, *, exact_numbers: bool = False) -> object:
    """Parse JSON text; with exact_numbers every number becomes a Decimal.

    Rejects duplicate members, NaN/Infinity, escapes of lone surrogates (JSON
    syntax but not Unicode text) and, without exact_numbers, numbers a double
    or a Python int cannot hold.
    """
    try:
        value = json.loads(
            text,
            parse_float=Decimal if exact_numbers else _finite_float,
            parse_int=Decimal if exact_numbers else None,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_members,
        )
        _reject_lone_surrogates(value)
    except NotStrictJson:
        raise
    except ValueError as error:
        raise NotStrictJson(str(error)) from error
    except RecursionError as error:
        raise NotStrictJson("nested deeper than Python can read") from error
    return value


def read_strict(path: Path, *, exact_numbers: bool = False) -> object:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise NotStrictJson(f"not UTF-8 ({error})") from error
    return loads_strict(text, exact_numbers=exact_numbers)


PLAIN_EXPONENT_LIMIT = 20


def _canonical_number(number: int | float | Decimal) -> str:
    exact = Decimal(repr(number)) if isinstance(number, float) else Decimal(number)
    if not exact.is_finite():
        raise ValueError(f"{number!r} is not a JSON number")
    if exact.is_zero():
        return "0"
    sign, digit_tuple, exponent = exact.as_tuple()
    digits = "".join(map(str, digit_tuple)).lstrip("0")
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    digits = stripped
    adjusted = len(digits) - 1 + exponent
    minus = "-" if sign else ""
    if abs(adjusted) > PLAIN_EXPONENT_LIMIT:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{minus}{mantissa}e{'+' if adjusted >= 0 else '-'}{abs(adjusted)}"
    if exponent >= 0:
        return minus + digits + "0" * exponent
    point = len(digits) + exponent
    if point > 0:
        return f"{minus}{digits[:point]}.{digits[point:]}"
    return f"{minus}0.{'0' * -point}{digits}"


def _canonical_text(value: object) -> str:
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int() | float() | Decimal():
            return _canonical_number(value)
        case str():
            return json.dumps(value, ensure_ascii=False)
        case list():
            return "[" + ",".join(map(_canonical_text, value)) + "]"
        case dict():
            for name in value:
                if not isinstance(name, str):
                    raise TypeError(f"member name {name!r} is not a str")
            members = sorted(value.items())
            return "{" + ",".join(f"{json.dumps(name, ensure_ascii=False)}:{_canonical_text(item)}" for name, item in members) + "}"
    raise TypeError(f"not a JSON value: {value!r}")


def canonical_json(value: object) -> bytes:
    """One byte string per JSON value as the round-trip comparator sees it.

    Members sorted by code point, no whitespace, UTF-8, strings as their code
    points, numbers by exact decimal value (1 and 1.0 agree, 0 and -0 agree);
    an absent member and a null member differ. Floats are taken at their
    shortest repr. Used for round-trip comparison and for the Hub's
    "same content" test on id reuse.
    """
    return _canonical_text(value).encode("utf-8")
