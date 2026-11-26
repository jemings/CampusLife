"""The changes feed body (D3, E18): current state of every row above a cursor."""

from __future__ import annotations

from .db import Snapshot
from .identity import read_identity
from .store import Entity, EntityRef, get


def page(snap: Snapshot, since: int, limit: int) -> dict:
    identity = read_identity(snap)
    rows = snap.execute(
        "SELECT entity, id, rev, deleted FROM sync_index WHERE rev > ? ORDER BY rev LIMIT ?",
        (since, limit + 1),
    ).fetchall()
    changes = []
    for entity, identifier, rev, deleted in rows[:limit]:
        change = {"entity": entity, "id": identifier, "rev": rev, "deleted": bool(deleted)}
        if not deleted:
            change["data"] = get(snap, EntityRef(Entity(entity), identifier))
        changes.append(change)
    return {
        "hub_id": identity.hub_id,
        "epoch": identity.epoch,
        "changes": changes,
        "next_since": changes[-1]["rev"] if changes else since,
        "has_more": len(rows) > limit,
    }
