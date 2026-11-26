"""The changes-feed storage layer (D3, E5).

`write` is the only code that advances `change_seq` and touches `sync_index`.
Every synced write goes through it: the Mutation applier, backups
(`backed_up_at`) and jobs (Artifact, Suggestion). Each call takes its own rev,
so rows written in one transaction still get distinct revs and a changes page
boundary never falls between rows of equal rev.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass

from campuslife_contracts import canonical_json

from .db import Snapshot, Tx


class Entity(enum.StrEnum):
    CAPTURE = "capture"
    ATTACHMENT = "attachment"
    ITEM = "item"
    RELATION = "relation"


TABLES: dict[Entity, str] = {
    Entity.CAPTURE: "captures",
    Entity.ATTACHMENT: "attachments",
    Entity.ITEM: "items",
    Entity.RELATION: "relations",
}


ROW_CARRIES_REV = frozenset({Entity.ITEM})
"""Entities whose contract row holds the rev of its last write (item/item `rev`)."""


@dataclass(frozen=True)
class EntityRef:
    entity: Entity
    id: str


def _next_rev(tx: Tx) -> int:
    (rev,) = tx.execute("UPDATE hub_state SET change_seq = change_seq + 1 RETURNING change_seq").fetchone()
    return rev


def write(tx: Tx, ref: EntityRef, data: dict | None) -> int:
    """Store the row's current state, or a tombstone when data is None, and return its new rev."""
    rev = _next_rev(tx)
    table = TABLES[ref.entity]
    if data is None:
        tx.execute(f"DELETE FROM {table} WHERE id = ?", (ref.id,))
    else:
        row = {**data, "rev": rev} if ref.entity in ROW_CARRIES_REV else data
        tx.execute(
            f"INSERT INTO {table} (id, data) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET data = excluded.data",
            (ref.id, canonical_json(row).decode("utf-8")),
        )
    tx.execute(
        "INSERT INTO sync_index (entity, id, rev, deleted) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (entity, id) DO UPDATE SET rev = excluded.rev, deleted = excluded.deleted",
        (ref.entity.value, ref.id, rev, data is None),
    )
    return rev


def get(runner: Tx | Snapshot, ref: EntityRef) -> dict | None:
    row = runner.execute(f"SELECT data FROM {TABLES[ref.entity]} WHERE id = ?", (ref.id,)).fetchone()
    return None if row is None else json.loads(row[0])
