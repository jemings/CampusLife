"""Entity references, "<entity>:<id>" (E14), and whether the Hub holds what one names."""

from __future__ import annotations

from . import store
from .blobs import has_blob
from .db import Snapshot, Tx
from .store import Entity, EntityRef


def exists(runner: Tx | Snapshot, entity_ref: str) -> bool:
    """A soft-deleted Item exists; a tombstone does not, nor does an entity this Hub does not store yet."""
    entity, _, identifier = entity_ref.partition(":")
    if entity == "blob":
        return has_blob(runner, identifier)
    try:
        stored = Entity(entity)
    except ValueError:
        return False
    return store.get(runner, EntityRef(stored, identifier)) is not None
