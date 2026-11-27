"""What search results belong to, worked out from a database snapshot when a query is filtered (#11 decision 4)."""

from __future__ import annotations

from ..db import Snapshot, Tx
from ..store import Entity, EntityRef

SNAPSHOT_ARTIFACT_TYPE = "web_snapshot"


def holders(runner: Tx | Snapshot, sha256: str) -> set[EntityRef]:
    """What holds the Blob, through which the blocks extracted from it belong to a course or tag: every Attachment
    naming it, and the Capture a web_snapshot Artifact holding it is about, as a link snapshot (#63) has no Attachment."""
    attachments = runner.execute("SELECT id FROM attachments WHERE blob_sha256 = ?", (sha256,)).fetchall()
    snapshots = runner.execute(
        "SELECT json_extract(data, '$.subject_ref') FROM artifacts WHERE content_blob = ? AND artifact_type = ?", (sha256, SNAPSHOT_ARTIFACT_TYPE)
    ).fetchall()
    return {EntityRef(Entity.ATTACHMENT, identifier) for (identifier,) in attachments} | {
        ref for (subject,) in snapshots if (ref := EntityRef.parse(subject)) is not None and ref.entity is Entity.CAPTURE
    }
