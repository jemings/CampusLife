"""One Hub per data root (E11): an OS lock on `<root>/hub.lock`.

The OS drops the lock when its holder dies, so a lock file left behind is stale
by construction and the next run takes it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Self


class AlreadyRunning(Exception):
    def __init__(self, lock_file: Path):
        super().__init__(f"another CampusLife Hub holds {lock_file}; stop it first")
        self.lock_file = lock_file


class LockNotHeld(Exception):
    def __init__(self, lock_file: Path):
        super().__init__(f"only the holder of {lock_file} may do this")
        self.lock_file = lock_file


def require_held(lock: object, lock_file: Path) -> HubLock:
    """`lock` itself when it is an unreleased HubLock on `lock_file`, else LockNotHeld."""
    if isinstance(lock, HubLock) and lock.holds(lock_file):
        return lock
    raise LockNotHeld(lock_file)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class HubLock:
    """Proof that this process holds a data root's hub.lock; the writer database asks for one."""

    def __init__(self, fd: int, lock_file: Path):
        self._fd: int | None = fd
        self.lock_file = lock_file

    @classmethod
    def acquire(cls, lock_file: str | os.PathLike[str]) -> HubLock:
        path = Path(lock_file)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(fd):
            os.close(fd)
            raise AlreadyRunning(path)
        return cls(fd, path.resolve())

    def holds(self, lock_file: str | os.PathLike[str]) -> bool:
        return self._fd is not None and self.lock_file == Path(lock_file).resolve()

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
