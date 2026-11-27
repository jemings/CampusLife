"""ConflictRecords (D18, D24 decisions 1, 2, 6 and 11): what a patch submitted and the Hub did not write.

One record per Mutation, its id the Mutation's mutation_id, holding the submitted changes of the keys that were not
written as the patch's set sent them. Open while it carries set, resolved once it carries resolved instead. Every write
goes through store.write, so records reach the changes feed as entity conflict_record.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from campuslife_contracts import effective_cloud_policy

from . import store
from .db import Snapshot, Tx
from .store import Entity, EntityRef


def keep(tx: Tx, mutation_id: str, target: EntityRef, changes: dict[str, dict], keys: Iterable[str]) -> tuple[str, ...]:
    """Keep the changes of `keys` under mutation_id and return the ids a result names in conflict_record_ids.

    A Mutation has one record for good: when a resend is judged again after its result was forgotten, the record it
    made, open or resolved, stays as it is and is named again. A record purged with its entity is not made again.
    """
    kept = {key: changes[key] for key in keys}
    if not kept:
        return ()
    record = EntityRef(Entity.CONFLICT_RECORD, mutation_id)
    if store.ever_written(tx, record):
        return () if store.get(tx, record) is None else (mutation_id,)
    store.write(tx, record, {"id": mutation_id, "entity_ref": f"{target.entity.value}:{target.id}", "set": kept})
    return (mutation_id,)


def _of(runner: Tx | Snapshot, entity_refs: Iterable[str]) -> list[dict]:
    rows = runner.execute(
        "SELECT data FROM conflict_records WHERE json_extract(data, '$.entity_ref') IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(list(entity_refs)),),
    ).fetchall()
    return [json.loads(data) for (data,) in rows]


def purge_with(tx: Tx, targets: Iterable[EntityRef]) -> None:
    """A purge of an entity takes its records with it, open or not: the user chose to destroy it (D24 decision 2)."""
    for record in _of(tx, (f"{target.entity.value}:{target.id}" for target in targets)):
        store.write(tx, EntityRef(Entity.CONFLICT_RECORD, record["id"]), None)


def open_count(runner: Tx | Snapshot) -> int:
    (count,) = runner.execute("SELECT count(*) FROM conflict_records WHERE json_type(data, '$.set') IS NOT NULL").fetchone()
    return count


def strictest_submitted_cloud_policy(runner: Tx | Snapshot, attachment_id: str) -> str | None:
    """The strictest cloud_policy an open record of the Attachment submitted, or None.

    While such a record is open the Hub's value may be looser than one the user asked for, so a provider call takes
    the minimum of the two (D8, D24 decision 11).
    """
    submitted = [record["set"]["cloud_policy"]["to"] for record in _of(runner, [f"attachment:{attachment_id}"]) if "cloud_policy" in record.get("set", {})]
    return effective_cloud_policy(submitted) if submitted else None
