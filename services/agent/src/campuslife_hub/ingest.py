"""capture.ingest (E14): the one job capture.commit registers, in the commit's own transaction.

It does nothing else there. Its handler runs later, outside that transaction, and asks every
registered planner what to run for the Capture, so planners, which are other code, cannot
undo an ACK by failing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import jobs, store
from .db import Snapshot
from .jobs import Failed, Finished, Job, JobSpec, Pipeline, Succeeded
from .store import Entity, EntityRef

if TYPE_CHECKING:
    from .job_runner import JobContext

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
    await context.db.write(lambda tx: [context.registry.enqueue(tx, spec) for spec in planned])
    if failures:
        raise PlanningFailed("; ".join(failures))
    return Succeeded()
