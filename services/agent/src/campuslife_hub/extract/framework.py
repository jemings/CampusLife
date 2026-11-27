"""The file pipeline (decision 2, PR2 decision on identify's input): identify each Blob once, then extract it with
each reader refine picks for an Attachment.

plan is the one planner, and it converges: it looks at an Attachment as the Hub holds it now and returns only the
next job it lacks. A Blob not yet identified by the current identify pipeline gets identify; an identified one
gets its Attachment's detected_mime stored and an extract job for each reader of it, or nothing. capture.ingest
asks plan for each Attachment of a Capture, and the end of identify asks it again for every Attachment of the
Blob, so a Capture that comes before, during or after the Blob's identify reaches the same jobs. Jobs carry no
Attachment: the same Blob read by the same extractor with the same params is one job (D2).

Both handlers run the child through running.run_child in work/<job id>/, which they empty before and after. The
Hub builds and validates the body (bodies) off the event loop, as the child's work is, and stores it as a Blob of its
own, staged in the Blob store and never in the child's directory; that Blob is the content of the job's Artifact.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING

from .. import artifacts, media, store
from ..db import Tx
from ..ingest import CaptureView, enqueue_held
from ..jobs import Failed, Finished, Job, JobSpec, OutcomeCode, Pipeline, Succeeded
from ..store import Entity, EntityRef
from . import bodies, sniff
from .extractors import Extractor
from .formats import refine
from .limits import Limits
from .running import Answer, run_child, unsafe

if TYPE_CHECKING:
    from ..job_runner import JobContext, JobRegistry

IDENTIFY = "identify"
IDENTIFIER_ID = "identify"
PLANNED_ROLES = frozenset({"file", "text", "web_snapshot"})
"""S2: audio_segment and photo are #8's."""
PLANNER = "files"
HANDLER_SLACK = timedelta(seconds=60)
"""S5: a handler's time is the child's and this, for reading the Blob and storing the body."""


def identify_spec(sha256: str) -> JobSpec:
    return JobSpec(IDENTIFY, f"blob:{sha256}", (sha256,))


class Files:
    """The identify and extract handlers and the planner, for one set of extractors under one limits table."""

    def __init__(self, extractors: Sequence[Extractor], limits: Limits):
        types = [extractor.job_type for extractor in extractors]
        if len(set(types)) != len(types):
            raise ValueError(f"two extractors share a job type: {types}")
        self.extractors = {extractor.job_type: extractor for extractor in extractors}
        self.readers = tuple(extractor.reader() for extractor in extractors)
        self.limits = limits
        self.identify_pipeline = Pipeline(f"{IDENTIFIER_ID}-{sniff.VERSION}+L{limits.hash8()}")

    def pipeline(self, extractor: Extractor) -> Pipeline:
        return Pipeline(f"{extractor.id}-{extractor.version}+L{self.limits.hash8()}")

    def register(self, registry: JobRegistry) -> None:
        timeout = timedelta(seconds=self.limits.timeout_s) + HANDLER_SLACK
        registry.handle(IDENTIFY, self.identify_pipeline, self.identify, timeout=timeout)
        for extractor in self.extractors.values():
            registry.handle(extractor.job_type, self.pipeline(extractor), self.extract, timeout=timeout)
        registry.plan(PLANNER, self.planner)

    def plan(self, tx: Tx, attachment_id: str) -> list[JobSpec]:
        attachment = store.get(tx, EntityRef(Entity.ATTACHMENT, attachment_id))
        if attachment is None or attachment["role"] not in PLANNED_ROLES:
            return []
        sha256 = attachment["blob_sha256"]
        found = media.get(tx, sha256)
        if found is None or found.pipeline_version != self.identify_pipeline.version:
            return [identify_spec(sha256)]
        refined = refine(found.media_type, attachment.get("original_filename"), attachment.get("declared_mime"), self.readers)
        media.set_detected_mime(tx, attachment, refined.detected_mime)
        return [JobSpec(job_type, f"blob:{sha256}", (sha256,), dict(params)) for job_type, params in refined.jobs]

    async def planner(self, view: CaptureView, context: JobContext) -> list[JobSpec]:
        ids = [attachment["id"] for attachment in view.attachments]
        return await context.db.write(lambda tx: [spec for attachment_id in ids for spec in self.plan(tx, attachment_id)])

    def _plan_blob(self, tx: Tx, registry: JobRegistry, sha256: str) -> None:
        ids = [identifier for (identifier,) in tx.execute("SELECT id FROM attachments WHERE blob_sha256 = ? ORDER BY rowid", (sha256,))]
        enqueue_held(tx, registry, [(PLANNER, spec) for attachment_id in ids for spec in self.plan(tx, attachment_id)])

    async def _read(self, job: Job, context: JobContext, request: dict, made: Callable[[str, dict], tuple[bytes, Callable[[Tx, str], object]]]) -> Finished:
        """Run the child on the job's Blob and record what made builds from its answer."""
        sha256 = job.inputs[0]
        size = await context.db.read(lambda snap: snap.execute("SELECT size FROM blobs WHERE sha256 = ?", (sha256,)).fetchone())
        if size is None:
            raise FileNotFoundError(f"the Hub holds no Blob {sha256}")
        if size[0] > self.limits.input_max_bytes:
            return unsafe(f"the Blob holds {size[0]} bytes, more than {self.limits.input_max_bytes}")
        path = context.blobs.path_of(sha256)
        if not path.is_file():
            raise FileNotFoundError(f"the file of Blob {sha256} is missing")
        work = context.work_dir(job)
        shutil.rmtree(work, ignore_errors=True)
        try:
            judged = await run_child(job, {**request, "path": str(path)}, self.limits, work)
            if not isinstance(judged, Answer):
                return judged
            try:
                stored, record = await asyncio.to_thread(made, sha256, judged.result)
            except bodies.TooLarge as error:
                return unsafe(str(error))
            except bodies.Rejected as error:
                return Failed(f"the child's result breaks the contract: {error}")
            content = await context.blobs.put_bytes(stored)
            await context.db.write(lambda tx: record(tx, content.sha256))
            return Succeeded(OutcomeCode.PARTIAL if judged.partial else None)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def identify(self, job: Job, context: JobContext) -> Finished:
        identifier = {"id": IDENTIFIER_ID, "version": sniff.VERSION}

        def made(sha256: str, result: dict):
            media_type, stored = bodies.identification(sha256, identifier, result, self.limits)

            def record(tx: Tx, content: str) -> None:
                artifact = artifacts.record(tx, job, "identify", content)
                media.put(tx, media.Media(sha256, media_type, job.pipeline.version, artifact["id"]))
                self._plan_blob(tx, context.registry, sha256)

            return stored, record

        return await self._read(job, context, {"op": "identify"}, made)

    async def extract(self, job: Job, context: JobContext) -> Finished:
        extractor = self.extractors[job.job_type]
        named = {"id": extractor.id, "version": extractor.version, "params": dict(job.params)}

        def made(sha256: str, result: dict):
            stored = bodies.extraction(sha256, named, result, self.limits)
            return stored, lambda tx, content: artifacts.record(tx, job, "extraction", content)

        return await self._read(job, context, {"op": "extract", "entry": extractor.entry, "params": job.params}, made)

