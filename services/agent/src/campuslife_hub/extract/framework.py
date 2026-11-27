"""The file pipeline (decision 2, PR2 decision on identify's input): identify each Blob once, then extract or render
it with each reader refine picks for an Attachment.

plan is the one planner, and it converges: it looks at an Attachment as the Hub holds it now and returns only the
next job it lacks. A Blob not yet identified by the current identify pipeline gets identify; an identified one
gets its Attachment's detected_mime stored and an extract job for each reader of it, or nothing. capture.ingest
asks plan for each Attachment of a Capture, and the end of identify asks it again for every Attachment of the
Blob, so a Capture that comes before, during or after the Blob's identify reaches the same jobs. Jobs carry no
Attachment: the same Blob read by the same extractor with the same params is one job (D2).

A reprocess's nonce goes into identify's params and every extract and render job's, so each runs again; plan treats a
Blob as identified for the reprocess only once identify ran under its nonce. The nonce never reaches an extractor or
a body, so the block ids stay those of the same Blob, extractor, version and params.

Both handlers run the child through running.run_child in work/<job id>/, which they empty before and after. The
Hub builds and validates the body (bodies) off the event loop, as the child's work is, and stores it as a Blob of its
own, staged in the Blob store and never in the child's directory; that Blob is the content of the job's Artifact. A
converter instead writes a PDF in its directory, which the Hub checks and stores as it is: that PDF is the content of
the rendition Artifact, and no Relation is written (D1; D26).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .. import artifacts, jobs, media, store
from ..db import Snapshot, Tx
from ..ingest import REPROCESS, CaptureView, FileStatus, enqueue_held
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
RULES_CHANGED = "the Hub's processing rules changed since this file was read; reprocess it to read it under the new ones"


def can_read(job_type: str) -> str:
    return f"this Hub can now read it with {job_type}; reprocess it to read it"
HANDLER_SLACK = timedelta(seconds=60)
"""S5: a handler's time is the child's and this, for reading the Blob and storing the body."""


RENDITION = "rendition.pdf"
"""The file a converter writes in its work directory."""
PDF_MAGIC = b"%PDF-"


def _nonce(reprocess: str | None) -> dict[str, str]:
    return {} if reprocess is None else {REPROCESS: reprocess}


def reading(params: Mapping[str, object]) -> dict[str, object]:
    """A job's params as its extractor or converter takes them: without the reprocess nonce, which only makes the
    job's key new. The body and its block ids are made of these, so a reprocess keeps the block ids."""
    return {name: value for name, value in params.items() if name != REPROCESS}


def identify_spec(sha256: str, reprocess: str | None = None) -> JobSpec:
    return JobSpec(IDENTIFY, f"blob:{sha256}", (sha256,), _nonce(reprocess))


class Files:
    """The identify and extract handlers and the planner, for one set of extractors under one limits table."""

    def __init__(self, extractors: Sequence[Extractor], limits: Limits):
        types = [extractor.job_type for extractor in extractors]
        if len(set(types)) != len(types):
            raise ValueError(f"two extractors share a job type: {types}")
        for extractor in extractors:
            if any(REPROCESS in params for params in extractor.accepts.values()):
                raise ValueError(f"{extractor.job_type} takes a param named {REPROCESS}, which a reprocess uses for its nonce")
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
            handler = self.render if extractor.kind == "render" else self.extract
            registry.handle(extractor.job_type, self.pipeline(extractor), handler, timeout=timeout)
        registry.plan(PLANNER, self.planner, status=self.status)

    def _identified(self, runner: Tx | Snapshot, sha256: str, reprocess: str | None) -> media.Media | None:
        """What identify read from the Blob, when the current identify pipeline read it, and, for a reprocess, read it
        again under the reprocess's nonce; None while identify is still to run."""
        found = media.get(runner, sha256)
        if found is None or found.pipeline_version != self.identify_pipeline.version:
            return None
        if reprocess is not None and artifacts.made_by(runner, jobs.job_key(identify_spec(sha256, reprocess), self.identify_pipeline), IDENTIFY) is None:
            return None
        return found

    def plan(self, tx: Tx, attachment_id: str, reprocess: str | None = None) -> list[JobSpec]:
        """The next jobs the Attachment lacks; with a reprocess nonce, those of the reprocess, identify first."""
        attachment = store.get(tx, EntityRef(Entity.ATTACHMENT, attachment_id))
        if attachment is None or attachment["role"] not in PLANNED_ROLES:
            return []
        sha256 = attachment["blob_sha256"]
        found = self._identified(tx, sha256, reprocess)
        if found is None:
            return [identify_spec(sha256, reprocess)]
        refined = refine(found.media_type, attachment.get("original_filename"), attachment.get("declared_mime"), self.readers)
        media.set_detected_mime(tx, attachment, refined.detected_mime)
        return [JobSpec(job_type, f"blob:{sha256}", (sha256,), {**params, **_nonce(reprocess)}) for job_type, params in refined.jobs]

    async def planner(self, view: CaptureView, context: JobContext) -> list[JobSpec]:
        ids = [attachment["id"] for attachment in view.attachments]
        return await context.db.write(lambda tx: [spec for attachment_id in ids for spec in self.plan(tx, attachment_id, view.reprocess)])

    def _plan_blob(self, tx: Tx, registry: JobRegistry, sha256: str, reprocess: str | None) -> None:
        ids = [identifier for (identifier,) in tx.execute("SELECT id FROM attachments WHERE blob_sha256 = ? ORDER BY rowid", (sha256,))]
        enqueue_held(tx, registry, [(PLANNER, spec) for attachment_id in ids for spec in self.plan(tx, attachment_id, reprocess)])

    def status(self, snap: Snapshot, attachment_id: str) -> FileStatus | None:
        """Where the Attachment stands: the latest identify job of its Blob and, once identified, the latest job of
        each reader refine picks, with what refine found that no job holds (the PR2 decision on identify's input).
        A Blob last identified under other rules (another identify pipeline) is shown as that reading left it, with
        RULES_CHANGED: nothing replans it until the user reprocesses (decision 5). So is a reader with no job under
        its current pipeline (an extractor added or upgraded since), with can_read; RULES_CHANGED, which a reprocess
        also answers, stands for those."""
        attachment = store.get(snap, EntityRef(Entity.ATTACHMENT, attachment_id))
        if attachment is None or attachment["role"] not in PLANNED_ROLES:
            return None
        sha256 = attachment["blob_sha256"]
        latest: dict[tuple[str, str], Job] = {}
        current: set[tuple[str, str]] = set()
        for job in jobs.of_subject(snap, f"blob:{sha256}"):
            key = job.job_type, bodies.canonical_json(reading(job.params)).decode()
            latest[key] = job
            extractor = self.extractors.get(job.job_type)
            if extractor is not None and job.pipeline.version == self.pipeline(extractor).version:
                current.add(key)
        identified = latest.get((IDENTIFY, "{}"))
        found = media.get(snap, sha256)
        if found is None:
            return FileStatus(None, (), (identified,) if identified else ())
        refined = refine(found.media_type, attachment.get("original_filename"), attachment.get("declared_mime"), self.readers)
        facts = []
        if found.pipeline_version != self.identify_pipeline.version:
            facts.append(RULES_CHANGED)
        if refined.mismatch:
            facts.append(f"its name or declared type says another kind of file than its bytes, which are {found.media_type}")
        if not refined.jobs:
            facts.append(f"no extractor reads {refined.detected_mime}")
        keys = [(job_type, bodies.canonical_json(dict(params)).decode()) for job_type, params in refined.jobs]
        if RULES_CHANGED not in facts:
            facts.extend(can_read(job_type) for job_type, key in keys if (job_type, key) not in current)
        wanted = [latest.get(key) for key in keys]
        return FileStatus(refined.detected_mime, tuple(facts), tuple(job for job in (identified, *wanted) if job is not None))

    async def _read(
        self, job: Job, context: JobContext, request: dict, made: Callable[[str, dict], tuple[bytes | Path, Callable[[Tx, str], object]]],
        *, whole: bool = True,
    ) -> Finished:
        """Run the child on the job's Blob and record what made builds from its answer. A child that reads the whole
        Blob (whole) is not handed one larger than input_max_bytes; identify never holds a whole Blob (sniff)."""
        sha256 = job.inputs[0]
        size = await context.db.read(lambda snap: snap.execute("SELECT size FROM blobs WHERE sha256 = ?", (sha256,)).fetchone())
        if size is None:
            raise FileNotFoundError(f"the Hub holds no Blob {sha256}")
        if whole and size[0] > self.limits.input_max_bytes:
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
            content = await (context.blobs.put_file(stored) if isinstance(stored, Path) else context.blobs.put_bytes(stored))
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
                self._plan_blob(tx, context.registry, sha256, job.params.get(REPROCESS))

            return stored, record

        return await self._read(job, context, {"op": "identify"}, made, whole=False)

    async def extract(self, job: Job, context: JobContext) -> Finished:
        extractor = self.extractors[job.job_type]
        params = reading(job.params)
        named = {"id": extractor.id, "version": extractor.version, "params": params}

        def made(sha256: str, result: dict):
            stored = bodies.extraction(sha256, named, result, self.limits)
            return stored, lambda tx, content: artifacts.record(tx, job, "extraction", content)

        return await self._read(job, context, {"op": "extract", "entry": extractor.entry, "params": params}, made)

    async def render(self, job: Job, context: JobContext) -> Finished:
        """The converter writes a PDF in the work directory; that file, as it is, is the content of the job's rendition
        Artifact (D12 and the PR1 decision: no body, no Relation)."""
        converter = self.extractors[job.job_type]
        out = context.work_dir(job) / RENDITION

        def made(sha256: str, result: dict):
            if out.is_symlink() or not out.is_file():
                raise bodies.Rejected(f"the converter wrote no {RENDITION}")
            size = out.stat().st_size
            if size > self.limits.rendition_max_bytes:
                raise bodies.TooLarge(f"the rendition holds {size} bytes, more than {self.limits.rendition_max_bytes}")
            with out.open("rb") as handle:
                if handle.read(len(PDF_MAGIC)) != PDF_MAGIC:
                    raise bodies.Rejected(f"the converter's {RENDITION} is not a PDF")
            return out, lambda tx, content: artifacts.record(tx, job, "rendition", content)

        request = {"op": "render", "entry": converter.entry, "params": reading(job.params), "out": str(out)}
        return await self._read(job, context, request, made)

