import hashlib
import subprocess
import sys
from datetime import UTC, datetime

from campuslife_hub import artifacts, jobs
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


CRASHING_BEFORE_FINISH = f"""
import asyncio, os, sys
from pathlib import Path
from campuslife_hub import artifacts, jobs
from campuslife_hub.core import open_core
from campuslife_hub.job_runner import JobContext, JobRegistry, JobRunner
from campuslife_hub.jobs import JobSpec, Pipeline, Succeeded

async def summarize(job, context):
    made = Path(sys.argv[2]) / "first.txt"
    made.write_bytes(b"first run")
    content = await context.blobs.put_file(made)
    await context.db.write(lambda tx: artifacts.record(tx, job, "summary", content.sha256))
    return Succeeded()

def crash(*args, **kwargs):
    os._exit({CRASH})

async def main(root):
    registry = JobRegistry()
    registry.handle("summarize", Pipeline("summary-1"), summarize)
    async with open_core(Path(root), create=False, sync_roots=[]) as core:
        await core.db.write(lambda tx: registry.enqueue(tx, JobSpec("summarize", "attachment:0f8fad5b-d9cb-469f-a165-70867728950e", ("a" * 64,), {{}})))
        jobs.finish = crash
        await JobRunner(JobContext(core.db, core.blobs, registry)).run_due()

asyncio.run(main(sys.argv[1]))
"""


async def test_a_job_run_again_after_a_crash_between_its_handler_and_finish_gets_its_first_artifact_back(tmp_path):
    root = tmp_path / "hub"
    async with open_core(root, create=True, sync_roots=[]):
        pass

    crashed = subprocess.run([sys.executable, "-c", CRASHING_BEFORE_FINISH, str(root), str(tmp_path)], capture_output=True, timeout=120)
    assert crashed.returncode == CRASH, crashed.stderr.decode(errors="replace")

    recorded = []

    async def summarize_again(job, context):
        made = tmp_path / "second.txt"
        made.write_bytes(b"second run")
        content = await context.blobs.put_file(made)
        recorded.append(await context.db.write(lambda tx: artifacts.record(tx, job, "summary", content.sha256)))
        return Succeeded()

    registry = JobRegistry()
    registry.handle("summarize", Pipeline("summary-1"), summarize_again)
    async with open_core(root, create=False, sync_roots=[]) as core:
        [interrupted] = await all_jobs(core)
        first = await core.db.read(lambda snap: artifacts.made_by(snap, interrupted.job_key, "summary"))
        assert (interrupted.state, first["content_blob"]) == (JobState.RETRY_WAIT, hashlib.sha256(b"first run").hexdigest())

        assert await JobRunner(JobContext(core.db, core.blobs, registry)).run_due() == 1

        assert [(each.state, each.attempts) for each in await all_jobs(core)] == [(JobState.SUCCEEDED, 2)]
        assert recorded == [first]
        assert await core.db.read(lambda snap: snap.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == 1
