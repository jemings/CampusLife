"""Media types (decision 7): blob_media, what identify read from each Blob's bytes, and the detected_mime of an
Attachment refined from it.

Only this module writes either. blob_media is filled when an identify job ends and leaves with its Blob's row
(migration 0009). detected_mime is the Hub's member of an Attachment and holds refine's value: it is written by
planning, through set_detected_mime, whenever the value it computes differs from the stored one.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import store
from .db import Snapshot, Tx
from .store import Entity, EntityRef


@dataclass(frozen=True)
class Media:
    sha256: str
    media_type: str
    pipeline_version: str
    """The identify pipeline that read it; another one reads the Blob again when it is next planned."""
    artifact_id: str


def get(runner: Tx | Snapshot, sha256: str) -> Media | None:
    row = runner.execute("SELECT sha256, media_type, pipeline_version, artifact_id FROM blob_media WHERE sha256 = ?", (sha256,)).fetchone()
    return None if row is None else Media(*row)


def put(tx: Tx, media: Media) -> None:
    tx.execute(
        "INSERT INTO blob_media (sha256, media_type, pipeline_version, artifact_id) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (sha256) DO UPDATE SET media_type = excluded.media_type, pipeline_version = excluded.pipeline_version,"
        " artifact_id = excluded.artifact_id",
        (media.sha256, media.media_type, media.pipeline_version, media.artifact_id),
    )


def set_detected_mime(tx: Tx, attachment: dict, detected_mime: str) -> None:
    """Store detected_mime on the Attachment when it differs; the feed sends the Attachment out again then."""
    if attachment.get("detected_mime") != detected_mime:
        store.write(tx, EntityRef(Entity.ATTACHMENT, attachment["id"]), {**attachment, "detected_mime": detected_mime})
