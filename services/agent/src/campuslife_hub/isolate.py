"""Run an outside program in its own process, bounded in time, memory and output (D16, E13).

Whatever the program does, crash, hang, allocate or print without end, the Hub gets a
RunResult and carries on. The program and the processes it started end with the run, also
when the task awaiting the run is cancelled. A process outside the program's session (POSIX:
one started in a session of its own) or Job Object (Windows: one started before the program
joined it) is out of reach and lives on; the run does not wait for it, even while it holds
the pipes open.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

STDOUT_MAX_BYTES = 16 << 20
"""The stdout a program may print unless its caller allows less. The Hub holds all of it in memory, so it is
bounded like the program: several times the JSON an extractor prints for a long document, a fraction of the
memory a program itself gets."""
STDERR_TAIL_BYTES = 8192
READ_CHUNK_BYTES = 1 << 16
EXIT_POLL_SECONDS = 0.02
SETTLE_SECONDS = 1.0
"""Once the program is killed: how long its exit is awaited, and then how long its pipes are read."""


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    """Negative on POSIX when a signal ended the program."""
    stdout: bytes
    stderr_tail: bytes
    timed_out: bool
    mem_exceeded: bool
    stdout_exceeded: bool
    """The program printed more than stdout_max_bytes and was killed; stdout holds the first stdout_max_bytes."""


_POSIX_LAUNCHER = (
    "import os, resource, sys\n"
    "limit = int(sys.argv[1])\n"
    "resource.setrlimit(resource.RLIMIT_AS, (limit, limit))\n"
    "os.execv(sys.argv[2], sys.argv[2:])\n"
)


class _ProcessGroup:
    """POSIX: RLIMIT_AS set by a launcher that then execs the program (preexec_fn is unsafe in a threaded Hub),
    and a session of its own so the whole group can be killed. The kernel reports no breach of RLIMIT_AS, so
    mem_exceeded stays False here and a breach shows only as the program failing."""

    options: dict = {"start_new_session": True}

    def __init__(self, mem_bytes: int):
        self._mem_bytes = mem_bytes

    def command(self, argv: list[str]) -> list[str]:
        return [sys.executable, "-I", "-c", _POSIX_LAUNCHER, str(self._mem_bytes), *argv]

    def attach(self, process: asyncio.subprocess.Process) -> None:
        pass

    def stop(self, process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    async def mem_exceeded(self) -> bool:
        return False

    def close(self) -> None:
        pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _CompletionPort(ctypes.Structure):
        _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wintypes.HANDLE)]

    def _declare(name: str, restype: object, *argtypes: object) -> None:
        function = getattr(_kernel32, name)
        function.restype = restype
        function.argtypes = argtypes

    _declare("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    _declare("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _declare("CreateIoCompletionPort", wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD)
    _declare("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _declare("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
    _declare("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _declare("WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    _declare(
        "GetQueuedCompletionStatus", wintypes.BOOL,
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
    )
    _declare("CloseHandle", wintypes.BOOL, wintypes.HANDLE)

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION = 7
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
    _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO = 4
    _JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT = 9
    _JOB_OBJECT_MSG_JOB_MEMORY_LIMIT = 10
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _SYNCHRONIZE = 0x00100000
    _WAIT_OBJECT_0 = 0
    _DRAIN_SECONDS = 5.0

    def _checked(result: object) -> object:
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    class _JobObject:
        """Windows: a Job Object with a per-process memory limit that kills its processes when closed, never shows
        a crash dialog, and posts a limit breach to a completion port. The Hub runs windowless (pythonw), so a
        console program gets no window of its own either."""

        options: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}

        def __init__(self, mem_bytes: int):
            self._job = _checked(_kernel32.CreateJobObjectW(None, None))
            self._port = None
            self._assigned = False
            try:
                limits = _ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = (
                    _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                )
                limits.ProcessMemoryLimit = mem_bytes
                _checked(_kernel32.SetInformationJobObject(self._job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)))
                self._port = _checked(_kernel32.CreateIoCompletionPort(_INVALID_HANDLE_VALUE, None, 0, 1))
                port = _CompletionPort(None, self._port)
                _checked(_kernel32.SetInformationJobObject(self._job, _JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION, ctypes.byref(port), ctypes.sizeof(port)))
            except BaseException:
                self.close()
                raise

        def command(self, argv: list[str]) -> list[str]:
            return argv

        def attach(self, process: asyncio.subprocess.Process) -> None:
            """The program joins the job just after it starts; a process it starts within that instant is not bound."""
            handle = _checked(_kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _SYNCHRONIZE, False, process.pid))
            try:
                if _kernel32.AssignProcessToJobObject(self._job, handle):
                    self._assigned = True
                elif _kernel32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                _kernel32.CloseHandle(handle)

        def stop(self, process: asyncio.subprocess.Process) -> None:
            if self._assigned:
                _kernel32.TerminateJobObject(self._job, 1)
                return
            try:
                process.kill()
            except ProcessLookupError:
                pass

        async def mem_exceeded(self) -> bool:
            if not self._assigned:
                return False
            return await asyncio.to_thread(self._drain)

        def _drain(self) -> bool:
            """Read the port up to the job's last exit; the memory messages come before it."""
            exceeded = False
            message, key, overlapped = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
            deadline = time.monotonic() + _DRAIN_SECONDS
            while time.monotonic() < deadline:
                if not _kernel32.GetQueuedCompletionStatus(self._port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(overlapped), 100):
                    continue
                if message.value in (_JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT, _JOB_OBJECT_MSG_JOB_MEMORY_LIMIT):
                    exceeded = True
                if message.value == _JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO:
                    break
            return exceeded

        def close(self) -> None:
            for handle in (self._job, self._port):
                if handle:
                    _kernel32.CloseHandle(handle)
            self._job = self._port = None

    _Confinement = _JobObject
else:
    _Confinement = _ProcessGroup


async def _feed(pipe: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if pipe is None or data is None:
        return
    try:
        pipe.write(data)
        await pipe.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        pipe.close()


async def _read_up_to(stream: asyncio.StreamReader, into: bytearray, limit: int, exceeded: Callable[[], None]) -> bool:
    """Read stream into `into` until its end; past limit bytes, call exceeded and stop. Whether it went past."""
    while chunk := await stream.read(READ_CHUNK_BYTES):
        room = limit - len(into)
        into += chunk[:room]
        if len(chunk) > room:
            exceeded()
            return True
    return False


async def _read_tail(stream: asyncio.StreamReader, into: bytearray) -> None:
    while chunk := await stream.read(READ_CHUNK_BYTES):
        into += chunk
        del into[:-STDERR_TAIL_BYTES]


async def _ended(process: asyncio.subprocess.Process, within_s: float) -> bool:
    """Whether the program exits within within_s. Process.wait would also wait for the pipes, which a process the
    program started can hold open; returncode is set at the exit."""
    try:
        async with asyncio.timeout(within_s):
            while process.returncode is None:
                await asyncio.sleep(EXIT_POLL_SECONDS)
    except TimeoutError:
        return False
    return True


async def _supervise(
    process: asyncio.subprocess.Process, confinement: _ProcessGroup, stdin: bytes | None, timeout_s: float, stdout_max_bytes: int,
) -> RunResult:
    confinement.attach(process)
    stop = functools.partial(confinement.stop, process)
    stdout, stderr_tail = bytearray(), bytearray()
    reading = asyncio.create_task(_read_up_to(process.stdout, stdout, stdout_max_bytes, stop))
    io = (asyncio.create_task(_feed(process.stdin, stdin)), reading, asyncio.create_task(_read_tail(process.stderr, stderr_tail)))
    try:
        timed_out = not await _ended(process, timeout_s)
        stop()
        if not await _ended(process, SETTLE_SECONDS):
            raise RuntimeError(f"{process.pid} did not end when killed")
        await asyncio.wait(io, timeout=SETTLE_SECONDS)
    finally:
        for task in io:
            task.cancel()
        await asyncio.wait(io)
    for task in io:
        if not task.cancelled():
            task.result()
    stdout_exceeded = not reading.cancelled() and reading.result()
    return RunResult(process.returncode, bytes(stdout), bytes(stderr_tail), timed_out, await confinement.mem_exceeded(), stdout_exceeded)


async def run(
    argv: Sequence[str], *, stdin: bytes | None, timeout_s: float, mem_bytes: int, stdout_max_bytes: int = STDOUT_MAX_BYTES,
) -> RunResult:
    """Run argv with stdin, keeping up to stdout_max_bytes of its stdout and the last STDERR_TAIL_BYTES of its stderr.

    The program and what it started are killed at timeout_s, once it prints past stdout_max_bytes, when it ends,
    and when this is cancelled. RuntimeError when the killed program does not end within SETTLE_SECONDS.
    """
    program = shutil.which(argv[0])
    if program is None:
        raise FileNotFoundError(errno.ENOENT, "no such program", argv[0])
    confinement = _Confinement(mem_bytes)
    try:
        process = await asyncio.create_subprocess_exec(
            *confinement.command([program, *argv[1:]]),
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **confinement.options,
        )
        try:
            return await _supervise(process, confinement, stdin, timeout_s, stdout_max_bytes)
        finally:
            confinement.stop(process)
            # Process has no close of its own; its transport's closes the pipes a process that got away may hold.
            process._transport.close()
            await _ended(process, SETTLE_SECONDS)
    finally:
        confinement.close()
