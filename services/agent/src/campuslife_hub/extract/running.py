"""Running a child and judging what it printed (S3, S4, decision 6): the only caller of isolate.run for extractors.

A child that breaks a limit (time, memory, output) or reports unsafe_input fails the job with unsafe_input, and a
retry would break it again, so none follows. corrupt, password_required and unsupported fail it with that code;
needs_conversion blocks it. A child that exits without a result crashed: the job is tried again, once, since a
second crash is the input's doing. A result that is not what the protocol says is the extractor's mistake and
fails the job.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import isolate
from ..jobs import Blocked, Failed, Job, OutcomeCode
from .limits import Limits
from .protocol import EXIT_MEMORY, Status

CHILD = (sys.executable, "-I", "-m", "campuslife_hub.extract.child")
CRASHES_BEFORE_FAILING = 2
WINDOWS_ENV = ("SYSTEMROOT", "WINDIR")
"""What a Windows child needs to start; TEMP and TMP point into its work directory. Nothing else of the Hub's env
is passed."""

REFUSED = {
    Status.CORRUPT: OutcomeCode.CORRUPT,
    Status.PASSWORD_REQUIRED: OutcomeCode.PASSWORD_REQUIRED,
    Status.UNSUPPORTED: OutcomeCode.UNSUPPORTED,
    Status.UNSAFE_INPUT: OutcomeCode.UNSAFE_INPUT,
}


class ChildCrashed(Exception):
    """The child ended without a result; the runner tries the job again."""


@dataclass(frozen=True)
class Answer:
    """A result the child gave with status ok or partial."""

    result: dict
    partial: bool


type Judged = Answer | Failed | Blocked


def child_env(work: Path) -> dict[str, str]:
    if sys.platform == "win32":
        return {**{name: os.environ[name] for name in WINDOWS_ENV if name in os.environ}, "TEMP": str(work), "TMP": str(work)}
    return {"TMPDIR": str(work)}


def unsafe(reason: str) -> Failed:
    return Failed(reason, OutcomeCode.UNSAFE_INPUT)


async def run_child(job: Job, request: Mapping[str, object], limits: Limits, work: Path) -> Judged:
    """Run the child on request in work, and judge it; ChildCrashed to try again."""
    work.mkdir(parents=True, exist_ok=True)
    ran = await isolate.run(
        CHILD, stdin=json.dumps({**request, "limits": limits.to_json()}).encode("utf-8"), timeout_s=limits.timeout_s,
        mem_bytes=limits.mem_bytes, stdout_max_bytes=limits.stdout_max_bytes, cwd=work, env=child_env(work),
    )
    if ran.timed_out:
        return unsafe(f"the child ran past {limits.timeout_s}s")
    if ran.mem_exceeded or ran.exit_code == EXIT_MEMORY:
        return unsafe(f"the child needed more than {limits.mem_bytes} bytes of memory")
    if ran.stdout_exceeded:
        return unsafe(f"the child printed more than {limits.stdout_max_bytes} bytes")
    if ran.exit_code != 0 or not ran.stdout:
        error = f"the child exited {ran.exit_code} without a result: {ran.stderr_tail.decode('utf-8', 'replace')[-500:]}"
        if job.attempts >= CRASHES_BEFORE_FAILING:
            return Failed(error)
        raise ChildCrashed(error)
    try:
        result = json.loads(ran.stdout)
        status = Status(result["status"])
    except (ValueError, TypeError, KeyError) as error:
        return Failed(f"the child's result is not the protocol's: {type(error).__name__}: {error}")
    if status in (Status.OK, Status.PARTIAL):
        return Answer(result, status is Status.PARTIAL)
    reason = str(result.get("reason", ""))
    if status is Status.NEEDS_CONVERSION:
        return Blocked(OutcomeCode.NEEDS_CONVERSION, reason)
    return Failed(reason, REFUSED[status])
