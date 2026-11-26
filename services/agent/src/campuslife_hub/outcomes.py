"""What a handler decided about one Mutation, before the applier wraps it as a wire result (E12).

duplicate and not_attempted are the applier's own answers, so handlers cannot return them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Reason(enum.StrEnum):
    ID_REUSE = "id_reuse"
    NOT_CANONICAL = "not_canonical"
    OWNER = "owner"
    SCHEMA = "schema"
    DELETED = "deleted"
    LINK_TARGET_MISSING = "link_target_missing"
    UNSUPPORTED_ENTITY = "unsupported_entity"


@dataclass(frozen=True)
class Applied:
    applied_fields: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Conflict:
    """Some set keys lost compare-and-set (D18); applied_fields are those now holding their to."""

    applied_fields: tuple[str, ...]
    conflicted_fields: tuple[str, ...]


@dataclass(frozen=True)
class Rejected:
    reason: Reason


@dataclass(frozen=True)
class Retry:
    missing_blobs: tuple[str, ...]


Outcome = Applied | Conflict | Rejected | Retry


def outcome_json(outcome: Outcome) -> dict:
    match outcome:
        case Applied(None):
            return {"result": "applied"}
        case Applied(applied_fields):
            return {"result": "applied", "applied_fields": list(applied_fields)}
        case Conflict(applied_fields, conflicted_fields):
            return {"result": "conflict", "applied_fields": list(applied_fields), "conflicted_fields": list(conflicted_fields)}
        case Rejected(reason):
            return {"result": "rejected", "reason": reason.value}
        case Retry(missing_blobs):
            return {"result": "retry", "missing_blobs": list(missing_blobs)}
