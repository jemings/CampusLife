import asyncio
import hashlib
import logging
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import SignedAs
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from campuslife_hub import host, jobs, mutations
from campuslife_hub.core import open_core
from campuslife_hub.db import Database
from campuslife_hub.host import HostConfig, PortTaken
from campuslife_hub.job_runner import JobRegistry, JobRunner
from campuslife_hub.jobs import JobSpec, JobState, Pipeline, Succeeded
from campuslife_hub.redaction import Redactor, file_handler

TOKEN = secrets.token_urlsafe(32)
SIGNED = SignedAs(TOKEN)
EXTRACT = Pipeline("extract-1")
QUICK = {"admin_port": 0, "sync_port": 0, "stop_grace": timedelta(seconds=0.2), "runner_restart_delay": timedelta(seconds=0.05), "runner_idle_s": 0.05}


@asynccontextmanager
async def serving(tmp_path, *, registry=None, redactor=None, config=None, **options):
    """A Hub served on ports of the OS's choosing, a client of its admin listener, and how to stop it."""
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        served = asyncio.create_task(
            host.serve(
                core, registry or JobRegistry(), stop, admin_token=TOKEN, redactor=redactor or Redactor(),
                config=HostConfig(**{**QUICK, **(config or {})}), listening=ready.set_result, **options,
            )
        )
        await asyncio.wait({served, ready}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        if served.done():
            served.result()
        listening = ready.result()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listening.admin[1]}", trust_env=False) as client:
                yield core, client, listening, stop, served
        finally:
            stop.set()
            await asyncio.wait_for(served, 20)


async def until(condition, within_s=10.0):
    deadline = time.monotonic() + within_s
    while not await condition():
        assert time.monotonic() < deadline, "the condition did not come about in time"
        await asyncio.sleep(0.02)


async def test_the_admin_listener_listens_on_127_0_0_1_only_and_leaves_its_port_while_it_serves(tmp_path):
    async with serving(tmp_path) as (core, client, listening, stop, served):
        assert listening.admin[0] == "127.0.0.1"
        assert listening.sync is None
        assert core.layout.admin_port.read_text().strip() == str(listening.admin[1])
        assert (await client.get("/api/status", auth=SIGNED)).json()["hub_id"] == core.identity.hub_id
        assert (await client.get("/api/status")).status_code == 401

        stop.set()
        await asyncio.wait_for(served, 20)

        assert not core.layout.admin_port.exists()
        with pytest.raises(httpx.ConnectError):
            await client.get("/api/status", auth=SIGNED)


async def test_the_sync_app_is_plugged_in_on_its_own_127_0_0_1_port_with_the_hosted_services(tmp_path):
    hosted = []

    def create_sync_app(given):
        hosted.append(given)
        app = FastAPI(openapi_url=None)
        app.get("/v1/ping", response_class=PlainTextResponse)(lambda: "pong")
        return app

    async with serving(tmp_path, sync_app=create_sync_app) as (core, client, listening, stop, served):
        assert listening.sync[0] == "127.0.0.1"
        assert listening.sync[1] != listening.admin[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listening.sync[1]}", trust_env=False) as sync:
            assert (await sync.get("/v1/ping")).text == "pong"
            assert (await sync.get("/api/status", auth=SIGNED)).status_code == 404
        assert (await client.get((await client.post("/api/login", auth=SIGNED)).json()["url"])).status_code == 303
        assert (await client.get("/v1/ping")).status_code == 404
        [given] = hosted
        assert given.core is core


async def test_a_port_another_program_holds_refuses_to_serve_and_the_port_an_earlier_hub_left_is_gone(tmp_path):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    left = tmp_path / "root" / "admin.port"
    left.parent.mkdir()
    left.write_text("9\n")
    try:
        with pytest.raises(PortTaken, match=str(taken.getsockname()[1])):
            async with serving(tmp_path, config={"admin_port": taken.getsockname()[1]}):
                pass
    finally:
        taken.close()
    assert not left.exists()


async def test_the_port_an_earlier_hub_left_is_gone_as_soon_as_the_lock_is_taken(tmp_path, monkeypatch):
    root = tmp_path / "root"
    async with open_core(root, create=True, sync_roots=[]):
        pass
    (root / "admin.port").write_text("9\n")
    opened, left = Database.open, []

    async def opening(layout, **options):
        left.append(layout.admin_port.exists())
        return await opened(layout, **options)

    monkeypatch.setattr(Database, "open", staticmethod(opening))
    async with open_core(root, create=False, sync_roots=[]):
        pass

    assert left == [False]


async def test_a_runner_that_ends_with_an_error_is_logged_counted_and_started_again(tmp_path, monkeypatch, caplog):
    registry = JobRegistry()

    async def raising(job, context):
        raise RuntimeError("provider busy")

    registry.handle("extract.text", EXTRACT, raising)
    defer, refusals = jobs.defer, []

    def refusing_once(*args, **options):
        if not refusals:
            refusals.append(True)
            raise sqlite3.OperationalError("disk I/O error")
        return defer(*args, **options)

    monkeypatch.setattr(jobs, "defer", refusing_once)
    with caplog.at_level(logging.ERROR, logger="campuslife_hub.host"):
        async with serving(tmp_path, registry=registry) as (core, client, *_):
            queued = await core.db.write(lambda tx: registry.enqueue(tx, JobSpec("extract.text", "attachment:1", ("a" * 64,))))

            async def tried_twice():
                job = await core.db.read(lambda snap: jobs.get(snap, queued.id))
                return (job.state, job.attempts) == (JobState.RETRY_WAIT, 2)

            await until(tried_twice)
            status = (await client.get("/api/status", auth=SIGNED)).json()

    assert status["runner"] == {"restarts": 1, "last_error": "OperationalError"}
    assert "the job runner ended with OperationalError" in caplog.text


async def test_a_runner_that_keeps_ending_with_an_error_starts_again_only_after_the_delay(tmp_path, monkeypatch):
    starts = []

    async def ending(self, stop, *, idle_s):
        starts.append(time.monotonic())
        raise RuntimeError("the runner broke")

    monkeypatch.setattr(JobRunner, "run_forever", ending)
    async with serving(tmp_path, config={"runner_restart_delay": timedelta(seconds=0.2)}) as (core, client, *_):

        async def started_thrice():
            return len(starts) >= 3

        await until(started_thrice)
        status = (await client.get("/api/status", auth=SIGNED)).json()

    # asyncio runs a timer up to one clock tick early, and Windows' clock ticks every 15.6 ms.
    slack = 0.01 + 2 * time.get_clock_info("monotonic").resolution
    assert all(later - earlier >= 0.2 - slack for earlier, later in zip(starts, starts[1:], strict=False))
    assert status["runner"]["restarts"] >= 3 and status["runner"]["last_error"] == "RuntimeError"


async def test_maintenance_runs_every_period_with_the_hosts_clock(tmp_path):
    later = datetime.now(UTC) + timedelta(days=91)
    data = b"synthetic slide deck"
    create = {"mutation_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301", "op": "create", "entity": "tag",
              "id": "0f8fad5b-d9cb-469f-a165-70867728950e", "data": {"name": "시험", "color": "#ff8800"}}
    lonely = tmp_path / "lonely.pdf"
    lonely.write_bytes(b"synthetic blob never committed")

    async with serving(tmp_path, config={"maintenance_every": timedelta(seconds=0.05)}, clock=lambda: later) as (core, *_):
        await core.blobs.open_upload(core.hub_device, hashlib.sha256(data).hexdigest(), len(data))
        await core.blobs.put_file(lonely)
        await mutations.apply(core.db, [create], core.hub_device)

        async def all_forgotten():
            counts = await core.db.read(
                lambda snap: [snap.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("upload_sessions", "blobs", "applied_mutations")]
            )
            return counts == [0, 0, 0]

        await until(all_forgotten)


async def test_a_failed_maintenance_round_is_logged_and_the_next_one_runs(tmp_path, caplog):
    rounds = []

    with caplog.at_level(logging.ERROR, logger="campuslife_hub.host"):
        async with serving(tmp_path, config={"maintenance_every": timedelta(seconds=0.05)}) as (core, *_):
            async def failing_once(now):
                rounds.append(now)
                if len(rounds) == 1:
                    raise OSError("disk unplugged")
                return []

            core.blobs.collect_garbage = failing_once

            async def ran_again():
                return len(rounds) >= 2

            await until(ran_again)

    assert "maintenance failed" in caplog.text


async def test_stopping_leaves_behind_a_handler_that_will_not_end_and_its_job_running(tmp_path, caplog):
    started, released = asyncio.Event(), asyncio.Event()

    async def stubborn(job, context):
        started.set()
        while not released.is_set():
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                pass
        return Succeeded()

    registry = JobRegistry()
    registry.handle("extract.text", EXTRACT, stubborn)
    try:
        with caplog.at_level(logging.WARNING, logger="campuslife_hub"):
            async with serving(tmp_path, registry=registry) as (core, client, listening, stop, served):
                queued = await core.db.write(lambda tx: registry.enqueue(tx, JobSpec("extract.text", "attachment:1", ("a" * 64,))))
                await asyncio.wait_for(started.wait(), 10)
                stopping = time.monotonic()

                stop.set()
                await asyncio.wait_for(served, 20)

                assert time.monotonic() - stopping < 5
                assert (await core.db.read(lambda snap: jobs.get(snap, queued.id))).state is JobState.RUNNING
        assert f"the handler of job {queued.id} does not end" in caplog.text
    finally:
        released.set()


async def test_a_login_code_in_the_access_log_and_the_admin_token_are_masked_in_the_log_file(tmp_path):
    redactor = Redactor()
    log_file = tmp_path / "logs" / "hub.log"
    handler = file_handler(log_file, redactor)
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        async with serving(tmp_path, redactor=redactor) as (core, client, *_):
            url = (await client.post("/api/login", auth=SIGNED)).json()["url"]
            assert (await client.get(url)).status_code == 303
            logging.getLogger("campuslife_hub.test").warning("the token was %s", TOKEN)
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
        handler.close()

    written = log_file.read_text(encoding="utf-8")
    code = url.partition("code=")[2]
    assert "GET /login?code=[redacted]" in written
    assert code not in written and TOKEN not in written


def test_run_returns_without_waiting_on_a_task_that_will_not_end():
    program = (
        "import asyncio\n"
        "from campuslife_hub import host\n"
        "async def stubborn():\n"
        "    while True:\n"
        "        try:\n"
        "            await asyncio.sleep(1)\n"
        "        except asyncio.CancelledError:\n"
        "            pass\n"
        "async def main():\n"
        "    asyncio.create_task(stubborn())\n"
        "    await asyncio.sleep(0.1)\n"
        "    return 'done'\n"
        "print(host.run(main()))\n"
    )

    finished = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=60)

    assert (finished.returncode, finished.stdout.strip()) == (0, "done")


def test_run_returns_without_waiting_on_a_thread_a_cancelled_task_left_blocking():
    program = (
        "import asyncio, time\n"
        "from campuslife_hub import host\n"
        "async def main():\n"
        "    blocking = asyncio.create_task(asyncio.to_thread(time.sleep, 60))\n"
        "    await asyncio.sleep(0.2)\n"
        "    blocking.cancel()\n"
        "    return await asyncio.to_thread(lambda: 'done')\n"
        "print(host.run(main()))\n"
    )
    started = time.monotonic()

    finished = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=60)

    assert (finished.returncode, finished.stdout.strip()) == (0, "done")
    assert time.monotonic() - started < 30


async def test_stopping_names_the_threads_a_handler_left_blocking(tmp_path, caplog):
    started, released = threading.Event(), threading.Event()

    async def blocking(job, context):
        await asyncio.to_thread(lambda: (started.set(), released.wait(30)))
        return Succeeded()

    registry = JobRegistry()
    registry.handle("extract.text", EXTRACT, blocking)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(host._DaemonThreads())
    try:
        with caplog.at_level(logging.WARNING, logger="campuslife_hub.host"):
            async with serving(tmp_path, registry=registry) as (core, client, listening, stop, served):
                await core.db.write(lambda tx: registry.enqueue(tx, JobSpec("extract.text", "attachment:1", ("a" * 64,))))
                await asyncio.to_thread(started.wait, 10)

                stop.set()
                await asyncio.wait_for(served, 20)

        assert "1 threads a handler started still run" in caplog.text
    finally:
        released.set()
