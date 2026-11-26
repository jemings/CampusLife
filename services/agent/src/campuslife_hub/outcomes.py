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
    pass


@dataclass(frozen=True)
class Rejected:
    reason: Reason


@dataclass(frozen=True)
class Retry:
    missing_blobs: tuple[str, ...]


Outcome = Applied | Rejected | Retry


def outcome_json(outcome: Outcome) -> dict:
    match outcome:
        case Applied():
            return {"result": "applied"}
        case Rejected(reason):
            return {"result": "rejected", "reason": reason.value}
        case Retry(missing_blobs):
            return {"result": "retry", "missing_blobs": list(missing_blobs)}
