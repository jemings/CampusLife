"""capture.ingest (E14): the one job capture.commit registers, in the commit's own transaction.

It does nothing else there: planners, which are other code, run later from the job, so
their failures cannot undo an ACK.
"""

from __future__ import annotations

from .jobs import JobSpec, Pipeline

INGEST = "capture.ingest"
INGEST_PIPELINE = Pipeline("capture.ingest-1")


def ingest_spec(capture: dict) -> JobSpec:
    """The capture id is a param: two Captures of the same files are ingested each, not deduplicated as one."""
    blobs = dict.fromkeys(attachment["blob_sha256"] for attachment in capture["attachments"])
    return JobSpec(INGEST, f"capture:{capture['id']}", tuple(blobs), {"capture_id": capture["id"]})
