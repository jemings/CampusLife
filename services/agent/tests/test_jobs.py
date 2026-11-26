import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from campuslife_hub import jobs
from campuslife_hub.clock import instant
from campuslife_hub.jobs import Blocked, Failed, JobSpec, JobState, NotRetryable, OutcomeCode, Pipeline, Succeeded

PDF = "a" * 64
SLIDES = "b" * 64
EXTRACT = Pipeline("extract-1")
NOW = datetime(2026, 3, 2, 0, 30, tzinfo=UTC)


def extraction(**changes):
    return JobSpec(**{"job_type": "extract.text", "subject_ref": "attachment:7c9e6679-7425-40de-944b-e07fc1f90ae7", "inputs": (PDF,), "params": {}, **changes})


async def enqueue(db, spec=None, pipeline=EXTRACT, now=NOW):
    return await db.write(lambda tx: jobs.enqueue(tx, spec or extraction(), pipeline, now=now))


async def job(db, job_id):
    return await db.read(lambda snap: jobs.get(snap, job_id))


async def count(db):
    return await db.read(lambda snap: snap.execute("SELECT count(*) FROM processing_jobs").fetchone()[0])


async def claim(db, now=NOW, job_types=("extract.text",)):
    return await db.write(lambda tx: jobs.claim(tx, job_types, now=now))


async def test_an_enqueued_job_is_pending_and_due_now_with_no_attempts(db):
    queued = await enqueue(db)

    assert queued == await job(db, queued.id)
    assert (queued.job_type, queued.subject_ref, queued.inputs, queued.params, queued.pipeline) == (
        "extract.text", "attachment:7c9e6679-7425-40de-944b-e07fc1f90ae7", (PDF,), {}, EXTRACT,
    )
    assert (queued.state, queued.attempts, queued.outcome_code, queued.error) == (JobState.PENDING, 0, None, None)
    assert queued.next_attempt_at == instant(NOW)
    assert re.fullmatch("[0-9a-f]{64}", queued.job_key)


async def test_the_same_spec_under_the_same_pipeline_is_one_job(db):
    first = await enqueue(db)

    again = await enqueue(db, now=NOW + timedelta(hours=1))

    assert again == first
    assert await count(db) == 1


@pytest.mark.parametrize(
    ("spec", "pipeline"),
    [
        (extraction(job_type="extract.ocr"), EXTRACT),
        (extraction(inputs=(PDF, SLIDES)), EXTRACT),
        (extraction(params={"language": "ko"}), EXTRACT),
        (extraction(), Pipeline("extract-2")),
        (extraction(), Pipeline("extract-1", provider="local")),
        (extraction(), Pipeline("extract-1", model="whisper-small")),
        (extraction(), Pipeline("extract-1", prompt_version="p1")),
    ],
    ids=["job_type", "inputs", "params", "pipeline_version", "provider", "model", "prompt_version"],
)
async def test_each_part_of_the_job_key_makes_another_job(db, spec, pipeline):
    first = await enqueue(db)

    other = await enqueue(db, spec, pipeline)

    assert other.job_key != first.job_key
    assert await count(db) == 2


async def test_the_subject_and_the_order_and_repeats_of_inputs_are_not_part_of_the_job_key(db):
    first = await enqueue(db, extraction(inputs=(PDF, SLIDES)))

    again = await enqueue(db, extraction(inputs=(SLIDES, PDF, SLIDES), subject_ref="attachment:6ba7b810-9dad-41d1-80b4-00c04fd430c8"))

    assert again == first


@pytest.mark.parametrize(
    "spec",
    [
        extraction(params={"due": date(2026, 3, 2)}),
        extraction(params={"scale": float("nan")}),
        extraction(params={"pages": (1, 2)}),
        extraction(subject_ref=None),
        extraction(subject_ref=""),
        extraction(inputs=("slides.pdf",)),
        extraction(inputs=(7,)),
        extraction(subject_ref="attachment:x\ud800"),
        extraction(params={"n": 10**5000}),
        extraction(params={"scale": Decimal("1.5")}),
        extraction(job_type=7),
    ],
    ids=[
        "date param", "nan param", "tuple param", "no subject", "empty subject", "input not a sha256", "input not a str",
        "subject not UTF-8", "param past what JSON text holds", "Decimal param",
        "job type not a str",
    ],
)
async def test_a_spec_that_cannot_be_stored_is_refused_and_nothing_is_queued(db, spec):
    with pytest.raises((TypeError, ValueError)):
        jobs.check(spec, EXTRACT)
    with pytest.raises((TypeError, ValueError)):
        await enqueue(db, spec)

    assert await count(db) == 0


def test_a_spec_that_can_be_stored_checks_to_its_job_key():
    assert jobs.check(extraction(params={"language": "ko"}), EXTRACT) == jobs.job_key(extraction(params={"language": "ko"}), EXTRACT)


async def test_a_claimed_job_is_running_with_one_more_attempt(db):
    queued = await enqueue(db)

    claimed = await claim(db)

    assert claimed == await job(db, queued.id)
    assert (claimed.state, claimed.attempts, claimed.next_attempt_at) == (JobState.RUNNING, 1, None)
    assert await claim(db) is None


async def test_only_due_jobs_of_the_given_types_are_claimed_oldest_first(db):
    later = await enqueue(db, extraction(params={"n": 2}), now=NOW + timedelta(seconds=1))
    earlier = await enqueue(db, extraction(params={"n": 1}))
    await enqueue(db, extraction(job_type="summary"))

    assert await claim(db, now=NOW - timedelta(seconds=1)) is None
    assert [(await claim(db, now=NOW + timedelta(seconds=1))).id for _ in range(2)] == [earlier.id, later.id]
    assert await claim(db, now=NOW + timedelta(days=1)) is None


@pytest.mark.parametrize(
    ("result", "state", "code", "error"),
    [
        (Succeeded(), JobState.SUCCEEDED, None, None),
        (Succeeded(OutcomeCode.PARTIAL), JobState.SUCCEEDED, OutcomeCode.PARTIAL, None),
        (Failed("not a PDF", OutcomeCode.CORRUPT), JobState.FAILED, OutcomeCode.CORRUPT, "not a PDF"),
        (Blocked(OutcomeCode.PASSWORD_REQUIRED, "encrypted"), JobState.BLOCKED, OutcomeCode.PASSWORD_REQUIRED, "encrypted"),
    ],
)
async def test_a_finished_job_keeps_its_state_outcome_and_error(db, result, state, code, error):
    queued = await enqueue(db)
    await claim(db)

    await db.write(lambda tx: jobs.finish(tx, queued.id, result))

    finished = await job(db, queued.id)
    assert (finished.state, finished.outcome_code, finished.error, finished.attempts, finished.next_attempt_at) == (state, code, error, 1, None)


async def test_a_deferred_job_waits_until_its_time_and_is_then_claimed_again(db):
    queued = await enqueue(db)
    await claim(db)
    at = NOW + timedelta(minutes=2)

    await db.write(lambda tx: jobs.defer(tx, queued.id, "provider busy", at=at))

    waiting = await job(db, queued.id)
    assert (waiting.state, waiting.error, waiting.next_attempt_at) == (JobState.RETRY_WAIT, "provider busy", instant(at))
    assert await claim(db, now=at - timedelta(milliseconds=1)) is None
    assert (await claim(db, now=at)).attempts == 2


@pytest.mark.parametrize("result", [Failed("broken", OutcomeCode.CORRUPT), Blocked(OutcomeCode.POLICY_BLOCKED, "blocked")])
async def test_retry_puts_a_failed_or_blocked_job_back_as_new(db, result):
    queued = await enqueue(db)
    await claim(db)
    await db.write(lambda tx: jobs.finish(tx, queued.id, result))
    later = NOW + timedelta(hours=1)

    retried = await db.write(lambda tx: jobs.retry(tx, queued.id, now=later))

    assert retried == await job(db, queued.id)
    assert (retried.id, retried.state, retried.attempts, retried.outcome_code, retried.error) == (queued.id, JobState.PENDING, 0, None, None)
    assert retried.next_attempt_at == instant(later)
    assert await count(db) == 1


async def in_state(db, state):
    """A job brought to state through the queue's own steps."""
    queued = await enqueue(db)
    if state is not JobState.PENDING:
        await claim(db)
    match state:
        case JobState.RETRY_WAIT:
            await db.write(lambda tx: jobs.defer(tx, queued.id, "busy", at=NOW))
        case JobState.SUCCEEDED:
            await db.write(lambda tx: jobs.finish(tx, queued.id, Succeeded()))
        case JobState.CANCELLED:
            await db.write(lambda tx: jobs.cancel(tx, queued.id))
    return await job(db, queued.id)


@pytest.mark.parametrize("state", [JobState.PENDING, JobState.RUNNING, JobState.RETRY_WAIT, JobState.SUCCEEDED, JobState.CANCELLED])
async def test_retry_refuses_a_job_that_did_not_fail_or_block(db, state):
    before = await in_state(db, state)
    assert before.state is state

    with pytest.raises(NotRetryable):
        await db.write(lambda tx: jobs.retry(tx, before.id, now=NOW))

    assert await job(db, before.id) == before


@pytest.mark.parametrize("state", [JobState.PENDING, JobState.RETRY_WAIT, JobState.RUNNING])
async def test_a_job_not_yet_finished_is_cancelled_and_never_claimed(db, state):
    queued = await in_state(db, state)

    cancelled = await db.write(lambda tx: jobs.cancel(tx, queued.id))

    assert (cancelled.state, cancelled.next_attempt_at) == (JobState.CANCELLED, None)
    assert await claim(db, now=NOW + timedelta(days=1)) is None


@pytest.mark.parametrize("state", [JobState.PENDING, JobState.RETRY_WAIT, JobState.SUCCEEDED, JobState.CANCELLED])
@pytest.mark.parametrize(
    "change",
    [
        lambda tx, job_id: jobs.finish(tx, job_id, Succeeded()),
        lambda tx, job_id: jobs.finish(tx, job_id, Failed("broken")),
        lambda tx, job_id: jobs.finish(tx, job_id, Blocked(OutcomeCode.POLICY_BLOCKED, "blocked")),
        lambda tx, job_id: jobs.defer(tx, job_id, "busy", at=NOW + timedelta(hours=1)),
    ],
    ids=["succeed", "fail", "block", "defer"],
)
async def test_only_a_running_job_finishes_or_waits_again(db, state, change):
    before = await in_state(db, state)

    assert await db.write(lambda tx: change(tx, before.id)) == before
    assert await job(db, before.id) == before


@pytest.mark.parametrize(
    "result",
    [Succeeded("partly"), Failed(None), Failed("broken", "broken"), Blocked(None, "blocked"), Blocked(OutcomeCode.POLICY_BLOCKED, 3), "done", None],
    ids=["unknown outcome", "no error", "unknown failure outcome", "blocked without outcome", "error not a str", "a str", "nothing"],
)
async def test_a_result_outside_the_contract_is_refused_and_the_job_stays_running(db, result):
    running = await in_state(db, JobState.RUNNING)

    with pytest.raises(ValueError, match="is not how a job finishes"):
        await db.write(lambda tx: jobs.finish(tx, running.id, result))

    assert await job(db, running.id) == running


async def test_an_outcome_given_as_its_value_is_recorded_as_that_outcome(db):
    running = await in_state(db, JobState.RUNNING)

    finished = await db.write(lambda tx: jobs.finish(tx, running.id, Failed("not a PDF", "corrupt")))

    assert (finished.state, finished.outcome_code) == (JobState.FAILED, OutcomeCode.CORRUPT)


async def test_cancelling_a_finished_job_changes_nothing(db):
    queued = await enqueue(db)
    await claim(db)
    await db.write(lambda tx: jobs.finish(tx, queued.id, Succeeded()))
    before = await job(db, queued.id)

    assert await db.write(lambda tx: jobs.cancel(tx, queued.id)) == before


async def test_a_cancelled_job_stays_cancelled_when_its_spec_is_enqueued_again(db):
    queued = await enqueue(db)
    await db.write(lambda tx: jobs.cancel(tx, queued.id))

    again = await enqueue(db)

    assert (again.id, again.state) == (queued.id, JobState.CANCELLED)


async def test_an_unknown_job_id_is_no_such_job(db):
    with pytest.raises(jobs.NoSuchJob):
        await db.write(lambda tx: jobs.cancel(tx, "00000000-0000-4000-8000-000000000000"))


async def test_jobs_are_listed_by_subject_in_the_order_they_were_enqueued(db):
    subject = "capture:0f8fad5b-d9cb-469f-a165-70867728950e"
    first = await enqueue(db, extraction(subject_ref=subject))
    await enqueue(db, extraction(params={"other": True}))
    second = await enqueue(db, extraction(job_type="extract.ocr", subject_ref=subject))

    assert [queued.id for queued in await db.read(lambda snap: jobs.of_subject(snap, subject))] == [first.id, second.id]


async def test_jobs_a_stopped_hub_left_running_wait_again_or_fail_once_out_of_attempts(db):
    fresh, spent, waiting, done = [await enqueue(db, extraction(params={"n": n})) for n in range(4)]
    assert (await claim(db)).id == fresh.id
    for finished, state, attempts in ((spent, "running", jobs.MAX_ATTEMPTS), (done, "succeeded", 1)):
        await db.write(lambda tx: tx.execute(
            "UPDATE processing_jobs SET state = ?, attempts = ?, next_attempt_at = NULL WHERE id = ?", (state, attempts, finished.id)
        ))
    untouched = [await job(db, each.id) for each in (waiting, done)]
    restarted = NOW + timedelta(hours=1)

    assert await db.write(lambda tx: jobs.recover_after_restart(tx, now=restarted)) == 2

    again, out = await job(db, fresh.id), await job(db, spent.id)
    assert (again.state, again.attempts, again.next_attempt_at, again.error) == (JobState.RETRY_WAIT, 1, instant(restarted), jobs.STOPPED_BY_RESTART)
    assert (out.state, out.attempts, out.next_attempt_at, out.error) == (JobState.FAILED, jobs.MAX_ATTEMPTS, None, jobs.STOPPED_BY_RESTART)
    assert [await job(db, each.id) for each in (waiting, done)] == untouched
