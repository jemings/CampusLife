"""Contract rules bound to a place in a schema through an x- keyword (E6).

A rule judges only values that already fit their own schema shape, so one
defect yields one violation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import unicodedata
from datetime import date
from functools import cache
from importlib.resources import files
from typing import NamedTuple

from jsonschema import ValidationError

from .ecma import ecma_search
from .violations import Rule


@cache
def _iana_zones() -> frozenset[str]:
    # Judged by the PyPI tzdata package so every platform agrees: Windows
    # Python has no system TZPATH.
    return frozenset(files("tzdata").joinpath("zones").read_text(encoding="utf-8").split())


def _fits_own_pattern(instance: str, schema: dict) -> bool:
    return "pattern" not in schema or ecma_search(schema["pattern"], instance)


def _tz_exists(validator, enabled, instance, schema):
    if isinstance(instance, str) and _fits_own_pattern(instance, schema) and instance not in _iana_zones():
        yield ValidationError(f"{instance!r} is not an IANA time zone")


def _is_calendar_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _calendar_date(validator, enabled, instance, schema):
    if isinstance(instance, str) and _fits_own_pattern(instance, schema) and not _is_calendar_date(instance[:10]):
        yield ValidationError(f"{instance[:10]!r} is not a date in the calendar")


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _comparable(start: object, end: object) -> bool:
    both_numbers = _is_number(start) and _is_number(end)
    both_strings = isinstance(start, str) and isinstance(end, str)
    return both_numbers or both_strings


def _member_fits(validator, schema: dict, name: str, value: object) -> bool:
    member_schema = schema.get("properties", {}).get(name, True)
    return next(validator.descend(value, member_schema), None) is None


def _range_order(validator, pairs, instance, schema):
    if not isinstance(instance, dict):
        return
    for pair in pairs:
        start, end = instance.get(pair["start"]), instance.get(pair["end"])
        if not _comparable(start, end):
            continue
        if not (_member_fits(validator, schema, pair["start"], start) and _member_fits(validator, schema, pair["end"], end)):
            continue
        in_order = start < end if pair.get("strict", False) else start <= end
        if not in_order:
            yield ValidationError(
                f"{pair['end']} must come after {pair['start']}",
                path=[pair["end"]],
            )


def _members_fit(validator, schema: dict, instance: dict, names: list[str]) -> bool:
    return all(_member_fits(validator, schema, name, instance[name]) for name in names)


def _raw_form(validator, pairs, instance, schema):
    if not isinstance(instance, dict):
        return
    for pair in pairs:
        raw, text = instance.get(pair["raw"]), instance.get(pair["text"])
        if not (isinstance(raw, str) and isinstance(text, str)):
            continue
        if not _members_fit(validator, schema, instance, [pair["raw"], pair["text"]]):
            continue
        if raw == text or unicodedata.normalize("NFC", raw) != text:
            yield ValidationError(
                f"{pair['raw']} must be the non-NFC form {pair['text']} was received in",
                path=[pair["raw"]],
            )


RELATION_PARTS = ("from", "type", "to")


def _relation_id(validator, enabled, instance, schema):
    if not isinstance(instance, dict):
        return
    parts = [instance.get(name) for name in RELATION_PARTS]
    identifier = instance.get("id")
    if not all(isinstance(value, str) for value in [identifier, *parts]):
        return
    if not _members_fit(validator, schema, instance, ["id", *RELATION_PARTS]):
        return
    if identifier != "|".join(parts):
        yield ValidationError(f"id must be {'|'.join(parts)!r}", path=["id"])


def _box_extent(validator, enabled, instance, schema):
    if not (isinstance(instance, list) and len(instance) == 4 and all(map(_is_number, instance))):
        return
    x, y, w, h = instance
    if x + w > 1 or y + h > 1:
        yield ValidationError(f"box {instance!r} reaches past the unit square")


class RuleKeyword(NamedTuple):
    rule: Rule
    check: Callable[..., Iterator[ValidationError]]


RULE_KEYWORDS = {
    "x-calendar-date": RuleKeyword(Rule.CALENDAR, _calendar_date),
    "x-tz-exists": RuleKeyword(Rule.TZ_EXISTS, _tz_exists),
    "x-range-order": RuleKeyword(Rule.RANGE_ORDER, _range_order),
    "x-box-extent": RuleKeyword(Rule.BOX_EXTENT, _box_extent),
    "x-raw-form": RuleKeyword(Rule.RAW_FORM, _raw_form),
    "x-relation-id": RuleKeyword(Rule.RELATION_ID, _relation_id),
}
