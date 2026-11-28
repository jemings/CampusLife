"""create, patch, delete and restore of user data: Items, Relations, Tags and an Attachment's cloud_policy (D1, D18, A4).

The applier has run `validate()` before any of these. What it cannot judge, because
it needs the Hub's rows, is judged here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from campuslife_contracts import contract_root, text_hash, validate

from . import conflict_records, purge, refs, store
from .clock import now_instant
from .created import earlier_create, remember_create
from .db import Tx
from .devices import DeviceId
from .outcomes import Applied, Conflict, Outcome, Reason, Rejected
from .store import Entity, EntityRef


def _schema(name: str) -> dict:
    return json.loads((contract_root() / "schemas" / name).read_text(encoding="utf-8"))


MUTATION_DEFS = _schema("mutation/mutation.json")["$defs"]

ITEM_DATA = MUTATION_DEFS["item_data"]["properties"]

ITEM_KINDS = frozenset(MUTATION_DEFS["closed_fields"])
"""Kinds whose fields the contract defines; this Hub cannot judge another kind's fields, so it does not create one."""

TEXT_COLUMNS = {
    Entity(entity): frozenset(name for name, column in _schema(f"mutation/{target['schema']}")["properties"].items() if column.get("x-text") is True)
    for entity, target in MUTATION_DEFS["patch"]["x-set-targets"].items()
}
"""Each entity's x-text columns: a patch compares them by from_hash (E7)."""

SCHEMA_VERSION = 1

FIELDS_PREFIX = "fields."


def _is_canonical_text(text: object) -> bool:
    """An x-text value arrives with LF line ends (E7), so its from_hash is the sha256 of what is stored."""
    return not isinstance(text, str) or "\r" not in text


def create_item(tx: Tx, ref: EntityRef, data: dict) -> Outcome:
    """A Note forked from an Artifact a purge removed, written offline before the purge reached the client, arrives
    marked as the purge would have marked it (U2)."""
    if data["kind"] not in ITEM_KINDS:
        return Rejected(Reason.SCHEMA)
    if not all(_is_canonical_text(data.get(column)) for column in TEXT_COLUMNS[Entity.ITEM]):
        return Rejected(Reason.NOT_CANONICAL)
    row = {"id": ref.id, "schema_version": SCHEMA_VERSION, **data}
    forked_from = data["fields"].get("forked_from_artifact") if data["kind"] == "note" else None
    purged = forked_from and purge.purged_at(tx, EntityRef(Entity.ARTIFACT, forked_from))
    return _create_once(tx, ref, data, {**row, "evidence_purged_at": purged} if purged else row)


def _create_once(tx: Tx, ref: EntityRef, data: dict, row: dict) -> Outcome:
    earlier = earlier_create(tx, ref, data)
    if earlier is not None:
        return earlier
    store.write(tx, ref, row)
    remember_create(tx, ref, data)
    return Applied()


def _create_tag(tx: Tx, ref: EntityRef, data: dict) -> Outcome:
    return _create_once(tx, ref, data, {"id": ref.id, **data})


def create_relation(tx: Tx, ref: EntityRef, data: dict) -> Outcome:
    """A Relation is named by its ends, so creating it again adds it back after a delete (D18)."""
    if not (refs.exists(tx, data["from"]) and refs.exists(tx, data["to"])):
        return Rejected(Reason.LINK_TARGET_MISSING)
    if store.get(tx, ref) is None:
        store.write(tx, ref, {"id": ref.id, **data})
    return Applied()


CREATES = {Entity.ITEM: create_item, Entity.TAG: _create_tag, Entity.RELATION: create_relation}


def create(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    ref = EntityRef(Entity(mutation["entity"]), mutation["id"])
    return CREATES[ref.entity](tx, ref, mutation["data"])


def _value(row: dict, key: str) -> object:
    if key.startswith(FIELDS_PREFIX):
        return row["fields"].get(key.removeprefix(FIELDS_PREFIX))
    return row.get(key)


def _without(mapping: dict, name: str) -> dict:
    return {member: value for member, value in mapping.items() if member != name}


def with_value(row: dict, key: str, value: object) -> dict:
    """to: null removes the column (A3)."""
    if key.startswith(FIELDS_PREFIX):
        name = key.removeprefix(FIELDS_PREFIX)
        fields = _without(row["fields"], name)
        return {**row, "fields": fields if value is None else {**fields, name: value}}
    rest = _without(row, key)
    return rest if value is None else {**rest, key: value}


def _holds_from(current: object, change: dict) -> bool:
    """from: null and from_hash: null name an absent column (A3); an x-text column compares by text_hash (E7)."""
    if "from_hash" in change:
        return change["from_hash"] == (None if current is None else text_hash(current))
    return current == change["from"]


def could_be_created(entity: Entity, row: dict, mutation_id: str) -> bool:
    """An Item must stay one a client could create: dates that follow all_day and only the fields its kind defines."""
    if entity is not Entity.ITEM:
        return True
    data = {name: row[name] for name in ITEM_DATA if name in row}
    return not validate("mutation/mutation", {"mutation_id": mutation_id, "op": "create", "entity": "item", "id": row["id"], "data": data})


PATCHABLE = frozenset({Entity.ITEM, Entity.ATTACHMENT, Entity.TAG})


def live(tx: Tx, ref: EntityRef) -> dict | None:
    """The row a patch may change: neither missing nor soft-deleted."""
    row = store.get(tx, ref)
    return None if row is None or "deleted_at" in row else row


BOUND_KEYS = (frozenset({"all_day", "starts_at", "due_at"}),)
"""Keys an Item can hold only together (item.json dates_follow_all_day), so a patch writes or holds back each group whole."""


def _units(keys: Iterable[str]) -> list[tuple[str, ...]]:
    """`keys` as they are written or held back: each BOUND_KEYS group among them, then every other key alone."""
    keys = tuple(keys)
    grouped = [tuple(key for key in keys if key in group) for group in BOUND_KEYS]
    return [unit for unit in grouped if unit] + [(key,) for key in keys if not any(key in group for group in BOUND_KEYS)]


def _with_changes(row: dict, changes: dict[str, dict], keys: Iterable[str]) -> dict:
    for key in keys:
        row = with_value(row, key, changes[key]["to"])
    return row


def patch(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    """A client's patch: compare-and-set on a live entity (_compare_and_set). Every key sent to an entity that is not
    live is kept in the Mutation's ConflictRecord (D24), except for an entity a purge removed: the user destroyed it,
    and its text does not come back (D24 decision 2).
    """
    ref = EntityRef(Entity(mutation["entity"]), mutation["id"])
    if ref.entity not in PATCHABLE:
        return Rejected(Reason.UNSUPPORTED_ENTITY)
    changes: dict[str, dict] = mutation["set"]
    if not _all_canonical(changes):
        return Rejected(Reason.NOT_CANONICAL)
    row = live(tx, ref)
    if row is None and purge.purged_at(tx, ref) is not None:
        return Rejected(Reason.DELETED)
    if row is None:
        return Rejected(Reason.DELETED, conflict_records.keep(tx, mutation["mutation_id"], ref, changes, changes))
    return _compare_and_set(tx, ref, row, mutation["mutation_id"], changes)


def _all_canonical(changes: dict[str, dict]) -> bool:
    return all(_is_canonical_text(change["to"]) for change in changes.values() if "from_hash" in change)


def _compare_and_set(tx: Tx, ref: EntityRef, row: dict, mutation_id: str, changes: dict[str, dict]) -> Outcome:
    """Per-key compare-and-set (D18): a key wins when the row still holds its from, or already holds its to.

    When every key won, a row a client could not create means the client broke the contract: schema. When a key
    lost, it holds back the rest of its BOUND_KEYS group, a group or key that would leave such a row on its own
    is held back too, and the others are written; held-back keys count as conflicted. So beside a key that lost,
    a key that breaks the row on its own shows as a conflict; resent with current froms, it shows as schema.
    Every conflicted key is kept in the ConflictRecord of mutation_id (D24).
    """
    lost = {key for key, change in changes.items() if _value(row, key) != change["to"] and not _holds_from(_value(row, key), change)}
    if lost:
        standing = {
            key
            for unit in _units(changes)
            if lost.isdisjoint(unit) and could_be_created(ref.entity, _with_changes(row, changes, unit), mutation_id)
            for key in unit
        }
    elif could_be_created(ref.entity, _with_changes(row, changes, changes), mutation_id):
        standing = set(changes)
    else:
        return Rejected(Reason.SCHEMA)
    applied = tuple(key for key in changes if key in standing)
    conflicted = tuple(key for key in changes if key not in standing)
    updated = _with_changes(row, changes, applied)
    if updated != row:
        store.write(tx, ref, updated)
    if not conflicted:
        return Applied(applied)
    return Conflict(applied, conflicted, conflict_records.keep(tx, mutation_id, ref, changes, conflicted))


def resolve(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    """conflict.resolve (D24 decision 3): set, when sent, then the record resolved, in one transaction.

    A record that is gone, tombstoned by a purge with its entity included, refuses as deleted. set is a patch of the
    record's entity and entity names it; another entity makes the Mutation malformed. A refused set resolves
    nothing; keys of set that lose are kept in a record of this Mutation's own. A resolved record stays resolved.
    """
    record = store.get(tx, EntityRef(Entity.CONFLICT_RECORD, mutation["conflict_record_id"]))
    if record is None:
        return Rejected(Reason.DELETED)
    ref = conflict_records.target(record)
    if ref.entity.value != mutation["entity"]:
        return Rejected(Reason.SCHEMA)
    outcome = _patch_resolving(tx, ref, mutation) if "set" in mutation else Applied()
    if not isinstance(outcome, Rejected):
        conflict_records.resolve(tx, record, mutation["mutation_id"], device)
    return outcome


def _patch_resolving(tx: Tx, ref: EntityRef, mutation: dict) -> Outcome:
    """A resolve's set, judged in a client's patch order; sent to an entity that is not live it keeps nothing."""
    changes: dict[str, dict] = mutation["set"]
    if not _all_canonical(changes):
        return Rejected(Reason.NOT_CANONICAL)
    row = live(tx, ref)
    if row is None:
        return Rejected(Reason.DELETED)
    return _compare_and_set(tx, ref, row, mutation["mutation_id"], changes)


def _delete_item(tx: Tx, ref: EntityRef) -> Outcome:
    """Soft: the row stays, with the time of the first delete (D18)."""
    row = store.get(tx, ref)
    if row is not None and "deleted_at" not in row:
        store.write(tx, ref, {**row, "deleted_at": now_instant()})
    return Applied()


def _delete_relation(tx: Tx, ref: EntityRef) -> Outcome:
    if store.get(tx, ref) is not None:
        store.write(tx, ref, None)
    return Applied()


def _delete_tag(tx: Tx, ref: EntityRef) -> Outcome:
    """The Relations to or from the Tag go with it, so none is left pointing at a tombstone."""
    if store.get(tx, ref) is None:
        return Applied()
    tag = f"tag:{ref.id}"
    ends = tx.execute(
        "SELECT id FROM relations WHERE json_extract(data, '$.from') = ? OR json_extract(data, '$.to') = ? ORDER BY id", (tag, tag)
    ).fetchall()
    for (relation_id,) in ends:
        store.write(tx, EntityRef(Entity.RELATION, relation_id), None)
    store.write(tx, ref, None)
    return Applied()


def _restore_item(tx: Tx, ref: EntityRef) -> Outcome:
    row = store.get(tx, ref)
    if row is None:
        return Rejected(Reason.DELETED)
    if "deleted_at" in row:
        store.write(tx, ref, {name: value for name, value in row.items() if name != "deleted_at"})
    return Applied()


DELETES = {Entity.ITEM: _delete_item, Entity.RELATION: _delete_relation, Entity.TAG: _delete_tag}
RESTORES = {Entity.ITEM: _restore_item}


def delete(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    """Where delete applies, it is applied whether or not anything was left to delete: the entity ends up gone."""
    ref = EntityRef(Entity(mutation["entity"]), mutation["id"])
    handler = DELETES.get(ref.entity)
    return Rejected(Reason.UNSUPPORTED_ENTITY) if handler is None else handler(tx, ref)


def restore(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    ref = EntityRef(Entity(mutation["entity"]), mutation["id"])
    handler = RESTORES.get(ref.entity)
    return Rejected(Reason.UNSUPPORTED_ENTITY) if handler is None else handler(tx, ref)
