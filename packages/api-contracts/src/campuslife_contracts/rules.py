"""Contract rules bound to a place in a schema through an x- keyword (E6).

A rule judges only values that already fit their own schema shape, so one
defect yields one violation. A rule reports a violation or says nothing: on a
value of a shape it does not expect, even one its schema lets through, it
stays silent and does not raise.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import hashlib
import re
import unicodedata
from datetime import date
from functools import cache
from importlib.resources import files
from typing import NamedTuple

from jsonschema import ValidationError

from .ecma import ecma_search
from .jsonio import canonical_json
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


A1_CELL = re.compile(r"([A-Z]+)([0-9]+)")


def _a1_column(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _a1_order(validator, enabled, instance, schema):
    if not (isinstance(instance, str) and _fits_own_pattern(instance, schema) and ":" in instance):
        return
    (first_column, first_row), (last_column, last_row) = (A1_CELL.fullmatch(cell).groups() for cell in instance.split(":"))
    if _a1_column(first_column) > _a1_column(last_column) or int(first_row) > int(last_row):
        yield ValidationError(f"{instance!r} does not go from the top-left cell to the bottom-right one")


def _conflict_not_self(validator, enabled, instance, schema):
    if not isinstance(instance, dict) or not isinstance(instance.get("conflicts_with"), list):
        return
    if not _members_fit(validator, schema, instance, [name for name in ("id", "conflicts_with") if name in instance]):
        return
    for index, other in enumerate(instance["conflicts_with"]):
        if other == instance.get("id"):
            yield ValidationError("a Suggestion does not conflict with itself", path=["conflicts_with", index])


LOCATOR_ANCHORS = (("blob", "blob:"), ("capture", "capture:"))


def _locator_anchor(validator, enabled, instance, schema):
    """An evidence Relation's Locators sit in its to; a Relation of another known type has none.

    A type the schema does not list may be a newer Hub's, so its locators are left alone, and so is a
    Locator without a blob or capture, which can only be of a kind this contract does not know.
    """
    if not isinstance(instance, dict) or not isinstance(instance.get("locators"), list):
        return
    if not _members_fit(validator, schema, instance, [name for name in ("type", "to", "locators") if name in instance]):
        return
    relation_type, to = instance.get("type"), instance.get("to")
    if relation_type != "evidence":
        known = schema.get("properties", {}).get("type", {}).get("examples", [])
        if relation_type in known:
            yield ValidationError(f"a {relation_type} Relation has no locators", path=["locators"])
        return
    if not isinstance(to, str):
        return
    for index, locator in enumerate(instance["locators"]):
        anchor = next(((name, prefix) for name, prefix in LOCATOR_ANCHORS if isinstance(locator.get(name), str)), None)
        if anchor is not None and f"{anchor[1]}{locator[anchor[0]]}" != to:
            yield ValidationError(f"the locator's {anchor[0]} is not the Relation's to", path=["locators", index, anchor[0]])


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


INT64_MAX = 2**63 - 1


def _numbers(text: str) -> list[int]:
    return [int(digits) for digits in re.findall(r"[0-9]+", text)]


def _int64_text(validator, enabled, instance, schema):
    if isinstance(instance, str) and _fits_own_pattern(instance, schema) and max(_numbers(instance), default=0) > INT64_MAX:
        yield ValidationError(f"{instance!r} holds a number past int64")


def _byte_range(validator, enabled, instance, schema):
    if not (isinstance(instance, str) and _fits_own_pattern(instance, schema)):
        return
    # Numbers past int64 are rule int64_text's to report.
    if max(_numbers(instance), default=0) > INT64_MAX:
        return
    found = re.search(r"([0-9]+)-([0-9]+)(?:/([0-9]+))?$", instance)
    if found is None:
        return
    first, last = int(found[1]), int(found[2])
    if first > last:
        yield ValidationError(f"{instance!r} ends before it starts")
    elif found[3] is not None and last >= int(found[3]):
        yield ValidationError(f"{instance!r} reaches past the size")


def _chunk_index(validator, enabled, instance, schema):
    if not isinstance(instance, dict) or not all(name in instance for name in ("size", "chunk_size", "received")):
        return
    if not _members_fit(validator, schema, instance, ["size", "chunk_size", "received"]):
        return
    count = -(-instance["size"] // instance["chunk_size"])
    for index, chunk in enumerate(instance["received"]):
        if chunk >= count:
            yield ValidationError(f"chunk {chunk} is past the last of {count} chunks", path=["received", index])


def _box_extent(validator, enabled, instance, schema):
    if not (isinstance(instance, list) and len(instance) == 4 and all(map(_is_number, instance))):
        return
    x, y, w, h = instance
    if x + w > 1 or y + h > 1:
        yield ValidationError(f"box {instance!r} reaches past the unit square")


def _fitting_blocks(validator, schema, instance) -> Iterator[tuple[int, dict]]:
    """(ordinal, block) for each block that fits its own schema, when the source blob and the extractor do."""
    if not isinstance(instance, dict) or not all(name in instance for name in ("blob", "extractor", "blocks")):
        return
    if not _members_fit(validator, schema, instance, ["blob", "extractor"]):
        return
    if not (isinstance(instance["blob"], str) and isinstance(instance["extractor"], dict) and isinstance(instance["blocks"], list)):
        return
    block_schema = schema.get("properties", {}).get("blocks", {}).get("items", True)
    for ordinal, block in enumerate(instance["blocks"]):
        if isinstance(block, dict) and _fits(validator, block, block_schema):
            yield ordinal, block


def _block_id(validator, enabled, instance, schema):
    """A block's id is sha256(canonical_json([blob, extractor id, extractor version, extractor params, ordinal])), the
    ordinal its index."""
    for ordinal, block in _fitting_blocks(validator, schema, instance):
        extractor = instance["extractor"]
        derived = [instance["blob"], extractor.get("id"), extractor.get("version"), extractor.get("params"), ordinal]
        if not isinstance(block.get("id"), str) or not all(isinstance(part, str) for part in derived[:3]) or not isinstance(derived[3], dict):
            continue
        if block["id"] != hashlib.sha256(canonical_json(derived)).hexdigest():
            yield ValidationError(f"block {ordinal}'s id is not derived from its blob, extractor and ordinal",
                                  path=["blocks", ordinal, "id"])


def _block_anchor(validator, enabled, instance, schema):
    """Every ref of an extracted block names the blob it was extracted from, whatever its kind, and none is in a
    recording."""
    for ordinal, block in _fitting_blocks(validator, schema, instance):
        refs = block.get("refs")
        for index, ref in enumerate(refs if isinstance(refs, list) else []):
            if not isinstance(ref, dict):
                continue
            at = ["blocks", ordinal, "refs", index]
            if ref.get("blob") != instance["blob"]:
                yield ValidationError("the ref does not name the extracted blob", path=[*at, "blob"])
            elif "capture" in ref:
                yield ValidationError("an extracted block is not in a recording", path=[*at, "capture"])


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
    "x-locator-anchor": RuleKeyword(Rule.LOCATOR_ANCHOR, _locator_anchor),
    "x-a1-order": RuleKeyword(Rule.A1_ORDER, _a1_order),
    "x-conflict-not-self": RuleKeyword(Rule.CONFLICT_SELF, _conflict_not_self),
    "x-batch-order": RuleKeyword(Rule.BATCH_ORDER, _batch_order),
    "x-original-id": RuleKeyword(Rule.ORIGINAL_ID, _original_id),
    "x-feed-order": RuleKeyword(Rule.FEED_ORDER, _feed_order),
    "x-chunk-index": RuleKeyword(Rule.CHUNK_INDEX, _chunk_index),
    "x-int64-text": RuleKeyword(Rule.INT64_TEXT, _int64_text),
    "x-byte-range": RuleKeyword(Rule.BYTE_RANGE, _byte_range),
    "x-block-id": RuleKeyword(Rule.BLOCK_ID, _block_id),
    "x-block-anchor": RuleKeyword(Rule.BLOCK_ANCHOR, _block_anchor),
    "x-set-owner": RuleKeyword(Rule.OWNER, set_owner),
    "x-set-type": RuleKeyword(Rule.SET_TYPE, set_type),
}
