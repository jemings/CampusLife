"""ProcessingJobs (D1, D4, E12): the only place processing state is kept.

A job is found by its job_key, the hash of what decides its result: its type, inputs,
params and the pipeline that runs it. Enqueueing the same again returns the job already
there, in whatever state it is; only `retry` puts a failed or blocked job back. A job's
state changes only as TRANSITIONS allows, checked in the UPDATE itself.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from campuslife_contracts import canonical_json

from .clock import instant
from .db import Snapshot, Tx


class JobState(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class OutcomeCode(enum.StrEnum):
    UNSUPPORTED = "unsupported"
    PASSWORD_REQUIRED = "password_required"
    CORRUPT = "corrupt"
    NEEDS_CONVERSION = "needs_conversion"
    PARTIAL = "partial"
    POLICY_BLOCKED = "policy_blocked"
    BUDGET_BLOCKED = "budget_blocked"
    UNSAFE_INPUT = "unsafe_input"


TRANSITIONS: Mapping[JobState, frozenset[JobState]] = {
    JobState.RUNNING: frozenset({JobState.PENDING, JobState.RETRY_WAIT}),
    JobState.RETRY_WAIT: frozenset({JobState.RUNNING}),
    JobState.SUCCEEDED: frozenset({JobState.RUNNING}),
    JobState.FAILED: frozenset({JobState.RUNNING}),
    JobState.BLOCKED: frozenset({JobState.RUNNING}),
    JobState.CANCELLED: frozenset({JobState.PENDING, JobState.RETRY_WAIT, JobState.RUNNING}),
    JobState.PENDING: frozenset({JobState.FAILED, JobState.BLOCKED}),
}
"""Each state a job can be moved to, with the states it can be moved there from (E12)."""


@dataclass(frozen=True)
class Pipeline:
    """What runs a job type, and so part of job_key (D4): another version or model makes other jobs."""

    version: str
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class JobSpec:
    job_type: str
    subject_ref: str
    inputs: tuple[str, ...] = ()
    """SHA-256 of each input: Blobs, and Artifacts by their content Blob. A set: their order and repeats make no
    other job, so an order that matters goes in params."""
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
    id: str
    job_key: str
    job_type: str
    subject_ref: str
    inputs: tuple[str, ...]
    params: dict
    pipeline: Pipeline
    state: JobState
    outcome_code: OutcomeCode | None
    attempts: int
    next_attempt_at: str | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Succeeded:
    outcome_code: OutcomeCode | None = None
    """partial when only part of the input could be processed."""


@dataclass(frozen=True)
class Failed:
    error: str
    outcome_code: OutcomeCode | None = None


@dataclass(frozen=True)
class Blocked:
    """Waits for the user or a setting to change, then for `retry`."""

    outcome_code: OutcomeCode
    error: str


type Finished = Succeeded | Failed | Blocked


class NoSuchJob(LookupError):
    pass


class NotRetryable(Exception):
    pass


SHA256 = re.compile("[0-9a-f]{64}")


def job_key(spec: JobSpec, pipeline: Pipeline) -> str:
    """D4. The inputs are a set: their order does not change the key. The subject is not part of it."""
    parts = {
        "job_type": spec.job_type,
        "inputs": sorted(set(spec.inputs)),
        "params": dict(spec.params),
        "pipeline_version": pipeline.version,
        "provider": pipeline.provider,
        "model": pipeline.model,
        "prompt_version": pipeline.prompt_version,
    }
    return hashlib.sha256(canonical_json(parts)).hexdigest()


def check(spec: JobSpec, pipeline: Pipeline) -> str:
    """spec's job_key under pipeline; TypeError or ValueError when spec cannot be stored as a job."""
    if not isinstance(spec.subject_ref, str) or not spec.subject_ref:
        raise TypeError(f"a job's subject_ref is a reference, not {spec.subject_ref!r}")
    for sha256 in spec.inputs:
        if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
            raise ValueError(f"a job's inputs are SHA-256 hex digests, not {sha256!r}")
    return job_key(spec, pipeline)


COLUMNS = (
    "id, job_key, job_type, subject_ref, inputs, params, pipeline_version, provider, model, prompt_version,"
    " state, outcome_code, attempts, next_attempt_at, error, created_at, updated_at"
)


def _job(row: tuple) -> Job:
    (identifier, key, job_type, subject_ref, inputs, params, version, provider, model, prompt_version,
     state, outcome_code, attempts, next_attempt_at, error, created_at, updated_at) = row
    return Job(
        identifier, key, job_type, subject_ref, tuple(json.loads(inputs)), json.loads(params),
        Pipeline(version, provider, model, prompt_version), JobState(state), None if outcome_code is None else OutcomeCode(outcome_code),
        attempts, next_attempt_at, error, created_at, updated_at,
    )


def _now(now: datetime | None) -> str:
    return instant(datetime.now(UTC) if now is None else now)


def get(runner: Tx | Snapshot, job_id: str) -> Job:
    row = runner.execute(f"SELECT {COLUMNS} FROM processing_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise NoSuchJob(job_id)
    return _job(row)


def of_subject(runner: Tx | Snapshot, subject_ref: str) -> list[Job]:
    rows = runner.execute(f"SELECT {COLUMNS} FROM processing_jobs WHERE subject_ref = ? ORDER BY rowid", (subject_ref,)).fetchall()
    return [_job(row) for row in rows]


def enqueue(tx: Tx, spec: JobSpec, pipeline: Pipeline, *, now: datetime | None = None) -> Job:
    """The job for spec under pipeline: a new pending one, or the one with its job_key as it stands."""
    key = check(spec, pipeline)
    at = _now(now)
    tx.execute(
        f"INSERT INTO processing_jobs ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, NULL, ?, ?)"
        " ON CONFLICT (job_key) DO NOTHING",
        (
            str(uuid.uuid4()), key, spec.job_type, spec.subject_ref, json.dumps(list(spec.inputs)), json.dumps(dict(spec.params), ensure_ascii=False),
            pipeline.version, pipeline.provider, pipeline.model, pipeline.prompt_version, JobState.PENDING.value, at, at, at,
        ),
    )
    (identifier,) = tx.execute("SELECT id FROM processing_jobs WHERE job_key = ?", (key,)).fetchone()
    return get(tx, identifier)


def _change(tx: Tx, job_id: str, state: JobState, now: datetime | None, **columns: object) -> bool:
    """Every change of a job's state passes here; whether TRANSITIONS let the job move from where it is."""
    sources = sorted(TRANSITIONS[state])
    assignments = "".join(f", {name} = ?" for name in columns)
    values = [value.value if isinstance(value, enum.Enum) else value for value in columns.values()]
    cursor = tx.execute(
        f"UPDATE processing_jobs SET state = ?{assignments}, updated_at = ? WHERE id = ? AND state IN ({', '.join('?' for _ in sources)})",
        (state.value, *values, _now(now), job_id, *sources),
    )
    return cursor.rowcount == 1


def claim(tx: Tx, job_types: Iterable[str], *, now: datetime) -> Job | None:
    """The waiting job of one of job_types that has been due longest, now running and one attempt further."""
    types = sorted(set(job_types))
    row = tx.execute(
        f"SELECT id FROM processing_jobs WHERE next_attempt_at <= ? AND job_type IN ({', '.join('?' for _ in types)})"
        " ORDER BY next_attempt_at, rowid LIMIT 1",
        (instant(now), *types),
    ).fetchone()
    if row is None:
        return None
    job = get(tx, row[0])
    _change(tx, job.id, JobState.RUNNING, now, attempts=job.attempts + 1, next_attempt_at=None)
    return get(tx, job.id)


def next_due(runner: Tx | Snapshot, job_types: Iterable[str]) -> str | None:
    """When the soonest waiting job of one of job_types is due."""
    types = sorted(set(job_types))
    (due,) = runner.execute(
        f"SELECT min(next_attempt_at) FROM processing_jobs WHERE job_type IN ({', '.join('?' for _ in types)})", types
    ).fetchone()
    return due


def _recorded(result: object) -> tuple[JobState, OutcomeCode | None, str | None]:
    match result:
        case Succeeded(code) if code is None or code in OutcomeCode:
            state, error = JobState.SUCCEEDED, None
        case Failed(str() as error, code) if code is None or code in OutcomeCode:
            state = JobState.FAILED
        case Blocked(code, str() as error) if code in OutcomeCode:
            state = JobState.BLOCKED
        case _:
            raise ValueError(f"{result!r} is not how a job finishes")
    return state, None if code is None else OutcomeCode(code), error


def finish(tx: Tx, job_id: str, result: Finished, *, now: datetime | None = None) -> Job:
    """A running job ends as result says; a job no longer running stays as it is. ValueError for a result
    outside Succeeded, Failed and Blocked with their outcome codes and error strings."""
    state, code, error = _recorded(result)
    _change(tx, job_id, state, now, outcome_code=code, error=error, next_attempt_at=None)
    return get(tx, job_id)


def defer(tx: Tx, job_id: str, error: str, *, at: datetime, now: datetime | None = None) -> Job:
    """A running job waits until `at`, keeping its attempts; a job no longer running stays as it is."""
    _change(tx, job_id, JobState.RETRY_WAIT, now, error=error, next_attempt_at=instant(at))
    return get(tx, job_id)


def retry(tx: Tx, job_id: str, *, now: datetime) -> Job:
    """The same row, pending again as if new (E12): only a failed or blocked job."""
    job = get(tx, job_id)
    if not _change(tx, job_id, JobState.PENDING, now, attempts=0, outcome_code=None, error=None, next_attempt_at=instant(now)):
        raise NotRetryable(f"job {job_id} is {job.state}; only a failed or blocked job is retried")
    return get(tx, job_id)


def cancel(tx: Tx, job_id: str, *, now: datetime | None = None) -> Job:
    """A job not yet finished ends cancelled; a finished one stays as it is.

    A running job is cancelled through its runner, which stops it before calling this.
    """
    _change(tx, job_id, JobState.CANCELLED, now, next_attempt_at=None)
    return get(tx, job_id)
