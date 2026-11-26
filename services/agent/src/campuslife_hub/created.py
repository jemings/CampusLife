"""Which payload first created an entity id (E7).

capture.commit and create are idempotent per entity id: the canonical_json
sha256 of the first payload is kept, so a resend under another mutation_id
compares with what was created, not with the row a later patch changed.
"""

from __future__ import annotations

import hashlib

from campuslife_contracts import canonical_json

from .db import Tx
from .outcomes import Applied, Outcome, Reason, Rejected
from .store import EntityRef


def _content_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def earlier_create(tx: Tx, ref: EntityRef, payload: dict) -> Outcome | None:
    """applied when the id was created from this payload, id_reuse when from another, None when it is new."""
    row = tx.execute("SELECT content_sha256 FROM created_content WHERE entity = ? AND id = ?", (ref.entity.value, ref.id)).fetchone()
    if row is None:
        return None
    return Applied() if row[0] == _content_sha256(payload) else Rejected(Reason.ID_REUSE)


def remember_create(tx: Tx, ref: EntityRef, payload: dict) -> None:
    tx.execute(
        "INSERT INTO created_content (entity, id, content_sha256) VALUES (?, ?, ?)",
        (ref.entity.value, ref.id, _content_sha256(payload)),
    )
