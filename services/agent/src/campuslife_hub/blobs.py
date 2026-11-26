"""Content-addressed Blob storage (D2, E9) and the upload sessions that fill it (D7, E19).

A file reaches `blobs/<sha[0:2]>/<sha[2:4]>/<sha256>` only after its bytes were
fsynced in `staging/` on the same volume and hashed; the `blobs` row is written
after the file is in place, so a row always has its file. A staging file left by
a dead run is a `*.part` file no row points to.

A session's chunks reach `uploads/<session id>/<index>` the same way, whole, so a
chunk file is always complete. A chunk is placed outside the database writer and
recorded after; one whose session ended meanwhile lies in a directory no session
row owns, which the next sweep removes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import stat
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

from campuslife_contracts import validate

from .clock import instant, now_instant
from .db import Database, Snapshot, Tx
from .devices import DeviceId

COPY_CHUNK = 1024 * 1024
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
SESSION_LIFETIME = timedelta(days=7)
STAGING_SUFFIX = ".part"
CHUNK_NAME = re.compile(r"0|[1-9][0-9]*")
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

    def body(self) -> dict[str, object]:
        """blob/stored."""
        return {"sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class UploadSession:
    sha256: str
    size: int
    chunk_size: int
    received: tuple[int, ...]

    def body(self) -> dict[str, object]:
        """blob/upload_session."""
        return {"sha256": self.sha256, "size": self.size, "chunk_size": self.chunk_size, "received": list(self.received)}


@dataclass(frozen=True)
class KeptLeftover:
    path: Path
    reason: str


class UploadRefused(Exception):
    """An upload step the sync port refuses; `body()` is its common/error."""

    code: ClassVar[str]

    def __init__(self, message: str, details: dict[str, object] | None = None):
        super().__init__(message)
        self.details = details

    def body(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            error["details"] = self.details
        return error


class InvalidUpload(UploadRefused):
    code = "invalid_request"


class NoUploadSession(UploadRefused):
    code = "not_found"


class ChunkMismatch(UploadRefused):
    code = "chunk_mismatch"


class HashMismatch(UploadRefused):
    code = "hash_mismatch"


class Incomplete(UploadRefused):
    code = "incomplete"

    def __init__(self, missing: list[int]):
        super().__init__(f"{len(missing)} chunks are missing", {"missing_chunks": missing})


def _refuse_outside_contract(schema_id: str, instance: object) -> None:
    violations = validate(schema_id, instance)
    if violations:
        raise InvalidUpload(f"not a {schema_id}: " + "; ".join(map(str, violations)))


@dataclass(frozen=True)
class _Session:
    id: str
    size: int
    chunk_size: int

    @property
    def chunk_count(self) -> int:
        return -(-self.size // self.chunk_size)

    def chunk_length(self, index: int) -> int:
        return min(self.chunk_size, self.size - index * self.chunk_size)


def _open_session(runner: Tx | Snapshot, device: DeviceId, sha256: str) -> _Session | None:
    row = runner.execute("SELECT id, size, chunk_size FROM upload_sessions WHERE device_id = ? AND sha256 = ?", (device, sha256)).fetchone()
    return None if row is None else _Session(*row)


def _stored_blob(runner: Tx | Snapshot, sha256: str) -> StoredBlob | None:
    row = runner.execute("SELECT size FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
    return None if row is None else StoredBlob(sha256, row[0])


def _end_sessions(tx: Tx, where: str, params: Sequence[object]) -> list[str]:
    return [session_id for (session_id,) in tx.execute(f"DELETE FROM upload_sessions WHERE {where} RETURNING id", params)]


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


def _copy_hashing(sources: Sequence[Path], staging: Path) -> StoredBlob:
    digest = hashlib.sha256()
    size = 0
    with open(staging, "xb") as writer:
        for src in sources:
            with open(src, "rb") as reader:
                while chunk := reader.read(COPY_CHUNK):
                    digest.update(chunk)
                    writer.write(chunk)
                    size += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return StoredBlob(digest.hexdigest(), size)


def _free_space_at(path: Path) -> int:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


class BlobStore:
    def __init__(
        self,
        db: Database,
        *,
        blobs_dir: Path,
        staging_dir: Path,
        uploads_dir: Path,
        files: FileOps = FileOps(),
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        free_space: Callable[[Path], int] = _free_space_at,
    ):
        self._db = db
        self._blobs_dir = blobs_dir
        self._staging_dir = staging_dir
        self._uploads_dir = uploads_dir
        self._files = files
        self._chunk_size = chunk_size
        self._free_space = free_space

    def path_of(self, sha256: str) -> Path:
        return self._blobs_dir / sha256[0:2] / sha256[2:4] / sha256

    def _retry_while_locked[T](self, fn: Callable[[], T]) -> T:
        return with_lock_retry(fn, sleep=self._files.sleep)

    def _unlink_once(self, path: Path) -> list[KeptLeftover]:
        """Remove a file nobody needs, trying once.

        A file another process holds open, or an entry that is not a regular file, stays for the next
        clean-up and is returned. Any other failure raises.
        """
        if not path.is_file():
            return [KeptLeftover(path, "not a regular file")]
        try:
            self._files.unlink(path)
        except OSError as error:
            if not is_sharing_violation(error):
                raise
            return [KeptLeftover(path, f"still in use, left for the next clean-up: {error}")]
        return []

    def discard_staging_leftovers(self) -> list[KeptLeftover]:
        """Remove the staging files of dead runs; only the hub.lock holder calls this, so no live run owns one.

        Each leftover is tried once, so startup never waits on a lock.
        """
        kept = []
        for leftover in sorted(self._staging_dir.glob(f"*{STAGING_SUFFIX}")):
            kept += self._unlink_once(leftover)
        return kept

    def _discard_quietly(self, path: Path) -> None:
        """One try; a file that stays is a staging leftover the next start removes or reports."""
        with contextlib.suppress(OSError):
            self._files.unlink(path)

    def _new_staging_path(self) -> Path:
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        return self._staging_dir / f"{uuid.uuid4()}{STAGING_SUFFIX}"

    def _move_into_place(self, staging: Path, final: Path) -> None:
        """Converges however often it is retried: the final file ends up in place and read-only."""
        if final.exists():
            self._files.unlink(staging)
        else:
            final.parent.mkdir(parents=True, exist_ok=True)
            self._files.replace(staging, final)
            _fsync_directory(final.parent)
        os.chmod(final, READ_ONLY)

    def _store_from(self, sources: Sequence[Path], expect: StoredBlob | None) -> StoredBlob:
        staging = self._new_staging_path()
        try:
            stored = _copy_hashing(sources, staging)
            if expect is not None and stored != expect:
                raise HashMismatch(f"the bytes are {stored.sha256} and {stored.size} bytes long, not {expect.sha256} and {expect.size}")
            final = self.path_of(stored.sha256)
            self._retry_while_locked(lambda: self._move_into_place(staging, final))
        except BaseException:
            self._discard_quietly(staging)
            raise
        return stored

    async def _store_and_register(self, sources: Sequence[Path], expect: StoredBlob | None = None, *, ending: str | None = None) -> StoredBlob:
        stored = await asyncio.to_thread(self._store_from, sources, expect)

        def register(tx: Tx) -> None:
            tx.execute(
                "INSERT INTO blobs (sha256, size, stored_at) VALUES (?, ?, ?) ON CONFLICT (sha256) DO NOTHING",
                (stored.sha256, stored.size, now_instant()),
            )
            if ending is not None:
                _end_sessions(tx, "id = ?", (ending,))

        await self._db.write(register)
        return stored

    async def put_file(self, src: Path) -> StoredBlob:
        return await self._store_and_register([Path(src)])

    def _session_dir(self, session_id: str) -> Path:
        return self._uploads_dir / session_id

    def _received(self, session: _Session) -> tuple[int, ...]:
        directory = self._session_dir(session.id)
        names = [path.name for path in directory.iterdir()] if directory.is_dir() else []
        return tuple(sorted(index for index in (int(name) for name in names if CHUNK_NAME.fullmatch(name)) if index < session.chunk_count))

    def _remove_session_dir(self, directory: Path) -> list[KeptLeftover]:
        """Each file is tried once; what stays is an orphan the next sweep removes or reports."""
        if not directory.exists():
            return []
        if not directory.is_dir():
            return [KeptLeftover(directory, "not an upload session directory")]
        kept = []
        for path in sorted(directory.iterdir()):
            kept += self._unlink_once(path)
        if kept:
            return kept
        try:
            directory.rmdir()
        except OSError as error:
            if not is_sharing_violation(error):
                raise
            return [KeptLeftover(directory, f"still in use, left for the next clean-up: {error}")]
        return []

    async def _discard_sessions(self, session_ids: Sequence[str]) -> list[KeptLeftover]:
        kept = []
        for session_id in session_ids:
            kept += await asyncio.to_thread(self._remove_session_dir, self._session_dir(session_id))
        return kept

    async def open_upload(self, device: DeviceId, sha256: str, size: int) -> StoredBlob | UploadSession:
        """Open or resume the device's upload of one Blob (D7, E19); a Blob already stored needs none.

        A new upload must fit in the space free on the data root's volume when it opens.
        """
        _refuse_outside_contract("blob/upload_request", {"sha256": sha256, "size": size})

        def open_or_resume(tx: Tx) -> StoredBlob | UploadSession:
            stored = _stored_blob(tx, sha256)
            if stored is not None:
                if stored.size != size:
                    raise InvalidUpload(f"the stored Blob {sha256} is {stored.size} bytes, not {size}")
                return stored
            session = _open_session(tx, device, sha256)
            if session is None:
                free = self._free_space(self._uploads_dir)
                if size > free:
                    raise InvalidUpload(f"{size} bytes do not fit in the {free} bytes free on the data root's volume")
                session = _Session(str(uuid.uuid4()), size, self._chunk_size)
                tx.execute(
                    "INSERT INTO upload_sessions (id, device_id, sha256, size, chunk_size, touched_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session.id, device, sha256, size, session.chunk_size, now_instant()),
                )
            elif session.size != size:
                raise InvalidUpload(f"the open upload of {sha256} is {session.size} bytes, not {size}")
            else:
                tx.execute("UPDATE upload_sessions SET touched_at = ? WHERE id = ?", (now_instant(), session.id))
            return UploadSession(sha256, size, session.chunk_size, self._received(session))

        return await self._db.write(open_or_resume)

    def _write_staging(self, data: bytes) -> Path:
        staging = self._new_staging_path()
        with open(staging, "xb") as writer:
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
        return staging

    def _place_chunk(self, chunk: Path, data: bytes) -> None:
        """Converges however often the chunk is sent: the file ends up holding `data`, unless it holds other bytes."""
        if chunk.exists():
            if chunk.read_bytes() != data:
                raise ChunkMismatch(f"other bytes are already stored as chunk {chunk.name}")
            return
        staging = self._write_staging(data)
        try:
            chunk.parent.mkdir(parents=True, exist_ok=True)
            self._retry_while_locked(lambda: self._files.replace(staging, chunk))
            _fsync_directory(chunk.parent)
        except BaseException:
            self._discard_quietly(staging)
            raise

    async def put_chunk(self, device: DeviceId, sha256: str, index: int, data: bytes) -> None:
        """Store chunk `index` of the device's open upload of `sha256`; the same bytes again change nothing (E19)."""
        _refuse_outside_contract("common/sha256", sha256)
        session = await self._db.read(lambda snap: _open_session(snap, device, sha256))
        if session is None:
            raise NoUploadSession(f"this device has no open upload of {sha256}")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < session.chunk_count:
            raise InvalidUpload(f"chunk {index!r} is not one of the {session.chunk_count} chunks of this upload")
        if len(data) != session.chunk_length(index):
            raise InvalidUpload(f"chunk {index} is {session.chunk_length(index)} bytes, not {len(data)}")
        await asyncio.to_thread(self._place_chunk, self._session_dir(session.id) / str(index), data)

        def record(tx: Tx) -> None:
            if tx.execute("UPDATE upload_sessions SET touched_at = ? WHERE id = ?", (now_instant(), session.id)).rowcount == 0:
                raise NoUploadSession(f"the upload of {sha256} ended while chunk {index} was stored")

        await self._db.write(record)

    async def finalize(self, device: DeviceId, sha256: str) -> StoredBlob:
        """Combine the device's chunks of `sha256` and store the Blob (D7); a Blob already stored answers at once."""
        _refuse_outside_contract("common/sha256", sha256)
        stored, session = await self._db.read(lambda snap: (_stored_blob(snap, sha256), _open_session(snap, device, sha256)))
        if stored is not None:
            if session is not None:
                await self._db.write(lambda tx: _end_sessions(tx, "id = ?", (session.id,)))
                await self._discard_sessions([session.id])
            return stored
        if session is None:
            raise NoUploadSession(f"this device has no open upload of {sha256} and the Hub does not store it")
        missing = sorted(set(range(session.chunk_count)) - set(self._received(session)))
        if missing:
            raise Incomplete(missing)
        chunks = [self._session_dir(session.id) / str(index) for index in range(session.chunk_count)]
        try:
            stored = await self._store_and_register(chunks, StoredBlob(sha256, session.size), ending=session.id)
        except HashMismatch:
            await self._db.write(lambda tx: _end_sessions(tx, "id = ?", (session.id,)))
            await self._discard_sessions([session.id])
            raise
        except FileNotFoundError:
            # Another finalize or a revoke ended the session and removed its chunks while they were read.
            stored = await self._db.read(lambda snap: _stored_blob(snap, sha256))
            if stored is None:
                raise NoUploadSession(f"the upload of {sha256} ended while it was finalized") from None
            return stored
        await self._discard_sessions([session.id])
        return stored

    async def drop_sessions(self, device: DeviceId) -> list[KeptLeftover]:
        """Discard the device's open uploads, as revoking it does (D6)."""
        ended = await self._db.write(lambda tx: _end_sessions(tx, "device_id = ?", (device,)))
        return await self._discard_sessions(ended)

    async def _sweep_orphan_sessions(self) -> list[KeptLeftover]:
        # List before reading the rows: a directory made after the listing belongs to a row made before it.
        directories = await asyncio.to_thread(lambda: sorted(self._uploads_dir.iterdir()) if self._uploads_dir.is_dir() else [])
        owned = await self._db.read(lambda snap: {session_id for (session_id,) in snap.execute("SELECT id FROM upload_sessions")})
        kept = []
        for directory in directories:
            if directory.name not in owned:
                kept += await asyncio.to_thread(self._remove_session_dir, directory)
        return kept

    async def expire_sessions(self, now: datetime) -> list[KeptLeftover]:
        """End the uploads untouched for SESSION_LIFETIME before `now` (E10) and remove the directories no session owns.

        A crash between ending a session and removing its chunks leaves such a directory.
        """
        cutoff = instant(now - SESSION_LIFETIME)
        await self._db.write(lambda tx: _end_sessions(tx, "touched_at < ?", (cutoff,)))
        return await self._sweep_orphan_sessions()
