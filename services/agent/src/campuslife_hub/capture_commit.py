"""capture.commit (D7, E7, A5): a sealed Capture, its Attachments and the Relations its links make.

All checks run before the first write, so a commit either writes everything in
its transaction or nothing.
"""

from __future__ import annotations

import hashlib

from campuslife_contracts import canonical_json

from . import store
from .blobs import has_blob
from .db import Tx
from .devices import DeviceId
from .outcomes import Applied, Outcome, Reason, Rejected, Retry
from .store import Entity, EntityRef

COMMIT_ONLY_MEMBERS = ("links", "attachments")


def _content_sha256(capture: dict) -> str:
    return hashlib.sha256(canonical_json(capture)).hexdigest()


def _first_content(tx: Tx, ref: EntityRef) -> str | None:
    row = tx.execute("SELECT content_sha256 FROM created_content WHERE entity = ? AND id = ?", (ref.entity.value, ref.id)).fetchone()
    return None if row is None else row[0]


def _is_live(tx: Tx, entity_ref: str) -> bool:
    entity, _, identifier = entity_ref.partition(":")
    return store.get(tx, EntityRef(Entity(entity), identifier)) is not None


def _relations(capture: dict) -> dict[str, dict]:
    sources = [f"attachment:{attachment['id']}" for attachment in capture["attachments"]] or [f"capture:{capture['id']}"]
    relations = {}
    for link in capture["links"]:
        for source in sources:
            relation_id = f"{source}|{link['type']}|{link['to']}"
            relations[relation_id] = {"id": relation_id, "from": source, "type": link["type"], "to": link["to"]}
    return relations


def commit_capture(tx: Tx, capture: dict, device: DeviceId) -> Outcome:
    capture_ref = EntityRef(Entity.CAPTURE, capture["id"])
    content = _content_sha256(capture)
    first = _first_content(tx, capture_ref)
    if first is not None:
        return Applied() if first == content else Rejected(Reason.ID_REUSE)
    attachment_ids = [attachment["id"] for attachment in capture["attachments"]]
    if len(set(attachment_ids)) != len(attachment_ids):
        return Rejected(Reason.SCHEMA)
    if any(store.get(tx, EntityRef(Entity.ATTACHMENT, attachment_id)) is not None for attachment_id in attachment_ids):
        return Rejected(Reason.ID_REUSE)
    missing = sorted({sha for sha in (attachment["blob_sha256"] for attachment in capture["attachments"]) if not has_blob(tx, sha)})
    if missing:
        return Retry(tuple(missing))
    if not all(_is_live(tx, link["to"]) for link in capture["links"]):
        return Rejected(Reason.LINK_TARGET_MISSING)

    row = {name: value for name, value in capture.items() if name not in COMMIT_ONLY_MEMBERS}
    store.write(tx, capture_ref, {**row, "device_id": device})
    for attachment in capture["attachments"]:
        store.write(tx, EntityRef(Entity.ATTACHMENT, attachment["id"]), {**attachment, "capture_id": capture["id"]})
    for relation_id, relation in _relations(capture).items():
        store.write(tx, EntityRef(Entity.RELATION, relation_id), relation)
    tx.execute(
        "INSERT INTO created_content (entity, id, content_sha256) VALUES (?, ?, ?)",
        (capture_ref.entity.value, capture_ref.id, content),
    )
    return Applied()
