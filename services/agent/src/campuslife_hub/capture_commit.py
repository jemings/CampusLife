"""capture.commit (D7, E7, A5, E14): a sealed Capture, its Attachments, the Relations its links make
and its capture.ingest job.

All checks run before the first write, so a commit either writes everything in
its transaction or nothing.
"""

from __future__ import annotations

from . import jobs, refs, store
from .blobs import has_blob
from .created import earlier_create, remember_create
from .db import Tx
from .devices import DeviceId
from .ingest import INGEST_PIPELINE, ingest_spec
from .outcomes import Applied, Outcome, Reason, Rejected, Retry
from .store import Entity, EntityRef

COMMIT_ONLY_MEMBERS = ("links", "attachments")


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
    earlier = earlier_create(tx, capture_ref, capture)
    if earlier is not None:
        return earlier
    attachment_ids = [attachment["id"] for attachment in capture["attachments"]]
    if len(set(attachment_ids)) != len(attachment_ids):
        return Rejected(Reason.SCHEMA)
    if any(store.get(tx, EntityRef(Entity.ATTACHMENT, attachment_id)) is not None for attachment_id in attachment_ids):
        return Rejected(Reason.ID_REUSE)
    missing = sorted({sha for sha in (attachment["blob_sha256"] for attachment in capture["attachments"]) if not has_blob(tx, sha)})
    if missing:
        return Retry(tuple(missing))
    if not all(refs.exists(tx, link["to"]) for link in capture["links"]):
        return Rejected(Reason.LINK_TARGET_MISSING)

    row = {name: value for name, value in capture.items() if name not in COMMIT_ONLY_MEMBERS}
    store.write(tx, capture_ref, {**row, "device_id": device})
    for attachment in capture["attachments"]:
        store.write(tx, EntityRef(Entity.ATTACHMENT, attachment["id"]), {**attachment, "capture_id": capture["id"]})
    for relation_id, relation in _relations(capture).items():
        store.write(tx, EntityRef(Entity.RELATION, relation_id), relation)
    jobs.enqueue(tx, ingest_spec(capture), INGEST_PIPELINE)
    remember_create(tx, capture_ref, capture)
    return Applied()
