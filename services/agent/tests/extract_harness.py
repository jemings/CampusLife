"""A Hub with the file pipeline in a test: its database, Blob store, registry and runner, and what to read back."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from campuslife_contracts import validate
from test_capture_commit import apply, commit, new_id, store_bytes

from campuslife_hub import jobs, media, store
from campuslife_hub.blobs import BlobStore
from campuslife_hub.data_root import Layout
from campuslife_hub.db import Database
from campuslife_hub.devices import ensure_hub_device
from campuslife_hub.extract.extractors import ALL
from campuslife_hub.extract.limits import IDENTIFY_LIMITS, Limits
from campuslife_hub.identity import ensure_identity
from campuslife_hub.job_runner import JobContext, JobRunner
from campuslife_hub.jobs import JobSpec
from campuslife_hub.lock import HubLock
from campuslife_hub.pipelines import build_registry
from campuslife_hub.store import Entity, EntityRef

TEST_LIMITS = Limits(timeout_s=30.0, mem_bytes=512 << 20)


@dataclass
class Clock:
    """The time now, moved on by ahead."""

    ahead: timedelta = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.ahead


@dataclass
class Hub:
    db: object
    blobs: object
    device: object
    tmp_path: object
    registry: object
    clock: Clock
    runner: JobRunner

    @property
    def files(self):
        """The file pipeline's Files, whose plan the registry's planner calls."""
        return dict(self.registry.planners)["files"].__self__

    async def capture(self, *files, role="file"):
        """Commit a Capture of files, each (name, bytes or a file's Path) or (name, bytes, declared MIME); the committed Capture."""
        attachments = []
        for name, data, *declared in files:
            sha256 = (await self.blobs.put_file(data)).sha256 if isinstance(data, Path) else await store_bytes(self.blobs, self.tmp_path, data)
            attachment = {"id": new_id(), "blob_sha256": sha256, "role": role, "cloud_policy": "local_only"}
            if name is not None:
                attachment["original_filename"] = name
            if declared:
                attachment["declared_mime"] = declared[0]
            if role == "audio_segment":
                attachment["seq"] = len(attachments)
            attachments.append(attachment)
        capture = {"id": new_id(), "created_at": "2026-03-02T00:30:00.000Z", "origin": {"type": "import"}, "links": [], "attachments": attachments}
        [result] = await apply(self.db, self.device, commit(capture))
        assert result["result"] == "applied"
        return capture

    async def settle(self, *, retries=0):
        """Run every due job, then, retries times, move the clock past the retry delays and run again."""
        await self.runner.run_due()
        for _ in range(retries):
            self.clock.ahead += timedelta(hours=2)
            await self.runner.run_due()

    async def store(self, data):
        return await store_bytes(self.blobs, self.tmp_path, data)

    async def enqueue(self, job_type, sha256, params=None):
        spec = JobSpec(job_type, f"blob:{sha256}", (sha256,), params or {})
        return await self.db.write(lambda tx: self.registry.enqueue(tx, spec, now=self.clock()))

    async def jobs(self, job_type=None):
        rows = await self.db.read(lambda snap: [jobs.get(snap, row[0]) for row in snap.execute("SELECT id FROM processing_jobs ORDER BY rowid")])
        return [job for job in rows if job_type is None or job.job_type == job_type]

    async def job(self, job_id):
        return await self.db.read(lambda snap: jobs.get(snap, job_id))

    async def artifacts(self, artifact_type=None):
        rows = await self.db.read(lambda snap: [json.loads(data) for (data,) in snap.execute("SELECT data FROM artifacts ORDER BY rowid")])
        return [row for row in rows if artifact_type is None or row["artifact_type"] == artifact_type]

    async def body(self, artifact):
        _, handle = await self.blobs.open_blob(artifact["content_blob"])
        with handle:
            data = handle.read()
        return json.loads(data), data

    async def attachment(self, attachment_id):
        return await self.db.read(lambda snap: store.get(snap, EntityRef(Entity.ATTACHMENT, attachment_id)))

    async def media(self, sha256):
        return await self.db.read(lambda snap: media.get(snap, sha256))


def hub_with(db, blob_store, device, tmp_path, *extractors, limits=TEST_LIMITS, base=ALL):
    """A Hub reading with base and extractors; limits, unless None, is the table of identify and every reader alike,
    and with None each keeps its own."""
    readers = (*base, *extractors)
    if limits is not None:
        readers = tuple(replace(reader, limits=limits) for reader in readers)
    registry = build_registry(readers, IDENTIFY_LIMITS if limits is None else limits)
    clock = Clock()
    runner = JobRunner(JobContext(db, blob_store, registry, tmp_path / "work"), clock=clock)
    return Hub(db, blob_store, device, tmp_path, registry, clock, runner)


def valid_body(schema_id, body):
    return validate(schema_id, body) == []


@asynccontextmanager
async def fresh_hub(root, *extractors, limits=TEST_LIMITS):
    """Another Hub, with its own database and Blob store under root."""
    layout = Layout(root)
    root.mkdir(parents=True)
    with HubLock.acquire(layout.lock_file) as lock:
        database = await Database.open(layout, lock=lock, create=True)
        try:
            await database.write(ensure_identity)
            device = await database.write(ensure_hub_device)
            blob_store = BlobStore(database, blobs_dir=root / "blobs", staging_dir=root / "staging", uploads_dir=root / "uploads")
            yield hub_with(database, blob_store, device, root, *extractors, limits=limits)
        finally:
            await database.close()
