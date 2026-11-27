"""capture.ingest (E14): the one job capture.commit registers, in the commit's own transaction.

It does nothing else there. Its handler runs later, outside that transaction, and asks every
registered planner what to run for the Capture, so planners, which are other code, cannot
undo an ACK by failing. A purge can remove an Attachment or a Blob while the planners run, so
the jobs they propose are queued only if the Hub still holds their subject and inputs; the
others are dropped with a warning. A subject the Hub holds no record of, no row, no tombstone
and, for a Blob, no purge, is the planner's mistake, not a purge's, and fails the planning.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
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


def ingest_spec(capture: dict) -> JobSpec:
    """The capture id is a param: two Captures of the same files are ingested each, not deduplicated as one."""
    blobs = dict.fromkeys(attachment["blob_sha256"] for attachment in capture["attachments"])
    return JobSpec(INGEST, f"capture:{capture['id']}", tuple(blobs), {"capture_id": capture["id"]})


@dataclass(frozen=True)
class CaptureView:
    """What a planner sees: the Capture row and its Attachment rows in commit order."""

    capture: dict
    attachments: tuple[dict, ...]


class PlanningFailed(Exception):
    pass


def _view(snap: Snapshot, capture_id: str) -> CaptureView | None:
    capture = store.get(snap, EntityRef(Entity.CAPTURE, capture_id))
    if capture is None:
        return None
    rows = snap.execute("SELECT data FROM attachments WHERE capture_id = ? ORDER BY rowid", (capture_id,)).fetchall()
    return CaptureView(capture, tuple(json.loads(data) for (data,) in rows))


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
    view = await context.db.read(lambda snap: _view(snap, capture_id))
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
