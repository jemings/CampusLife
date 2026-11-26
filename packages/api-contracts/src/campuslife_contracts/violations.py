from __future__ import annotations

import enum
from dataclasses import dataclass


class Rule(enum.StrEnum):
    """Contract rules JSON Schema cannot express (E6)."""

    NFC = "nfc"
    BOX_EXTENT = "box_extent"
    CALENDAR = "calendar"
    RANGE_ORDER = "range_order"
    RAW_FORM = "raw_form"
    RELATION_ID = "relation_id"
    RELATION_SELF = "relation_self"
    OWNER = "owner"
    BATCH_ORDER = "batch_order"
    FEED_ORDER = "feed_order"
    ORIGINAL_ID = "original_id"
    SET_TYPE = "set_type"
    TZ_EXISTS = "tz_exists"


@dataclass(frozen=True)
class SchemaViolation:
    """The instance breaks a JSON Schema keyword at `path` (a JSON Pointer)."""

    path: str
    keyword: str


@dataclass(frozen=True)
class RuleViolation:
    """The instance breaks a contract rule at `path` (a JSON Pointer)."""

    path: str
    rule: Rule


Violation = SchemaViolation | RuleViolation


def violation_order(violation: Violation) -> tuple[str, int, str]:
    match violation:
        case SchemaViolation(path, keyword):
            return (path, 0, keyword)
        case RuleViolation(path, rule):
            return (path, 1, rule)


def describe(violation: Violation) -> str:
    match violation:
        case SchemaViolation(path, keyword):
            return f"{path} {keyword}"
        case RuleViolation(path, rule):
            return f"{path} rule {rule}"


class MalformedExpectation(ValueError):
    pass


def _is_pointer(value: object) -> bool:
    return isinstance(value, str) and (value == "" or value.startswith("/"))


def violation_from_json(item: object) -> Violation:
    """Read one `{path, keyword}` or `{path, rule}` entry of an expect file (E7)."""
    match item:
        case {"path": path, "keyword": str(keyword)} if len(item) == 2 and _is_pointer(path) and keyword:
            return SchemaViolation(path, keyword)
        case {"path": path, "rule": str(rule)} if len(item) == 2 and _is_pointer(path) and rule in set(Rule):
            return RuleViolation(path, Rule(rule))
    raise MalformedExpectation(f"not {{path, keyword}} or {{path, rule}} with a known rule: {item!r}")


def expectation_from_json(document: object) -> frozenset[Violation]:
    if not isinstance(document, list):
        raise MalformedExpectation("an expect file holds a JSON array")
    violations = [violation_from_json(item) for item in document]
    if len(set(violations)) != len(violations):
        raise MalformedExpectation("the same violation is listed twice")
    return frozenset(violations)
