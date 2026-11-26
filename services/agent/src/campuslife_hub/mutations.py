"""The Mutation applier (D1, D18, E6): the only write path for user data.

Sync, the local UI and local import all call `apply`. Every Mutation is checked
by `campuslife_contracts.validate()` here and nowhere else; each runs in its own
transaction, and a result other than retry is remembered under its mutation_id
for RESULT_RETENTION, so a resend gets the first answer back.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta

from campuslife_contracts import Rule, RuleViolation, Violation, contract_root, validate

from .capture_commit import commit_capture
from .clock import instant, now_instant
from .db import Database, Tx
from .devices import DeviceId
from .outcomes import Outcome, Reason, Rejected, outcome_json
from .user_data import create, delete, patch, restore

REASON_BY_RULE: dict[Rule, Reason] = {Rule.NFC: Reason.NOT_CANONICAL, Rule.OWNER: Reason.OWNER}
"""Rules with a reason of their own; every other violation, and any rule added later, is schema."""

REASON_PRECEDENCE = (Reason.SCHEMA, Reason.OWNER, Reason.NOT_CANONICAL)
"""When violations map to several reasons, the first of these that occurs is the answer."""

RESULT_RETENTION = timedelta(days=90)


class NoMutationId(ValueError):
    """A batch entry has no mutation_id a result could carry; the whole batch is malformed."""


class UnsupportedOp(ValueError):
    """A batch entry names a contract op this Hub does not apply yet; the batch is refused untouched."""


def reason_for(violations: Iterable[Violation]) -> Reason:
    reasons = {
        REASON_BY_RULE.get(violation.rule, Reason.SCHEMA) if isinstance(violation, RuleViolation) else Reason.SCHEMA
        for violation in violations
    }
    return next(reason for reason in REASON_PRECEDENCE if reason in reasons)


Handler = Callable[[Tx, dict, DeviceId], Outcome]

HANDLERS: dict[str, Handler] = {
    "capture.commit": lambda tx, mutation, device: commit_capture(tx, mutation["capture"], device),
    "create": create,
    "patch": patch,
    "delete": delete,
    "restore": restore,
}

CONTRACT_OPS = frozenset(
    json.loads((contract_root() / "schemas" / "mutation" / "mutation.json").read_text(encoding="utf-8"))["properties"]["op"]["enum"]
)


def _mutation_id(mutation: object) -> str:
    identifier = mutation.get("mutation_id") if isinstance(mutation, dict) else None
    if not isinstance(identifier, str) or validate("common/uuid", identifier):
        raise NoMutationId(f"no UUIDv4 mutation_id in {mutation!r:.200}")
    return identifier


def _refuse_unapplied_op(raw_mutation: dict) -> None:
    op = raw_mutation.get("op")
    if isinstance(op, str) and op in CONTRACT_OPS and op not in HANDLERS:
        raise UnsupportedOp(f"this Hub does not apply op {op} yet")


def _decide(tx: Tx, mutation: dict, violations: list[Violation], device: DeviceId) -> Outcome:
    if violations:
        return Rejected(reason_for(violations))
    return HANDLERS[mutation["op"]](tx, mutation, device)


def _apply_one(tx: Tx, mutation_id: str, mutation: dict, violations: list[Violation], device: DeviceId) -> dict:
    recorded = tx.execute("SELECT result FROM applied_mutations WHERE mutation_id = ?", (mutation_id,)).fetchone()
    if recorded is not None:
        return {"mutation_id": mutation_id, "result": "duplicate", "original": json.loads(recorded[0])}
    result = {"mutation_id": mutation_id, **outcome_json(_decide(tx, mutation, violations, device))}
    if result["result"] != "retry":
        tx.execute(
            "INSERT INTO applied_mutations (mutation_id, device_id, result, applied_at) VALUES (?, ?, ?, ?)",
            (mutation_id, device, json.dumps(result, ensure_ascii=False), now_instant()),
        )
    return result


async def apply(db: Database, batch: Sequence[object], device: DeviceId) -> list[dict]:
    """One result per Mutation in order; after the first retry the rest are not_attempted.

    A batch with an entry no result can describe raises before anything is written.
    """
    mutation_ids = [_mutation_id(mutation) for mutation in batch]
    for mutation in batch:
        _refuse_unapplied_op(mutation)
    results: list[dict] = []
    for mutation_id, mutation in zip(mutation_ids, batch, strict=True):
        if results and results[-1]["result"] in ("retry", "not_attempted"):
            results.append({"mutation_id": mutation_id, "result": "not_attempted"})
            continue
        violations = validate("mutation/mutation", mutation)
        results.append(await db.write(lambda tx: _apply_one(tx, mutation_id, mutation, violations, device)))
    return results


async def forget_old_results(db: Database, now: datetime) -> None:
    """Drop the results recorded more than RESULT_RETENTION before `now` (D18).

    An older resend is judged again. A create of an Item or a Tag compares with its first payload
    and a patch with the row, so neither applies twice; a delete, a restore and a Relation create
    have nothing to compare with and apply again. Accepted: on a single-user Hub an Outbox that
    could not send for that long is rare.
    """
    cutoff = instant(now - RESULT_RETENTION)
    await db.write(lambda tx: tx.execute("DELETE FROM applied_mutations WHERE applied_at < ?", (cutoff,)))
