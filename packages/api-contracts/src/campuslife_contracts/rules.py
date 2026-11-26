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
from .set_rules import set_owner, set_type
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


def _relation_id(validator, parts_member, instance, schema):
    """parts_member is true when from, type and to sit next to id, else the member holding them."""
    if not isinstance(instance, dict):
        return
    holder = instance if parts_member is True else instance.get(parts_member)
    if not isinstance(holder, dict):
        return
    parts = [holder.get(name) for name in RELATION_PARTS]
    identifier = instance.get("id")
    if not all(isinstance(value, str) for value in [identifier, *parts]):
        return
    fitted = ["id", *RELATION_PARTS] if parts_member is True else ["id", parts_member]
    if not _members_fit(validator, schema, instance, fitted):
        return
    if identifier != "|".join(parts):
        yield ValidationError(f"id must be {'|'.join(parts)!r}", path=["id"])


def _relation_not_self(validator, enabled, instance, schema):
    if not isinstance(instance, dict):
        return
    ends = [instance.get("from"), instance.get("to")]
    if not all(isinstance(end, str) for end in ends) or ends[0] != ends[1]:
        return
    if _members_fit(validator, schema, instance, ["from", "to"]):
        yield ValidationError("a relation cannot link an entity to itself", path=["to"])


def _batch_order(validator, enabled, instance, schema):
    if not isinstance(instance, list):
        return
    after_retry = False
    for index, item in enumerate(instance):
        if not isinstance(item, dict) or next(validator.descend(item, schema.get("items", True)), None) is not None:
            return
        result = item.get("result")
        if after_retry and result != "not_attempted":
            yield ValidationError("every mutation after the first retry is not_attempted", path=[index, "result"])
        elif not after_retry and result == "not_attempted":
            yield ValidationError("not_attempted only follows a retry", path=[index, "result"])
        after_retry = after_retry or result == "retry"


def _fits(validator, value, schema) -> bool:
    return next(validator.descend(value, schema), None) is None


def _original_id(validator, enabled, instance, schema):
    if not isinstance(instance, dict) or not isinstance(instance.get("original"), dict):
        return
    id_schema = schema.get("properties", {}).get("mutation_id", True)
    outer, inner = instance.get("mutation_id"), instance["original"].get("mutation_id")
    if not isinstance(outer, str) or not _fits(validator, outer, id_schema):
        return
    if inner is None or not _fits(validator, inner, id_schema):
        return
    if inner != outer:
        yield ValidationError("original repeats the result of this same mutation_id", path=["original", "mutation_id"])


def _feed_order(validator, enabled, instance, schema):
    if not isinstance(instance, dict) or not isinstance(instance.get("changes"), list):
        return
    properties = schema.get("properties", {})
    row_schema = properties.get("changes", {}).get("items", True)
    previous = highest = None
    for index, row in enumerate(instance["changes"]):
        if not isinstance(row, dict) or not _fits(validator, row, row_schema):
            continue
        data = row.get("data")
        if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"] != row["id"]:
            yield ValidationError("a row's id is its data's id", path=["changes", index, "id"])
        rev = row["rev"]
        if previous is not None and rev <= previous:
            yield ValidationError("revs rise strictly through a page", path=["changes", index, "rev"])
        previous = rev
        highest = rev if highest is None else max(highest, rev)
    next_since = instance.get("next_since")
    if highest is not None and _fits(validator, next_since, properties.get("next_since", True)) and next_since < highest:
        yield ValidationError("next_since is at least the highest rev of the page", path=["next_since"])


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
    "x-relation-not-self": RuleKeyword(Rule.RELATION_SELF, _relation_not_self),
    "x-batch-order": RuleKeyword(Rule.BATCH_ORDER, _batch_order),
    "x-original-id": RuleKeyword(Rule.ORIGINAL_ID, _original_id),
    "x-feed-order": RuleKeyword(Rule.FEED_ORDER, _feed_order),
    "x-set-owner": RuleKeyword(Rule.OWNER, set_owner),
    "x-set-type": RuleKeyword(Rule.SET_TYPE, set_type),
}
