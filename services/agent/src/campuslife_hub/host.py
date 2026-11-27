"""The Hub process host (D16, E17): one process, one event loop, serving until it is told to stop.

`serve` runs over an open Core:
- the admin listener, the local UI and admin API, on 127.0.0.1:admin_port; it is never put
  behind tailscale serve, and its port is left in admin.port for the CLI;
- the sync listener, the FastAPI app #4 makes through sync_app, on 127.0.0.1:sync_port;
- the job runner, started again after runner_restart_delay when it ends with an error, each end
  logged and counted in the status;
- maintenance every maintenance_every: upload session expiry, Blob collection, forgetting
  old Mutation results and finishing unfinished purges, which opening the Core also does once.
Stopping closes the listeners, stops the runner, ends the handlers left running and leaves behind
those that will not end. `run` then lets the process exit without waiting on them, nor on a
thread a cancelled handler started with asyncio.to_thread, which runs on to its end: the loop's
executor runs each such call on a daemon thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import APIRouter, FastAPI
from starlette.types import ASGIApp

from . import admin_token, purge
from .admin import Admin, create_app
from .admin_auth import AdminAuth
from .backup import BackupSettings
from .core import Core, open_core, safe_data_root
from .data_root import Layout
from .job_runner import STOP_GRACE, JobContext, JobRegistry, JobRunner
from .mutations import forget_old_results
from .redaction import RedactingFormatter, Redactor, file_handler

log = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
DEFAULT_SYNC_PORT = 8730
DEFAULT_ADMIN_PORT = 8731
LOG_FILE = "hub.log"
WORK_THREAD = "campuslife-hub-work"


@dataclass(frozen=True)
class HostConfig:
    admin_port: int = DEFAULT_ADMIN_PORT
    sync_port: int = DEFAULT_SYNC_PORT
    maintenance_every: timedelta = timedelta(hours=1)
    backup: BackupSettings = BackupSettings()
    runner_restart_delay: timedelta = timedelta(seconds=10)
    runner_idle_s: float = 5.0
    stop_grace: timedelta = STOP_GRACE
    """How long a cancelled handler, and then the open requests of a listener, are given to end."""


@dataclass(frozen=True)
class Hosted:
    """What a listener plugged into the host works with: the sync app #4 makes from it."""

    core: Core
    runner: JobRunner
    redactor: Redactor


type SyncAppFactory = Callable[[Hosted], FastAPI]


@dataclass(frozen=True)
class Serving:
    """Where the listeners took their sockets, once they listen."""

    admin: tuple[str, int]
    sync: tuple[str, int] | None


class PortTaken(Exception):
    pass


@dataclass
class RunnerRecord:
    """Each time the job runner ended with an error and was started again."""

    restarts: int = 0
    last_error: str | None = None

    def report(self) -> dict[str, object]:
        return {"runner": {"restarts": self.restarts, "last_error": self.last_error}}


class _Listener(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        """The host owns the process's signals; see stop_on_signals."""
        yield


def _bind(port: int) -> socket.socket:
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            listening.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listening.bind((LOOPBACK, port))
    except OSError as error:
        listening.close()
        raise PortTaken(f"{LOOPBACK}:{port} cannot be listened on ({error}); choose another port") from None
    listening.setblocking(False)
    return listening


def _listener(app: ASGIApp, config: HostConfig) -> _Listener:
    return _Listener(
        uvicorn.Config(
            app,
            http="h11",
            ws="none",
            lifespan="off",
            log_config=None,
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=max(1, round(config.stop_grace.total_seconds())),
        )
    )


async def _stopped_within(stopping: asyncio.Event, wait: timedelta) -> bool:
    try:
        async with asyncio.timeout(wait.total_seconds()):
            await stopping.wait()
    except TimeoutError:
        return False
    return True


async def _keep_running(runner: JobRunner, stopping: asyncio.Event, record: RunnerRecord, config: HostConfig) -> None:
    while not stopping.is_set():
        try:
            await runner.run_forever(stopping, idle_s=config.runner_idle_s)
        except Exception as error:
            record.restarts += 1
            record.last_error = type(error).__name__
            log.exception("the job runner ended with %s; it starts again in %s", type(error).__name__, config.runner_restart_delay)
            await _stopped_within(stopping, config.runner_restart_delay)


async def _maintain(core: Core, stopping: asyncio.Event, every: timedelta, clock: Callable[[], datetime]) -> None:
    while not await _stopped_within(stopping, every):
        now = clock()
        try:
            kept = await core.blobs.expire_sessions(now) + await core.blobs.collect_garbage(now)
            await forget_old_results(core.db, now)
            for finish in await purge.finish_unfinished(core.db, core.blobs, core.purge_hooks):
                kept += finish.kept
        except Exception:
            log.exception("maintenance failed; it runs again in %s", every)
            continue
        for leftover in kept:
            log.warning("left %s for a later clean-up: %s", leftover.path, leftover.reason)


def _raise_if_any_ended(tasks: Sequence[asyncio.Task]) -> None:
    for task in tasks:
        if task.done():
            task.result()
            raise RuntimeError("a listener ended while the Hub was serving")


def _leave_port(path: Path, port: int) -> None:
    staged = path.with_name(f"{path.name}.new")
    staged.write_text(f"{port}\n", encoding="ascii")
    os.replace(staged, path)


async def serve(
    core: Core,
    registry: JobRegistry,
    stop: asyncio.Event,
    *,
    admin_token: str,
    redactor: Redactor,
    config: HostConfig = HostConfig(),
    sync_app: SyncAppFactory | None = None,
    admin_routers: Sequence[APIRouter] = (),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    listening: Callable[[Serving], None] = lambda serving: None,
) -> None:
    """Serve until stop is set; raises when a listener ends on its own, after stopping the rest."""
    runner = JobRunner(JobContext(core.db, core.blobs, registry), clock=clock, stop_grace=config.stop_grace)
    record = RunnerRecord()
    stopping = asyncio.Event()
    admin = Admin(
        core, runner, AdminAuth(admin_token, redactor, clock=clock), redactor, status=record.report, stop=stop.set,
        backup_settings=config.backup, clock=clock,
    )
    sockets = [_bind(config.admin_port)]
    tasks: list[asyncio.Task] = []
    listeners: list[_Listener] = []
    try:
        admin_port = sockets[0].getsockname()[1]
        apps = [create_app(admin, port=admin_port, routers=admin_routers)]
        if sync_app is not None:
            sockets.append(_bind(config.sync_port))
            apps.append(sync_app(Hosted(core, runner, redactor)))
        listeners = [_listener(app, config) for app in apps]
        tasks = [asyncio.create_task(listener.serve(sockets=[bound])) for listener, bound in zip(listeners, sockets, strict=True)]
        tasks += [
            asyncio.create_task(_keep_running(runner, stopping, record, config)),
            asyncio.create_task(_maintain(core, stopping, config.maintenance_every, clock)),
        ]
        while not all(listener.started for listener in listeners):
            _raise_if_any_ended(tasks)
            await asyncio.sleep(0.01)
        _leave_port(core.layout.admin_port, admin_port)
        listening(Serving(sockets[0].getsockname()[:2], sockets[1].getsockname()[:2] if sync_app is not None else None))
        log.info("serving the admin listener on http://%s:%d", LOOPBACK, admin_port)
        stopped = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({stopped, *tasks}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
        if not stop.is_set():
            _raise_if_any_ended(tasks)
    finally:
        stopping.set()
        for listener in listeners:
            listener.should_exit = True
        if tasks:
            await asyncio.wait(tasks)
        for left in await runner.end_left_running():
            log.warning("the handler of job %s does not end; the Hub stops without it", left)
        if threads := sum(thread.name == WORK_THREAD for thread in threading.enumerate()):
            log.warning("%d threads a handler started still run; the Hub stops without them", threads)
        with contextlib.suppress(FileNotFoundError):
            core.layout.admin_port.unlink()
        for bound in sockets:
            bound.close()


@contextlib.contextmanager
def stop_on_signals(stop: asyncio.Event) -> Iterator[None]:
    """While inside, Ctrl+C, a terminate request and, on Windows, Ctrl+Break set stop."""
    loop = asyncio.get_running_loop()
    numbers = [number for name in ("SIGINT", "SIGTERM", "SIGBREAK") if (number := getattr(signal, name, None)) is not None]
    before = {number: signal.signal(number, lambda *_: loop.call_soon_threadsafe(stop.set)) for number in numbers}
    try:
        yield
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)


async def run_hub(root: Path, config: HostConfig = HostConfig(), *, registry: JobRegistry | None = None, sync_app: SyncAppFactory | None = None) -> None:
    """The Hub at root, serving until a signal stops it, with its log file in root/logs."""
    layout = Layout(safe_data_root(root))
    redactor = Redactor()
    handlers = [file_handler(layout.logs / LOG_FILE, redactor)]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
        handlers[-1].setFormatter(RedactingFormatter(redactor, "%(levelname)s %(name)s: %(message)s"))
    root_logger, level = logging.getLogger(), logging.getLogger().level
    for handler in handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    try:
        async with open_core(layout.root, create=False) as core:
            token = admin_token.load_or_create(core.layout.admin_token)
            stop = asyncio.Event()
            with stop_on_signals(stop):
                await serve(core, registry or JobRegistry(), stop, admin_token=token, redactor=redactor, config=config, sync_app=sync_app)
    finally:
        root_logger.setLevel(level)
        for handler in handlers:
            root_logger.removeHandler(handler)
            handler.close()


class _DaemonThreads(concurrent.futures.ThreadPoolExecutor):
    """Each call on a daemon thread of its own, as the pool's threads are waited on at exit; a loop takes only a
    ThreadPoolExecutor as its default executor."""

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()

        def work() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

        threading.Thread(target=work, name=WORK_THREAD, daemon=True).start()
        return future


def run[T](main: Coroutine[object, object, T]) -> T:
    """Run main on a new event loop and close it without waiting on tasks main left behind, such as a handler that
    will not end (asyncio.run would wait on them for ever), or on threads they started."""
    loop = asyncio.new_event_loop()
    loop.set_default_executor(_DaemonThreads())
    try:
        return loop.run_until_complete(main)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
