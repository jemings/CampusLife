"""The job runner (D16, E12, E14): runs due ProcessingJobs one at a time on the Hub's event loop.

A handler is registered per job type with the Pipeline that goes into its job_key and the
time it may take. It returns how the job finished. An exception, a result that cannot be
recorded, or running past its time means "try again later", up to MAX_ATTEMPTS.
Planners are registered by name; the capture.ingest handler, registered in every
JobRegistry, asks them what to run for each committed Capture.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import jobs
from .blobs import BlobStore
from .db import Database, Tx
from .ingest import INGEST, INGEST_PIPELINE, CaptureView, run_ingest
from .jobs import MAX_ATTEMPTS, Failed, Finished, Job, JobSpec, JobState, Pipeline

log = logging.getLogger(__name__)

RETRY_DELAYS = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=1))
"""The wait after each failed attempt; the last one repeats."""

HANDLER_TIMEOUT = timedelta(minutes=30)
"""How long a handler may run when its job type sets no time of its own."""

STOP_GRACE = timedelta(seconds=5)
"""How long a cancelled handler is given to end; one that does not is left running in the background, and its job
is not claimed again until it ends."""

ERROR_MAX_CHARS = 2000

type Handler = Callable[[Job, JobContext], Awaitable[Finished]]
type Planner = Callable[[CaptureView, JobContext], Awaitable[Iterable[JobSpec]]]


class UnknownJobType(LookupError):
    pass


class JobRegistry:
    def __init__(self) -> None:
        self._timeouts: dict[str, timedelta] = {}
        self._handlers: dict[str, tuple[Pipeline, Handler]] = {}
        self._planners: dict[str, Planner] = {}
        self.handle(INGEST, INGEST_PIPELINE, run_ingest)

    def handle(self, job_type: str, pipeline: Pipeline, handler: Handler, *, timeout: timedelta = HANDLER_TIMEOUT) -> None:
        if job_type in self._handlers:
            raise ValueError(f"{job_type} already has a handler")
        self._handlers[job_type] = (pipeline, handler)
        self._timeouts[job_type] = timeout

    @property
    def job_types(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def _entry(self, job_type: str) -> tuple[Pipeline, Handler]:
        try:
            return self._handlers[job_type]
        except KeyError:
            raise UnknownJobType(f"no handler for job type {job_type}") from None

    def pipeline(self, job_type: str) -> Pipeline:
        return self._entry(job_type)[0]

    def handler(self, job_type: str) -> Handler:
        return self._entry(job_type)[1]

    def timeout(self, job_type: str) -> timedelta:
        self._entry(job_type)
        return self._timeouts[job_type]

    def plan(self, name: str, planner: Planner) -> None:
        if name in self._planners:
            raise ValueError(f"a planner named {name} is already registered")
        self._planners[name] = planner

    @property
    def planners(self) -> tuple[tuple[str, Planner], ...]:
        """In the order they were registered."""
        return tuple(self._planners.items())

    def enqueue(self, tx: Tx, spec: JobSpec, *, now: datetime | None = None) -> Job:
        """jobs.enqueue under the Pipeline registered for spec's job type."""
        return jobs.enqueue(tx, spec, self.pipeline(spec.job_type), now=now)


@dataclass(frozen=True)
class JobContext:
    db: Database
    blobs: BlobStore
    registry: JobRegistry


@dataclass
class _Running:
    cancel_requested: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    settled: asyncio.Event = field(default_factory=asyncio.Event)


_NOT_STARTED = object()


async def _called(handler: Handler, job: Job, context: JobContext) -> object:
    """Awaited inside the job's task, so a handler that is not a coroutine fails the job rather than the runner.

    A job cancelled since its claim does not start. Its state is read in a write, which comes after any cancel
    written before this task was registered as running.
    """
    if (await context.db.write(lambda tx: jobs.get(tx, job.id))).state is not JobState.RUNNING:
        return _NOT_STARTED
    return await handler(job, context)


def _result_of(task: asyncio.Task) -> object:
    if task.cancelled():
        raise RuntimeError("the handler ended cancelled")
    return task.result()


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[-ERROR_MAX_CHARS:]


class JobRunner:
    def __init__(
        self,
        context: JobContext,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retry_delays: tuple[timedelta, ...] = RETRY_DELAYS,
        stop_grace: timedelta = STOP_GRACE,
    ) -> None:
        self.context = context
        self._clock = clock
        self._retry_delays = retry_delays
        self._stop_grace = stop_grace
        self._running: dict[str, _Running] = {}
        self._left_running: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()

    async def _end(self, job: Job, task: asyncio.Task) -> None:
        """Cancel the handler and give it stop_grace to end."""
        task.cancel()
        try:
            await asyncio.wait({task}, timeout=self._stop_grace.total_seconds())
        finally:
            if not task.done():
                log.warning("the %s handler of job %s did not stop and is left running", job.job_type, job.id)
                self._left_running[job.id] = task
                task.add_done_callback(lambda _: self._left_ended(job.id))

    def _left_ended(self, job_id: str) -> None:
        del self._left_running[job_id]
        self.wake()

    async def _try_again_later(self, job: Job, error: Exception) -> None:
        db, now = self.context.db, self._clock()
        log.warning("job %s (%s) attempt %d raised %s", job.id, job.job_type, job.attempts, type(error).__name__)
        if job.attempts >= MAX_ATTEMPTS:
            await db.write(lambda tx: jobs.finish(tx, job.id, Failed(_error_text(error)), now=now))
        else:
            delay = self._retry_delays[min(job.attempts, len(self._retry_delays)) - 1]
            await db.write(lambda tx: jobs.defer(tx, job.id, _error_text(error), at=now + delay, now=now))

    async def _settle(self, job: Job, task: asyncio.Task, running: _Running) -> None:
        """Wait for the handler, its time or a cancel, and record what is left to record."""
        timeout = self.context.registry.timeout(job.job_type)
        try:
            await asyncio.wait({task, running.cancel_requested}, timeout=timeout.total_seconds(), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            await self._end(job, task)
            raise
        if running.cancel_requested.done():
            await self._end(job, task)
            return
        if not task.done():
            await self._end(job, task)
            await self._try_again_later(job, TimeoutError(f"the handler ran past {timeout}"))
            return
        try:
            result = _result_of(task)
            if result is _NOT_STARTED:
                return
            await self.context.db.write(lambda tx: jobs.finish(tx, job.id, result, now=self._clock()))
        except Exception as error:
            await self._try_again_later(job, error)

    async def run_one(self) -> Job | None:
        """Claim the job due longest and run it to its end; None when nothing is due."""
        registry, db, held = self.context.registry, self.context.db, frozenset(self._left_running)
        job = await db.write(lambda tx: jobs.claim(tx, registry.job_types, now=self._clock(), skipping=held))
        if job is None:
            return None
        task = asyncio.create_task(_called(registry.handler(job.job_type), job, self.context))
        running = self._running[job.id] = _Running()
        try:
            await self._settle(job, task, running)
        finally:
            del self._running[job.id]
            running.settled.set()
        return await db.read(lambda snap: jobs.get(snap, job.id))

    async def run_due(self) -> int:
        """Run due jobs one after another until none is due; how many ran."""
        ran = 0
        while await self.run_one() is not None:
            ran += 1
        return ran

    async def cancel(self, job_id: str) -> Job:
        """Stop the job if it runs here, within stop_grace, and end it cancelled, as a job that waits; a finished
        job is left as it is."""
        running = self._running.get(job_id)
        if running is not None:
            if not running.cancel_requested.done():
                running.cancel_requested.set_result(None)
            await running.settled.wait()
        return await self.context.db.write(lambda tx: jobs.cancel(tx, job_id, now=self._clock()))

    def wake(self) -> None:
        """Look for due jobs now rather than at the next idle check."""
        self._wake.set()

    async def run_forever(self, stop: asyncio.Event, *, idle_s: float = 5.0) -> None:
        """Run jobs as they come and fall due until stop is set. A job running then is stopped, within stop_grace,
        and left running; the next run_forever, or the Hub opening again, recovers it (E12)."""
        now = self._clock()
        recovered = await self.context.db.write(lambda tx: jobs.recover_after_restart(tx, now=now))
        if recovered:
            log.warning("%d jobs were running when the runner last stopped; they wait again or failed", recovered)
        serving = asyncio.create_task(self._serve(idle_s))
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (serving, stopping):
                task.cancel()
            await asyncio.wait({serving, stopping})
        if not serving.cancelled():
            serving.result()

    async def _serve(self, idle_s: float) -> None:
        registry, db = self.context.registry, self.context.db
        while True:
            self._wake.clear()
            await self.run_due()
            held = frozenset(self._left_running)
            due = await db.read(lambda snap: jobs.next_due(snap, registry.job_types, skipping=held))
            wait = idle_s if due is None else (datetime.fromisoformat(due) - self._clock()).total_seconds()
            try:
                async with asyncio.timeout(min(max(wait, 0.0), idle_s)):
                    await self._wake.wait()
            except TimeoutError:
                pass
