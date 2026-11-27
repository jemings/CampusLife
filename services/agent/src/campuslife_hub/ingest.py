"""capture.ingest (E14): the one job capture.commit registers, in the commit's own transaction.

It does nothing else there. Its handler runs later, outside that transaction, and asks every
registered planner what to run for the Capture, so planners, which are other code, cannot
undo an ACK by failing. A purge can remove an Attachment or a Blob while the planners run, so
the jobs they propose are queued only if the Hub still holds their subject and inputs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
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


def _enqueue_held(tx: Tx, registry: JobRegistry, specs: Iterable[JobSpec]) -> list[JobSpec]:
    """Queue each spec whose subject and inputs the Hub holds; the ones it does not queue."""
    dropped = []
    for spec in specs:
        if refs.exists(tx, spec.subject_ref) and all(has_blob(tx, sha256) for sha256 in spec.inputs):
            registry.enqueue(tx, spec)
        else:
            dropped.append(spec)
    return dropped


async def run_ingest(job: Job, context: JobContext) -> Finished:
    """Queue every planner's jobs. A planner that raises, or proposes a job without a handler or one that cannot be
    stored, keeps out only its own jobs and makes the ingest try again; jobs queued before come back as they are."""
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
            planned += specs
    dropped = await context.db.write(lambda tx: _enqueue_held(tx, context.registry, planned))
    for spec in dropped:
        log.info("dropped the planned %s on %s: the Hub no longer holds its subject or an input", spec.job_type, spec.subject_ref)
    if failures:
        raise PlanningFailed("; ".join(failures))
    return Succeeded()
