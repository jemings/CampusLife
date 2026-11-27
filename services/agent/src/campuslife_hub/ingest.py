"""capture.ingest (E14): the one job capture.commit registers, in the commit's own transaction.

It does nothing else there. Its handler runs later, outside that transaction, and asks every
registered planner what to run for the Capture, so planners, which are other code, cannot
undo an ACK by failing. A purge can remove an Attachment or a Blob while the planners run, so
the jobs they propose are queued only if the Hub still holds their subject and inputs; the
others are dropped with a warning. A subject the Hub holds no record of, no row, no tombstone
and, for a Blob, no purge, is the planner's mistake, not a purge's, and fails the planning.

A reprocess (decision 5) queues capture.ingest again with a nonce in its params, the #8 convention, and planners put
the same nonce in the params of every job they plan for it: job keys are new, so the jobs run again, and jobs of one
reprocess still share keys, so a file several Captures hold is read once. Nothing replans by itself when an extractor
is upgraded.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from . import jobs, refs, store
from .blobs import has_blob
from .db import Snapshot, Tx
from .jobs import Failed, Finished, Job, JobSpec, Pipeline, Succeeded
from .store import Entity, EntityRef

if TYPE_CHECKING:
    from .job_runner import JobContext, JobRegistry

log = logging.getLogger(__name__)

INGEST = "capture.ingest"
INGEST_PIPELINE = Pipeline("capture.ingest-1")


REPROCESS = "reprocess"
"""The param naming a reprocess (decision 5): a nonce that makes new job keys for what was processed already."""


def ingest_spec(capture: dict, *, reprocess: str | None = None) -> JobSpec:
    """The capture id is a param: two Captures of the same files are ingested each, not deduplicated as one. A
    reprocess nonce is one too, so the Capture is planned again."""
    blobs = dict.fromkeys(attachment["blob_sha256"] for attachment in capture["attachments"])
    params = {"capture_id": capture["id"], **({REPROCESS: reprocess} if reprocess is not None else {})}
    return JobSpec(INGEST, f"capture:{capture['id']}", tuple(blobs), params)


class NoSuchCapture(LookupError):
    pass


@dataclass(frozen=True)
class Reprocessed:
    nonce: str
    captures: tuple[str, ...]


def reprocess(tx: Tx, capture_ids: Sequence[str] | None, *, now: datetime | None = None) -> Reprocessed:
    """Queue capture.ingest again for each Capture, or for every Capture when capture_ids is None, under one new nonce
    (decision 5). Its planners pass the nonce on to every job they plan, so what was processed is processed again,
    the files the Captures share once; the Blobs are only read. NoSuchCapture names one the Hub does not hold."""
    if capture_ids is None:
        capture_ids = [identifier for (identifier,) in tx.execute("SELECT id FROM captures ORDER BY rowid")]
    nonce = str(uuid.uuid4())
    for capture_id in capture_ids:
        view = _view(tx, capture_id, nonce)
        if view is None:
            raise NoSuchCapture(capture_id)
        jobs.enqueue(tx, ingest_spec({"id": capture_id, "attachments": view.attachments}, reprocess=nonce), INGEST_PIPELINE, now=now)
    return Reprocessed(nonce, tuple(capture_ids))


@dataclass(frozen=True)
class FileStatus:
    """What a planner says of one Attachment it plans for, for the local UI (S13)."""

    detected_mime: str | None
    facts: tuple[str, ...]
    """What the UI should say that no job holds, such as a name that disagrees with the bytes."""
    jobs: tuple[Job, ...]
    """The latest job of each kind the Attachment needs, in the order they run."""


type FileStatusOf = Callable[[Snapshot, str], FileStatus | None]
"""A planner's FileStatus of the Attachment with this id, None for one it does not plan."""


@dataclass(frozen=True)
class CaptureView:
    """What a planner sees: the Capture row and its Attachment rows in commit order, and the reprocess nonce the
    planner gives the jobs it plans, None when this is no reprocess."""

    capture: dict
    attachments: tuple[dict, ...]
    reprocess: str | None = None


class PlanningFailed(Exception):
    pass


def _view(snap: Snapshot, capture_id: str, reprocess: str | None) -> CaptureView | None:
    capture = store.get(snap, EntityRef(Entity.CAPTURE, capture_id))
    if capture is None:
        return None
    rows = snap.execute("SELECT data FROM attachments WHERE capture_id = ? ORDER BY rowid", (capture_id,)).fetchall()
    return CaptureView(capture, tuple(json.loads(data) for (data,) in rows), reprocess)


def enqueue_held(
    tx: Tx, registry: JobRegistry, planned: Sequence[tuple[str, JobSpec]], *, now: datetime | None = None,
) -> tuple[list[JobSpec], list[str]]:
    """Queue each planned spec whose subject and inputs the Hub holds. A planner that proposed a job on a subject the
    Hub has no record of (refs.ever_existed) has none of its specs queued. Returns the specs dropped for what a purge removed, and why each
    such planner failed."""
    mistakes: dict[str, str] = {}
    for name, spec in planned:
        if name not in mistakes and not refs.ever_existed(tx, spec.subject_ref):
            mistakes[name] = (
                f"{name}: the Hub holds no {spec.subject_ref}, the subject of a planned {spec.job_type}, and has no record of removing it"
            )
    dropped = []
    for name, spec in planned:
        if name in mistakes:
            continue
        if refs.exists(tx, spec.subject_ref) and all(has_blob(tx, sha256) for sha256 in spec.inputs):
            registry.enqueue(tx, spec, now=now)
        else:
            dropped.append(spec)
    return dropped, list(mistakes.values())


async def run_ingest(job: Job, context: JobContext) -> Finished:
    """Queue every planner's jobs. A planner that raises, or proposes a job without a handler, one that cannot be
    stored or one on a subject the Hub has no record of, keeps out only its own jobs and makes the ingest try again; jobs
    queued before come back as they are."""
    capture_id = job.params["capture_id"]
    view = await context.db.read(lambda snap: _view(snap, capture_id, job.params.get(REPROCESS)))
    if view is None:
        return Failed(f"capture {capture_id} is not in the Hub")
    planned, failures = [], []
    for name, planner in context.registry.planners:
        try:
            specs = list(await planner(view, context))
            for spec in specs:
                jobs.check(spec, context.registry.pipeline(spec.job_type))
        except Exception as error:
            failures.append(f"{name}: {type(error).__name__}: {error}")
        else:
            planned += [(name, spec) for spec in specs]
    dropped, mistakes = await context.db.write(lambda tx: enqueue_held(tx, context.registry, planned))
    for spec in dropped:
        log.warning("dropped the planned %s on %s: the Hub does not hold its subject or an input", spec.job_type, spec.subject_ref)
    failures += mistakes
    if failures:
        raise PlanningFailed("; ".join(failures))
    return Succeeded()
