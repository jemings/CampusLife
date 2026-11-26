"""Content-addressed Blob storage (D2, E9).

A file reaches `blobs/<sha[0:2]>/<sha[2:4]>/<sha256>` only after its bytes were
fsynced in `staging/` on the same volume and hashed; the `blobs` row is written
after the file is in place, so a row always has its file. A staging file left by
a dead run is a `*.part` file no row points to.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .clock import now_instant
from .db import Database, Snapshot, Tx

COPY_CHUNK = 1024 * 1024
STAGING_SUFFIX = ".part"
READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
LOCK_RETRY_ATTEMPTS = 8
LOCK_RETRY_FIRST_DELAY_S = 0.05
# ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION: what Windows reports while
# Defender or the indexer holds a file open for a moment.
TRANSIENT_WINERRORS = frozenset({5, 32})


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size: int


@dataclass(frozen=True)
class KeptLeftover:
    path: Path
    reason: str


def _unlink_if_present(path: Path) -> None:
    path.unlink(missing_ok=True)


@dataclass(frozen=True)
class FileOps:
    """The file steps Windows refuses while another handle has the file open, and the wait before trying again."""

    replace: Callable[[Path, Path], None] = os.replace
    unlink: Callable[[Path], None] = _unlink_if_present
    sleep: Callable[[float], None] = time.sleep


def is_sharing_violation(error: OSError) -> bool:
    return getattr(error, "winerror", None) in TRANSIENT_WINERRORS


def with_lock_retry[T](
    fn: Callable[[], T],
    *,
    attempts: int = LOCK_RETRY_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(attempts):
        try:
            return fn()
        except OSError as error:
            if not is_sharing_violation(error) or attempt == attempts - 1:
                raise
            sleep(LOCK_RETRY_FIRST_DELAY_S * 2**attempt)
    raise AssertionError("unreachable: the last attempt returns or raises")


def has_blob(runner: Tx | Snapshot, sha256: str) -> bool:
    return runner.execute("SELECT 1 FROM blobs WHERE sha256 = ?", (sha256,)).fetchone() is not None


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_hashing(src: Path, staging: Path) -> StoredBlob:
    digest = hashlib.sha256()
    size = 0
    with open(src, "rb") as reader, open(staging, "xb") as writer:
        while chunk := reader.read(COPY_CHUNK):
            digest.update(chunk)
            writer.write(chunk)
            size += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return StoredBlob(digest.hexdigest(), size)


class BlobStore:
    def __init__(self, db: Database, *, blobs_dir: Path, staging_dir: Path, files: FileOps = FileOps()):
        self._db = db
        self._blobs_dir = blobs_dir
        self._staging_dir = staging_dir
        self._files = files

    def path_of(self, sha256: str) -> Path:
        return self._blobs_dir / sha256[0:2] / sha256[2:4] / sha256

    def _retry_while_locked[T](self, fn: Callable[[], T]) -> T:
        return with_lock_retry(fn, sleep=self._files.sleep)

    def discard_staging_leftovers(self) -> list[KeptLeftover]:
        """Remove the staging files of dead runs; only the hub.lock holder calls this, so no live run owns one.

        Each leftover is tried once, so startup never waits on a lock. A file another process still holds
        open, or an entry that is not a regular file, stays for the next start and is returned. Any other
        failure to remove a leftover raises.
        """
        kept = []
        for leftover in sorted(self._staging_dir.glob(f"*{STAGING_SUFFIX}")):
            if not leftover.is_file():
                kept.append(KeptLeftover(leftover, "not a regular file"))
                continue
            try:
                self._files.unlink(leftover)
            except OSError as error:
                if not is_sharing_violation(error):
                    raise
                kept.append(KeptLeftover(leftover, f"still in use, left for the next start: {error}"))
        return kept

    def _move_into_place(self, staging: Path, final: Path) -> None:
        """Converges however often it is retried: the final file ends up in place and read-only."""
        if final.exists():
            self._files.unlink(staging)
        else:
            final.parent.mkdir(parents=True, exist_ok=True)
            self._files.replace(staging, final)
            _fsync_directory(final.parent)
        os.chmod(final, READ_ONLY)

    def _store_file(self, src: Path) -> StoredBlob:
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        staging = self._staging_dir / f"{uuid.uuid4()}{STAGING_SUFFIX}"
        try:
            stored = _copy_hashing(src, staging)
            final = self.path_of(stored.sha256)
            self._retry_while_locked(lambda: self._move_into_place(staging, final))
        finally:
            staging.unlink(missing_ok=True)
        return stored

    async def put_file(self, src: Path) -> StoredBlob:
        stored = await asyncio.to_thread(self._store_file, Path(src))
        await self._db.write(
            lambda tx: tx.execute(
                "INSERT INTO blobs (sha256, size, stored_at) VALUES (?, ?, ?) ON CONFLICT (sha256) DO NOTHING",
                (stored.sha256, stored.size, now_instant()),
            )
        )
        return stored
