"""Artifacts (D9, E14): what a job made, its content in a Blob, never changed once recorded.

A job's handler records its Artifacts before it returns. A Hub that stops after that and
before the job is finished runs the job again; `record` then gives back what the first run
recorded, so a job leaves one Artifact of each type whatever it made the second time.
A rerun under another pipeline is another job, and makes a new Artifact.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from campuslife_contracts import validate

from . import store
from .blobs import has_blob
from .clock import instant, now_instant
from .db import Snapshot, Tx
from .jobs import Job
from .store import Entity, EntityRef


class InvalidArtifact(ValueError):
    """The Artifact would break the contract or name content the Hub does not hold."""


def made_by(runner: Tx | Snapshot, job_key: str, artifact_type: str) -> dict | None:
    row = runner.execute("SELECT data FROM artifacts WHERE job_key = ? AND artifact_type = ?", (job_key, artifact_type)).fetchone()
    return None if row is None else json.loads(row[0])


def record(tx: Tx, job: Job, artifact_type: str, content_blob: str, *, now: datetime | None = None) -> dict:
    """The job's Artifact of artifact_type: the one recorded before, or a new one holding content_blob."""
    earlier = made_by(tx, job.job_key, artifact_type)
    if earlier is not None:
        return earlier
    pipeline = job.pipeline
    models = {"provider": pipeline.provider, "model": pipeline.model, "prompt_version": pipeline.prompt_version}
    artifact = {
        "id": str(uuid.uuid4()),
        "artifact_type": artifact_type,
        "subject_ref": job.subject_ref,
        "job_key": job.job_key,
        **{name: value for name, value in models.items() if value is not None},
        "pipeline_version": pipeline.version,
        "input_hashes": list(dict.fromkeys(job.inputs)),
        "content_blob": content_blob,
        "created_at": now_instant() if now is None else instant(now),
    }
    violations = validate("artifact/artifact", artifact)
    if violations:
        raise InvalidArtifact(f"the Artifact breaks the contract: {violations}")
    if not has_blob(tx, content_blob):
        raise InvalidArtifact(f"the Hub holds no Blob {content_blob}")
    store.write(tx, EntityRef(Entity.ARTIFACT, artifact["id"]), artifact)
    return artifact
