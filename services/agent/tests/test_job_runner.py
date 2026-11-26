import asyncio
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from campuslife_hub import jobs
from campuslife_hub.clock import instant
from campuslife_hub.job_runner import HANDLER_TIMEOUT, RETRY_DELAYS, STOP_GRACE, JobContext, JobRegistry, JobRunner, UnknownJobType
from campuslife_hub.jobs import MAX_ATTEMPTS, Blocked, Failed, JobSpec, JobState, OutcomeCode, Pipeline, Succeeded
from test_capture_commit import blob_store

__all__ = ["blob_store"]

NOW = datetime(2026, 3, 2, 0, 30, tzinfo=UTC)
EXTRACT = Pipeline("extract-1")


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def spec(n=1, job_type="extract.text"):
    return JobSpec(job_type, f"attachment:{n}", ("a" * 64,), {"n": n})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def registry():
    return JobRegistry()


@pytest.fixture
def runner(db, blob_store, registry, clock):
    return JobRunner(JobContext(db, blob_store, registry), clock=clock)


def handles(registry, run, job_type="extract.text", pipeline=EXTRACT, **options):
    registry.handle(job_type, pipeline, run, **options)
    return run


async def enqueue(db, registry, *specs):
    return await db.write(lambda tx: [registry.enqueue(tx, each, now=NOW) for each in specs])


async def job(db, queued):
    return await db.read(lambda snap: jobs.get(snap, queued.id))


def returning(result, seen=None):
    async def run(job, context):
        if seen is not None:
            seen.append((job, context))
        return result

    return run


def raising(error):
    async def run(job, context):
        raise error

    return run


async def test_a_registered_job_type_is_enqueued_under_its_pipeline(db, registry):
    handles(registry, returning(Succeeded()), pipeline=Pipeline("extract-3", model="m"))

    [queued] = await enqueue(db, registry, spec())

    assert queued.pipeline == Pipeline("extract-3", model="m")


async def test_a_job_type_is_registered_once_and_an_unknown_one_cannot_be_enqueued(db, registry):
    handles(registry, returning(Succeeded()))

    with pytest.raises(ValueError):
        handles(registry, returning(Failed("other")))
    with pytest.raises(UnknownJobType):
        await enqueue(db, registry, spec(job_type="summary"))


async def test_a_job_that_succeeds_is_succeeded_after_one_attempt(db, registry, runner):
    seen = []
    handles(registry, returning(Succeeded(), seen))
    [queued] = await enqueue(db, registry, spec())

    assert await runner.run_due() == 1

    done = await job(db, queued)
    assert (done.state, done.attempts, done.outcome_code, done.error) == (JobState.SUCCEEDED, 1, None, None)
    [(given, context)] = seen
    assert (given.id, given.state, given.params) == (queued.id, JobState.RUNNING, {"n": 1})
    assert context is runner.context


@pytest.mark.parametrize(
    ("result", "state"),
    [
        (Succeeded(OutcomeCode.PARTIAL), JobState.SUCCEEDED),
        (Failed("not a PDF", OutcomeCode.CORRUPT), JobState.FAILED),
        (Blocked(OutcomeCode.POLICY_BLOCKED, "the policy blocks cloud"), JobState.BLOCKED),
    ],
)
async def test_a_handlers_result_decides_the_state_outcome_and_error(db, registry, runner, result, state):
    handles(registry, returning(result))
    [queued] = await enqueue(db, registry, spec())

    await runner.run_due()

    done = await job(db, queued)
    assert (done.state, done.outcome_code, done.error) == (state, result.outcome_code, getattr(result, "error", None))


async def test_a_job_that_raises_waits_and_then_runs_again(db, registry, runner, clock):
    outcomes = [RuntimeError("provider busy"), Succeeded()]

    async def run(job, context):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    handles(registry, run)
    [queued] = await enqueue(db, registry, spec())

    await runner.run_due()
    waiting = await job(db, queued)
    assert (waiting.state, waiting.attempts, waiting.error) == (JobState.RETRY_WAIT, 1, "RuntimeError: provider busy")
    assert waiting.next_attempt_at == instant(NOW + RETRY_DELAYS[0])

    clock.now = NOW + RETRY_DELAYS[0] - timedelta(milliseconds=1)
    assert await runner.run_due() == 0
    clock.now = NOW + RETRY_DELAYS[0]
    assert await runner.run_due() == 1
    done = await job(db, queued)
    assert (done.state, done.attempts, done.error) == (JobState.SUCCEEDED, 2, None)


async def test_each_failed_attempt_waits_longer_and_the_last_one_fails_the_job(db, registry, runner, clock):
    handles(registry, raising(OSError("disk")))
    [queued] = await enqueue(db, registry, spec())
    waits = []

    for _ in range(MAX_ATTEMPTS):
        assert await runner.run_due() == 1
        current = await job(db, queued)
        if current.state is JobState.RETRY_WAIT:
            waits.append(datetime.fromisoformat(current.next_attempt_at) - clock.now)
            clock.now = datetime.fromisoformat(current.next_attempt_at)

    assert waits == list(RETRY_DELAYS[: MAX_ATTEMPTS - 1])
    failed = await job(db, queued)
    assert (failed.state, failed.attempts, failed.outcome_code, failed.error) == (JobState.FAILED, MAX_ATTEMPTS, None, "OSError: disk")


async def test_a_long_error_is_cut_to_its_end(db, registry, runner):
    handles(registry, raising(RuntimeError("x" * 5000 + "the end")))
    [queued] = await enqueue(db, registry, spec())

    await runner.run_due()

    error = (await job(db, queued)).error
    assert len(error) <= 2000 and error.endswith("the end")


@pytest.mark.parametrize("handler", [returning(None), lambda job, context: Succeeded()], ids=["no result", "not a coroutine"])
async def test_a_handler_that_gives_no_result_counts_as_an_error(db, registry, runner, handler):
    handles(registry, handler)
    [queued] = await enqueue(db, registry, spec())

    await runner.run_due()

    assert (await job(db, queued)).state is JobState.RETRY_WAIT


def test_the_retry_schedule_and_the_time_limits_are_as_decided():
    assert RETRY_DELAYS == (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=1))
    assert MAX_ATTEMPTS == 5
    assert (HANDLER_TIMEOUT, STOP_GRACE) == (timedelta(minutes=30), timedelta(seconds=5))


def test_a_handler_runs_for_its_job_types_time_or_the_default(registry):
    handles(registry, returning(Succeeded()), timeout=timedelta(hours=2))
    handles(registry, returning(Succeeded()), job_type="extract.ocr")

    assert (registry.timeout("extract.text"), registry.timeout("extract.ocr")) == (timedelta(hours=2), HANDLER_TIMEOUT)
    with pytest.raises(UnknownJobType):
        registry.timeout("summary")


async def test_a_handler_that_ends_cancelled_by_itself_counts_as_an_error(db, registry, runner):
    handles(registry, raising(asyncio.CancelledError()))
    [queued] = await enqueue(db, registry, spec())

    assert await runner.run_due() == 1

    waiting = await job(db, queued)
    assert (waiting.state, waiting.error) == (JobState.RETRY_WAIT, "RuntimeError: the handler ended cancelled")


async def test_a_handler_past_its_time_is_stopped_and_its_job_waits_to_run_again(db, registry, runner):
    stopped = []

    async def run(job, context):
        if job.params["n"] == 2:
            return Succeeded()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopped.append(job.id)
            raise

    handles(registry, run, timeout=timedelta(seconds=0.2))
    first, second = await enqueue(db, registry, spec(1), spec(2))
    started = time.monotonic()

    assert await runner.run_due() == 2

    assert time.monotonic() - started < 5
    waiting = await job(db, first)
    assert (waiting.state, waiting.error, stopped) == (JobState.RETRY_WAIT, "TimeoutError: the handler ran past 0:00:00.200000", [first.id])
    assert (await job(db, second)).state is JobState.SUCCEEDED


async def until(condition, within_s=10.0):
    deadline = time.monotonic() + within_s
    while not await condition():
        assert time.monotonic() < deadline, "the condition did not come about in time"
        await asyncio.sleep(0.02)


def stubborn():
    """A handler that swallows every cancellation until released, with the ids of the jobs it started."""
    started, released = [], asyncio.Event()

    async def run(job, context):
        started.append(job.id)
        while not released.is_set():
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                pass
        return Succeeded()

    return run, started, released


async def test_a_handler_that_ignores_cancellation_holds_up_neither_other_jobs_nor_stop(db, blob_store, registry):
    run, started, released = stubborn()
    handles(registry, run, job_type="extract.stuck", timeout=timedelta(seconds=0.2))
    handles(registry, returning(Succeeded()))
    handles(registry, run, job_type="extract.slow")
    runner = JobRunner(JobContext(db, blob_store, registry), stop_grace=timedelta(seconds=0.2))
    stuck, other, slow = await enqueue(db, registry, spec(1, "extract.stuck"), spec(2), spec(3, "extract.slow"))
    stop = asyncio.Event()
    serving = asyncio.create_task(runner.run_forever(stop, idle_s=30))
    try:
        async def slow_started():
            return slow.id in started

        await until(slow_started)
        assert (await job(db, other)).state is JobState.SUCCEEDED
        timed_out = await job(db, stuck)
        assert (timed_out.state, timed_out.error) == (JobState.RETRY_WAIT, "TimeoutError: the handler ran past 0:00:00.200000")

        stopping = time.monotonic()
        stop.set()
        await asyncio.wait_for(serving, 5)

        assert time.monotonic() - stopping < 2
        assert (await job(db, slow)).state is JobState.RUNNING
    finally:
        released.set()


async def test_a_job_whose_handler_is_left_running_is_not_claimed_again_until_that_handler_ends(db, blob_store, registry):
    run, started, released = stubborn()
    handles(registry, run, timeout=timedelta(seconds=0.1))
    runner = JobRunner(JobContext(db, blob_store, registry), retry_delays=(timedelta(0),), stop_grace=timedelta(seconds=0.1))
    looks, run_due = [], runner.run_due

    async def counted():
        looks.append(time.monotonic())
        return await run_due()

    runner.run_due = counted
    [held] = await enqueue(db, registry, spec())
    stop = asyncio.Event()
    serving = asyncio.create_task(runner.run_forever(stop, idle_s=30))
    try:
        async def due_again():
            return (await job(db, held)).state is JobState.RETRY_WAIT

        await until(due_again)
        await asyncio.sleep(0.5)

        assert started == [held.id]
        assert len(looks) == 1

        released.set()

        async def succeeded():
            return (await job(db, held)).state is JobState.SUCCEEDED

        await until(succeeded, within_s=5)
        assert started == [held.id, held.id]
        assert (await job(db, held)).attempts == 2
    finally:
        released.set()
        stop.set()
        await asyncio.wait_for(serving, 5)


async def test_cancelling_a_handler_that_ignores_it_still_ends_its_job_cancelled_in_bounded_time(db, blob_store, registry):
    run, started, released = stubborn()
    handles(registry, run)
    runner = JobRunner(JobContext(db, blob_store, registry), stop_grace=timedelta(seconds=0.2))
    [held] = await enqueue(db, registry, spec())
    running = asyncio.create_task(runner.run_due())
    try:
        async def held_started():
            return started == [held.id]

        await until(held_started)
        cancelling = time.monotonic()

        cancelled = await asyncio.wait_for(runner.cancel(held.id), 5)

        assert time.monotonic() - cancelling < 2
        assert cancelled.state is JobState.CANCELLED
        assert await asyncio.wait_for(running, 5) == 1
    finally:
        released.set()


async def test_a_runner_stopped_while_a_cancel_waits_for_the_handler_stops_and_the_cancel_still_lands(db, registry, runner):
    started = asyncio.Event()

    async def run(job, context):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
            raise

    handles(registry, run)
    [queued] = await enqueue(db, registry, spec())
    running = asyncio.create_task(runner.run_due())
    await asyncio.wait_for(started.wait(), 10)
    cancelling = asyncio.create_task(runner.cancel(queued.id))
    await asyncio.sleep(0.05)

    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    assert (await asyncio.wait_for(cancelling, 5)).state is JobState.CANCELLED


async def test_a_job_cancelled_between_its_claim_and_its_start_never_starts(db, registry, runner, monkeypatch, caplog):
    seen = []
    handles(registry, returning(Succeeded(), seen))
    [queued] = await enqueue(db, registry, spec())
    write, gate, cancelling = db.write, threading.Event(), []

    async def cancel_after_the_claim(fn):
        """Once the claim commits, and before the runner learns of it, a cancel arrives and waits its turn to write."""
        result = await write(fn)
        if not cancelling and isinstance(result, jobs.Job) and result.state is JobState.RUNNING:
            cancelling.append(asyncio.ensure_future(write(lambda tx: gate.wait(10))))
            cancelling.append(asyncio.create_task(runner.cancel(result.id)))
            for _ in range(3):
                await asyncio.sleep(0)
            asyncio.get_running_loop().call_later(0.3, gate.set)
        return result

    monkeypatch.setattr(db, "write", cancel_after_the_claim)

    with caplog.at_level(logging.WARNING):
        ran = await asyncio.wait_for(runner.run_one(), 10)

    assert seen == []
    assert ran.state is JobState.CANCELLED
    assert caplog.records == []
    assert (await asyncio.wait_for(cancelling[1], 10)).state is JobState.CANCELLED


@pytest.mark.parametrize("bad", ["unknown outcome", "not a result", "record fails"])
async def test_a_result_that_cannot_be_recorded_retries_its_job_and_the_runner_carries_on(db, blob_store, registry, monkeypatch, bad):
    results = {"unknown outcome": Succeeded("partly"), "not a result": "done", "record fails": Succeeded()}
    errors = {
        "unknown outcome": "ValueError: Succeeded(outcome_code='partly') is not how a job finishes",
        "not a result": "ValueError: 'done' is not how a job finishes",
        "record fails": "OperationalError: disk I/O error",
    }

    async def run(job, context):
        return results[bad] if job.params["n"] == 1 else Succeeded()

    handles(registry, run)
    first, second = await enqueue(db, registry, spec(1), spec(2))
    finish = jobs.finish

    def failing_for_the_first(tx, job_id, result, **options):
        if bad == "record fails" and job_id == first.id:
            raise sqlite3.OperationalError("disk I/O error")
        return finish(tx, job_id, result, **options)

    monkeypatch.setattr(jobs, "finish", failing_for_the_first)
    runner = JobRunner(JobContext(db, blob_store, registry))
    stop = asyncio.Event()
    serving = asyncio.create_task(runner.run_forever(stop, idle_s=30))

    async def second_succeeded():
        return (await job(db, second)).state is JobState.SUCCEEDED

    await until(second_succeeded)

    waiting = await job(db, first)
    assert (waiting.state, waiting.attempts, waiting.error) == (JobState.RETRY_WAIT, 1, errors[bad])
    assert not serving.done()
    stop.set()
    await asyncio.wait_for(serving, 5)


async def test_a_runner_that_cannot_record_a_failure_stops_with_the_error_rather_than_hide_it(db, registry, runner, monkeypatch):
    handles(registry, raising(RuntimeError("provider busy")))
    await enqueue(db, registry, spec())

    def refusing(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(jobs, "defer", refusing)

    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(runner.run_forever(asyncio.Event(), idle_s=30), 5)


async def looks_for_due_jobs(runner, *, act=lambda: None, over_s=0.3):
    """How often run_forever looks for due jobs over over_s, given nothing to run, after act."""
    calls = []
    run_due = runner.run_due

    async def counted():
        calls.append(time.monotonic())
        return await run_due()

    runner.run_due = counted
    stop = asyncio.Event()
    serving = asyncio.create_task(runner.run_forever(stop, idle_s=30))
    await asyncio.sleep(0.05)
    act()
    await asyncio.sleep(over_s)
    stop.set()
    await asyncio.wait_for(serving, 5)
    return len(calls)


@pytest.mark.parametrize("ending", ["stopped", "cancelled"])
async def test_a_job_an_ended_run_left_running_runs_again_when_the_runner_runs_again(db, registry, runner, ending):
    calls, started = [], asyncio.Event()

    async def run(job, context):
        calls.append(job.id)
        if len(calls) == 1:
            started.set()
            await asyncio.sleep(60)
        return Succeeded()

    handles(registry, run)
    [queued] = await enqueue(db, registry, spec())
    stop = asyncio.Event()
    serving = asyncio.create_task(runner.run_forever(stop, idle_s=30))
    await asyncio.wait_for(started.wait(), 10)
    if ending == "stopped":
        stop.set()
        await asyncio.wait_for(serving, 5)
    else:
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 5)
    assert (await job(db, queued)).state is JobState.RUNNING

    again = asyncio.Event()
    serving = asyncio.create_task(runner.run_forever(again, idle_s=30))

    async def succeeded():
        return (await job(db, queued)).state is JobState.SUCCEEDED

    await until(succeeded)
    again.set()
    await asyncio.wait_for(serving, 5)
    assert ((await job(db, queued)).attempts, calls) == (2, [queued.id, queued.id])


async def test_a_job_no_handler_here_runs_does_not_keep_the_runner_looking(db, registry, runner):
    other = JobRegistry()
    other.handle("summary", Pipeline("summary-1"), returning(Succeeded()))
    await enqueue(db, other, spec(job_type="summary"))

    assert await looks_for_due_jobs(runner) == 1


async def test_one_wake_makes_the_runner_look_once_more(db, runner):
    assert await looks_for_due_jobs(runner, act=runner.wake) == 2


async def test_a_wake_that_comes_while_the_runner_looks_makes_it_look_again(db, runner):
    looks, run_due = [], runner.run_due

    async def woken_while_looking():
        looks.append(time.monotonic())
        ran = await run_due()
        if len(looks) == 1:
            runner.wake()
        return ran

    runner.run_due = woken_while_looking
    stop = asyncio.Event()
    serving = asyncio.create_task(runner.run_forever(stop, idle_s=30))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(serving, 5)

    assert len(looks) == 2


async def test_a_failed_job_retried_by_the_user_runs_again(db, registry, runner):
    results = [Failed("encrypted", OutcomeCode.PASSWORD_REQUIRED), Succeeded()]
    handles(registry, lambda job, context: returning(results.pop(0))(job, context))
    [queued] = await enqueue(db, registry, spec())
    await runner.run_due()

    await db.write(lambda tx: jobs.retry(tx, queued.id, now=NOW))
    await runner.run_due()

    assert (await job(db, queued)).state is JobState.SUCCEEDED


async def test_jobs_of_a_type_without_a_handler_wait_untouched(db, registry, runner):
    handles(registry, returning(Succeeded()))
    other = JobRegistry()
    other.handle("summary", Pipeline("summary-1"), returning(Succeeded()))
    [waiting] = await enqueue(db, other, spec(job_type="summary"))
    [ran] = await enqueue(db, registry, spec())

    assert await runner.run_due() == 1

    assert (await job(db, waiting)).state is JobState.PENDING
    assert (await job(db, ran)).state is JobState.SUCCEEDED


async def test_due_jobs_run_one_at_a_time(db, registry, runner):
    running, overlaps = set(), []

    async def run(job, context):
        overlaps.append(bool(running))
        running.add(job.id)
        await asyncio.sleep(0.01)
        running.discard(job.id)
        return Succeeded()

    handles(registry, run)
    await enqueue(db, registry, spec(1), spec(2), spec(3))

    assert await runner.run_due() == 3
    assert overlaps == [False, False, False]


async def test_a_waiting_job_cancelled_through_the_runner_never_runs(db, registry, runner):
    seen = []
    handles(registry, returning(Succeeded(), seen))
    [queued] = await enqueue(db, registry, spec())

    cancelled = await runner.cancel(queued.id)

    assert cancelled.state is JobState.CANCELLED
    assert await runner.run_due() == 0
    assert seen == []


async def test_a_running_job_cancelled_through_the_runner_is_stopped_and_the_next_job_runs(db, registry, runner):
    started, stopped = asyncio.Event(), []

    async def run(job, context):
        if job.params["n"] == 2:
            return Succeeded()
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            stopped.append(job.id)
            raise
        return Succeeded()

    handles(registry, run)
    first, second = await enqueue(db, registry, spec(1), spec(2))
    running = asyncio.create_task(runner.run_due())
    await asyncio.wait_for(started.wait(), 10)

    cancelled = await asyncio.wait_for(runner.cancel(first.id), 10)

    assert (cancelled.state, stopped) == (JobState.CANCELLED, [first.id])
    assert await asyncio.wait_for(running, 10) == 2
    assert (await job(db, second)).state is JobState.SUCCEEDED


async def test_a_runner_stopped_mid_job_stops_the_handler_and_leaves_the_job_running_for_recovery(db, registry, runner):
    started, stopped = asyncio.Event(), []

    async def run(job, context):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopped.append(job.id)
            raise

    handles(registry, run)
    [queued] = await enqueue(db, registry, spec())
    running = asyncio.create_task(runner.run_due())
    await asyncio.wait_for(started.wait(), 10)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert stopped == [queued.id]
    assert (await job(db, queued)).state is JobState.RUNNING


async def test_running_until_stopped_picks_up_jobs_as_they_come_and_as_they_fall_due(db, blob_store):
    registry = JobRegistry()
    attempts = []

    async def run(job, context):
        attempts.append(job.id)
        if len(attempts) == 1:
            raise RuntimeError("once")
        return Succeeded()

    handles(registry, run)
    runner = JobRunner(JobContext(db, blob_store, registry), retry_delays=(timedelta(milliseconds=200),))
    stop = asyncio.Event()
    serving = asyncio.create_task(runner.run_forever(stop, idle_s=30))
    await asyncio.sleep(0.05)

    [queued] = await db.write(lambda tx: [registry.enqueue(tx, spec())])
    runner.wake()

    async def succeeded():
        while (await job(db, queued)).state is not JobState.SUCCEEDED:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(succeeded(), 10)
    assert attempts == [queued.id, queued.id]
    stop.set()
    await asyncio.wait_for(serving, 10)
