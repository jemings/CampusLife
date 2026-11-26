import asyncio
import os
import signal
import sys
import time
import tracemalloc

import pytest

from campuslife_hub import isolate
from campuslife_hub.isolate import SETTLE_SECONDS, STDERR_TAIL_BYTES, STDOUT_MAX_BYTES, RunResult

MiB = 1 << 20
WINDOWS = sys.platform == "win32"


async def run(code, *, stdin=None, timeout_s=60.0, mem_bytes=512 * MiB, **options):
    return await isolate.run([sys.executable, "-c", code], stdin=stdin, timeout_s=timeout_s, mem_bytes=mem_bytes, **options)


def leave_a_marker(marker, *, after_s=1.0):
    """Code that starts a grandchild which writes `marker` after a while, unless it is killed first."""
    grandchild = f"import pathlib, time; time.sleep({after_s}); pathlib.Path({str(marker)!r}).write_text('alive')"
    return f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "


async def appears(path, within_s):
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.05)
    return path.exists()


async def test_a_program_reads_its_stdin_and_its_stdout_and_exit_code_come_back():
    result = await run("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper()); sys.exit(3)", stdin=b"lecture")

    assert result == RunResult(exit_code=3, stdout=b"LECTURE", stderr_tail=b"", timed_out=False, mem_exceeded=False, stdout_exceeded=False)


async def test_without_stdin_a_program_reads_end_of_file():
    result = await run("import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))")

    assert (result.exit_code, result.stdout) == (0, b"0")


async def test_a_program_that_leaves_its_stdin_unread_still_ends():
    result = await run("import sys; sys.exit(0)", stdin=b"x" * (4 * MiB))

    assert result.exit_code == 0


async def test_only_the_tail_of_stderr_is_kept():
    tail = b"y" * STDERR_TAIL_BYTES
    result = await run(f"import sys; sys.stderr.buffer.write(b'x' * {2 * STDERR_TAIL_BYTES} + {tail!r})")

    assert result.stderr_tail == tail


def test_a_program_may_print_16_mib_unless_its_caller_allows_less():
    assert STDOUT_MAX_BYTES == 16 * MiB


async def test_stdout_up_to_its_bound_comes_back_whole():
    result = await run("import sys; sys.stdout.write('x' * 1000)", stdout_max_bytes=1000)

    assert (result.exit_code, result.stdout, result.stdout_exceeded) == (0, b"x" * 1000, False)


async def test_a_program_that_prints_past_its_bound_is_killed_with_what_it_started_and_what_fit_comes_back(tmp_path):
    marker = tmp_path / "grandchild"
    started = time.monotonic()

    result = await run(
        leave_a_marker(marker) + "sys.stdout.write('x' * 1000 + 'y'); sys.stdout.flush(); import time; time.sleep(60)",
        timeout_s=30.0,
        stdout_max_bytes=1000,
    )

    assert time.monotonic() - started < 10
    assert (result.stdout_exceeded, result.timed_out, result.stdout) == (True, False, b"x" * 1000)
    assert result.exit_code != 0
    assert not await appears(marker, within_s=3.0)


async def test_a_program_that_prints_without_end_is_killed_at_the_default_bound_and_the_hub_holds_no_more():
    endless = "import sys\nchunk = b'x' * (1 << 20)\nwhile True:\n    sys.stdout.buffer.write(chunk)"
    tracemalloc.start()
    try:
        result = await isolate.run([sys.executable, "-c", endless], stdin=None, timeout_s=5.0, mem_bytes=256 * MiB)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert (result.stdout_exceeded, len(result.stdout)) == (True, STDOUT_MAX_BYTES)
    assert result.exit_code != 0
    assert peak < 3 * STDOUT_MAX_BYTES


async def test_a_program_past_its_time_is_killed_and_what_it_wrote_comes_back(tmp_path):
    marker = tmp_path / "grandchild"
    started = time.monotonic()

    result = await run(leave_a_marker(marker) + "sys.stdout.write('started'); sys.stdout.flush(); import time; time.sleep(60)", timeout_s=1.0)

    assert time.monotonic() - started < 20
    assert (result.timed_out, result.stdout, result.mem_exceeded) == (True, b"started", False)
    assert result.exit_code != 0
    assert not await appears(marker, within_s=3.0)


async def test_what_a_program_started_ends_with_it(tmp_path):
    marker = tmp_path / "grandchild"
    started = time.monotonic()

    result = await run(leave_a_marker(marker, after_s=3.0) + "print('done')")

    assert time.monotonic() - started < 3.0
    assert (result.exit_code, result.timed_out) == (0, False)
    assert not await appears(marker, within_s=5.0)


async def test_a_crash_is_only_an_exit_code_and_the_next_program_runs():
    crashed = await run("import faulthandler; faulthandler._read_null()")

    assert crashed.exit_code != 0
    assert (crashed.timed_out, crashed.mem_exceeded) == (False, False)
    assert (await run("import sys; sys.stdout.write('ok')")).stdout == b"ok"


async def test_a_program_within_its_memory_limit_runs():
    result = await run("b = bytearray(64 << 20); b[-1] = 1", mem_bytes=256 * MiB)

    assert (result.exit_code, result.mem_exceeded) == (0, False)


async def test_a_program_over_its_memory_limit_is_stopped_and_the_next_program_runs():
    result = await run("b = bytearray(1 << 30); b[-1] = 1", mem_bytes=256 * MiB)

    assert result.exit_code != 0
    assert result.mem_exceeded is WINDOWS
    assert b"MemoryError" in result.stderr_tail
    assert (await run("import sys; sys.stdout.write('ok')")).stdout == b"ok"


async def test_cancelling_a_run_kills_the_program_and_what_it_started(tmp_path):
    began = tmp_path / "began"
    marker = tmp_path / "grandchild"
    task = asyncio.create_task(run(leave_a_marker(marker) + f"import pathlib, time; pathlib.Path({str(began)!r}).write_text('x'); time.sleep(60)"))
    assert await appears(began, within_s=20.0)

    cancelling = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - cancelling < 2 * SETTLE_SECONDS + 1.0
    assert asyncio.all_tasks() == {asyncio.current_task()}
    assert not await appears(marker, within_s=3.0)
    assert (await run("import sys; sys.stdout.write('ok')")).stdout == b"ok"


async def test_a_cancelled_run_ends_only_once_its_program_has_exited(tmp_path, monkeypatch):
    began, processes, create = tmp_path / "began", [], asyncio.create_subprocess_exec

    async def kept(*args, **options):
        processes.append(await create(*args, **options))
        return processes[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", kept)
    task = asyncio.create_task(run(f"import pathlib, time; pathlib.Path({str(began)!r}).write_text('x'); time.sleep(60)"))
    assert await appears(began, within_s=20.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [process] = processes
    assert process.returncode is not None


def leave_the_session(pid_file, *, hold_s=30):
    """Code that starts a grandchild in a session of its own, holding stdout and stderr open for hold_s."""
    grandchild = f"import time; time.sleep({hold_s})"
    return (
        f"import pathlib, subprocess, sys; held = subprocess.Popen([sys.executable, '-c', {grandchild!r}], start_new_session=True); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(held.pid)); "
    )


@pytest.mark.skipif(WINDOWS, reason="only a POSIX process can leave; on Windows the Job Object holds every process started after the program joined it")
@pytest.mark.parametrize("ending", ["print('done')", "import time; time.sleep(60)"], ids=["ends", "times out"])
async def test_a_process_that_left_the_session_and_holds_the_pipes_does_not_hold_up_the_run(tmp_path, ending):
    pid_file, timeout_s = tmp_path / "held", 5.0
    started = time.monotonic()
    try:
        result = await run(leave_the_session(pid_file) + ending, timeout_s=timeout_s)

        assert time.monotonic() - started < timeout_s + 2 * SETTLE_SECONDS + 1.0
        assert result.timed_out is (ending != "print('done')")
        assert asyncio.all_tasks() == {asyncio.current_task()}
    finally:
        if pid_file.exists():
            os.kill(int(pid_file.read_text()), signal.SIGKILL)


async def test_a_program_that_does_not_end_when_killed_fails_the_run_and_is_ended_on_the_way_out(monkeypatch, tmp_path):
    monkeypatch.setattr(isolate._Confinement, "stop", lambda self, process: None)
    marker = tmp_path / "alive"
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="did not end when killed"):
        await run(f"import pathlib, time; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('alive'); time.sleep(60)", timeout_s=0.5)

    assert time.monotonic() - started < 0.5 + 3 * SETTLE_SECONDS + 1.0
    assert asyncio.all_tasks() == {asyncio.current_task()}
    assert not await appears(marker, within_s=5.0)


async def test_a_program_that_is_not_there_is_file_not_found():
    with pytest.raises(FileNotFoundError):
        await isolate.run(["campuslife-no-such-program"], stdin=None, timeout_s=5.0, mem_bytes=256 * MiB)
