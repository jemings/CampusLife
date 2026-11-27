"""Entity references, "<entity>:<id>" (E14), and whether the Hub holds, or once held, what one names."""

from __future__ import annotations

from . import store
from .blobs import has_blob
from .db import Snapshot, Tx
from .store import EntityRef


def exists(runner: Tx | Snapshot, entity_ref: str) -> bool:
    """A soft-deleted Item exists; a tombstone does not, nor does an entity this Hub does not store yet."""
    entity, _, identifier = entity_ref.partition(":")
    if entity == "blob":
        return has_blob(runner, identifier)
    ref = EntityRef.parse(entity_ref)
    return ref is not None and store.get(runner, ref) is not None


def ever_existed(runner: Tx | Snapshot, entity_ref: str) -> bool:
    """What exists, a tombstone, and a Blob a purge removed; not what the Hub never held."""
    entity, _, identifier = entity_ref.partition(":")
    if entity == "blob":
        purged = runner.execute(
            "SELECT 1 FROM purge_journal, json_each(purge_journal.target, '$.blobs') WHERE json_each.value = ?", (identifier,)
        ).fetchone()
        return has_blob(runner, identifier) or purged is not None
    ref = EntityRef.parse(entity_ref)
    return ref is not None and store.ever_written(runner, ref)
