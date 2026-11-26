"""Submitted values the Hub did not write and must not lose (D18, E8), kept until #18 turns them into ConflictRecords.

Nothing here goes through store.write: the stash is not in the changes feed, and no
result names a stash row (#18 adds conflict_record_ids).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from .clock import now_instant
from .db import Snapshot, Tx
from .devices import DeviceId
from .store import EntityRef


@dataclass(frozen=True)
class StashedValue:
    id: str
    mutation_id: str
    device_id: str
    entity: str
    entity_id: str
    field: str
    change: dict
    theirs_rev: int | None
    """The rev of the row the value lost to, or None when the Hub never had it."""
    stashed_at: str


def keep(tx: Tx, mutation: dict, device: DeviceId, ref: EntityRef, keys: Iterable[str]) -> None:
    rev = tx.execute("SELECT rev FROM sync_index WHERE entity = ? AND id = ?", (ref.entity.value, ref.id)).fetchone()
    for key in keys:
        tx.execute(
            "INSERT INTO conflict_stash (id, mutation_id, device_id, entity, entity_id, field, change, theirs_rev, stashed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), mutation["mutation_id"], device, ref.entity.value, ref.id, key,
                json.dumps(mutation["set"][key], ensure_ascii=False), None if rev is None else rev[0], now_instant(),
            ),
        )


def kept(runner: Tx | Snapshot) -> list[StashedValue]:
    rows = runner.execute(
        "SELECT id, mutation_id, device_id, entity, entity_id, field, change, theirs_rev, stashed_at FROM conflict_stash ORDER BY rowid"
    ).fetchall()
    return [StashedValue(*row[:6], json.loads(row[6]), *row[7:]) for row in rows]
