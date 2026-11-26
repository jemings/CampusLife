from __future__ import annotations

import enum
from dataclasses import dataclass


class Rule(enum.StrEnum):
    """Contract rules JSON Schema cannot express (E6)."""

    NFC = "nfc"
    BOX_EXTENT = "box_extent"
    CALENDAR = "calendar"
    RANGE_ORDER = "range_order"
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

