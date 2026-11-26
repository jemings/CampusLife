import subprocess
import sys
from datetime import UTC, datetime

from campuslife_hub import jobs
from campuslife_hub.core import open_core
from campuslife_hub.job_runner import JobContext, JobRegistry, JobRunner
from campuslife_hub.jobs import JobSpec, JobState, Pipeline, Succeeded

CRASH = 70
EXTRACT = Pipeline("extract-1")


def extraction(n):
    return JobSpec("extract.text", f"attachment:{n}", ("a" * 64,), {"n": n})


CRASHING_HUB = f"""
import asyncio, os, sys
from pathlib import Path
from campuslife_hub.core import open_core
from campuslife_hub.job_runner import JobContext, JobRegistry, JobRunner
from campuslife_hub.jobs import JobSpec, Pipeline

async def crash(job, context):
    os._exit({CRASH})

async def main(root):
    registry = JobRegistry()
    registry.handle("extract.text", Pipeline("extract-1"), crash)
    async with open_core(Path(root), create=False, sync_roots=[]) as core:
        await core.db.write(lambda tx: [registry.enqueue(tx, JobSpec("extract.text", f"attachment:{{n}}", ("a" * 64,), {{"n": n}})) for n in (1, 2)])
        await JobRunner(JobContext(core.db, core.blobs, registry)).run_due()

asyncio.run(main(sys.argv[1]))
"""


async def succeeding(job, context):
    return Succeeded()


async def all_jobs(core):
    return await core.db.read(lambda snap: [jobs.get(snap, row[0]) for row in snap.execute("SELECT id FROM processing_jobs ORDER BY rowid")])


async def test_opening_the_core_puts_a_job_left_running_back_to_wait(tmp_path):
    registry = JobRegistry()
    registry.handle("extract.text", EXTRACT, succeeding)
    async with open_core(tmp_path, create=True, sync_roots=[]) as core:
        [queued] = await core.db.write(lambda tx: [registry.enqueue(tx, extraction(1))])
        await core.db.write(lambda tx: jobs.claim(tx, ["extract.text"], now=datetime.now(UTC)))

    async with open_core(tmp_path, create=False, sync_roots=[]) as core:
        recovered = await core.db.read(lambda snap: jobs.get(snap, queued.id))
        assert (recovered.state, recovered.attempts, recovered.error) == (JobState.RETRY_WAIT, 1, jobs.STOPPED_BY_RESTART)


async def test_after_a_crash_mid_job_the_reopened_hub_runs_that_job_and_the_one_still_waiting(tmp_path):
    async with open_core(tmp_path, create=True, sync_roots=[]):
        pass

    crashed = subprocess.run([sys.executable, "-c", CRASHING_HUB, str(tmp_path)], capture_output=True, timeout=120)
    assert crashed.returncode == CRASH, crashed.stderr.decode(errors="replace")

    registry = JobRegistry()
    registry.handle("extract.text", EXTRACT, succeeding)
    async with open_core(tmp_path, create=False, sync_roots=[]) as core:
        interrupted, waiting = await all_jobs(core)
        assert (interrupted.state, interrupted.attempts, interrupted.params) == (JobState.RETRY_WAIT, 1, {"n": 1})
        assert (waiting.state, waiting.attempts) == (JobState.PENDING, 0)

        assert await JobRunner(JobContext(core.db, core.blobs, registry)).run_due() == 2

        assert [(each.state, each.attempts) for each in await all_jobs(core)] == [(JobState.SUCCEEDED, 2), (JobState.SUCCEEDED, 1)]
