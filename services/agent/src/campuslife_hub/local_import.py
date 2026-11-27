"""Local import (D7, E15): files dropped on the Hub become a capture.commit from its own Device.

It builds the same payload a client would and calls the same `apply()`, so
validation, idempotency and the feed behave exactly as for a synced Capture.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from campuslife_contracts import effective_cloud_policy, resolve_cloud_policy

from . import mutations, store
from .clock import now_instant
from .core import Core
from .db import Snapshot
from .store import Entity, EntityRef

GLOBAL_DEFAULT_CLOUD_POLICY = "local_only"


class UnimportableFile(ValueError):
    pass


@dataclass(frozen=True)
class ImportResult:
    capture_id: str
    result: dict


def _linked_item(snap: Snapshot, entity_ref: str) -> dict | None:
    entity, _, identifier = entity_ref.partition(":")
    return store.get(snap, EntityRef(Entity(entity), identifier)) if entity == Entity.ITEM else None


def _course_of(snap: Snapshot, item: dict | None) -> dict | None:
    if item is not None and item.get("kind") == "lecture_session":
        return store.get(snap, EntityRef(Entity.ITEM, item["fields"]["course_id"]))
    return item if item is not None and item.get("kind") == "course" else None


def course_default_policy(snap: Snapshot, links: Iterable[str]) -> str | None:
    """default_cloud_policy of the courses the links reach, the strictest when they differ (D8)."""
    defaults = []
    for link in links:
        course = _course_of(snap, _linked_item(snap, link))
        if course is not None and "default_cloud_policy" in course["fields"]:
            defaults.append(course["fields"]["default_cloud_policy"])
    return effective_cloud_policy(defaults) if defaults else None


def _refuse_unimportable(path: Path, name: str) -> None:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise UnimportableFile(f"{name!r}: the file name is not valid Unicode; rename the file and import it again") from None
    if not name:
        raise UnimportableFile(f"{path}: the file has no name")
    if not path.is_file():
        raise UnimportableFile(f"{path}: not a regular file")


def _file_name_members(received: str) -> dict:
    normalized = unicodedata.normalize("NFC", received)
    members = {"original_filename": normalized}
    if received != normalized:
        members["original_filename_raw"] = received
    return members


async def import_files(
    core: Core,
    paths: Sequence[Path],
    *,
    names: Sequence[str] | None = None,
    links: Sequence[str] = (),
    cloud_policy: str | None = None,
    global_default: str = GLOBAL_DEFAULT_CLOUD_POLICY,
) -> ImportResult:
    """Store the files as Blobs and commit them as one Capture, each under its name in names, by default its file name.

    Every file is checked before the first Blob is stored, so a refused import leaves no Blob behind.
    """
    paths = [Path(path) for path in paths]
    names = [path.name for path in paths] if names is None else list(names)
    if len(names) != len(paths):
        raise UnimportableFile(f"{len(names)} names for {len(paths)} files")
    for path, name in zip(paths, names, strict=True):
        _refuse_unimportable(path, name)
    stored = [await core.blobs.put_file(path) for path in paths]
    course_default = await core.db.read(lambda snap: course_default_policy(snap, links))
    policy = resolve_cloud_policy(cloud_policy, course_default, global_default)
    capture_id = str(uuid.uuid4())
    capture = {
        "id": capture_id,
        "created_at": now_instant(),
        "origin": {"type": "import"},
        "links": [{"type": "about", "to": link} for link in links],
        "attachments": [
            {"id": str(uuid.uuid4()), "blob_sha256": blob.sha256, "role": "file", **_file_name_members(name), "cloud_policy": policy}
            for name, blob in zip(names, stored, strict=True)
        ],
    }
    mutation = {"mutation_id": str(uuid.uuid4()), "op": "capture.commit", "capture": capture}
    [result] = await mutations.apply(core.db, [mutation], core.hub_device)
    return ImportResult(capture_id, result)
