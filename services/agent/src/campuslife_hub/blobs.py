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
UNCOMMITTED_LIFETIME = timedelta(days=7)
# A collection takes a Blob only while its row is old, and a store that lost its file to one writes a new row.
STORE_ATTEMPTS = 2
# Every (table, column) that refers to a Blob by sha256 (D2: Attachment, Artifact and a Relation's ends now,
# AnnotationLayer later).
BLOB_REFERENCES = (
    ("attachments", "blob_sha256"),
    ("artifacts", "content_blob"),
    ("relations", "from_blob"),
    ("relations", "to_blob"),
)
COLLECTABLE = "SELECT sha256 FROM blobs WHERE stored_at < ?" + "".join(
    f" AND NOT EXISTS (SELECT 1 FROM {table} WHERE {table}.{column} = blobs.sha256)" for table, column in BLOB_REFERENCES
)
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


class BlobVanished(Exception):
    """A collection took the Blob's file after the store found it in place."""


class _InUse(Exception):
    def __init__(self, leftover: KeptLeftover):
        super().__init__(leftover.reason)
        self.leftover = leftover


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


def _restart_collection_clock(tx: Tx, sha256: str) -> None:
    tx.execute("UPDATE blobs SET stored_at = ? WHERE sha256 = ?", (now_instant(), sha256))


def _end_sessions(tx: Tx, where: str, params: Sequence[object]) -> list[str]:
    return [session_id for (session_id,) in tx.execute(f"DELETE FROM upload_sessions WHERE {where} RETURNING id", params)]


def _unlink_if_present(path: Path) -> None:
    path.unlink(missing_ok=True)


def _place_new(src: Path, dst: Path) -> None:
    """Give src's file the name dst too, or raise FileExistsError when dst exists; src may stay behind."""
    if sys.platform == "win32":
        # MoveFileEx without MOVEFILE_REPLACE_EXISTING refuses an existing target, on any Windows file system.
        os.rename(src, dst)
    else:
        # rename(2) replaces an existing target; link(2) refuses it.
        os.link(src, dst)


@dataclass(frozen=True)
class FileOps:
    """The file steps Windows refuses while another handle has the file open, and the wait before trying again."""

    replace: Callable[[Path, Path], None] = os.replace
    place_new: Callable[[Path, Path], None] = _place_new
    unlink: Callable[[Path], None] = _unlink_if_present
    rmdir: Callable[[Path], None] = Path.rmdir
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


def fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_hashing(sources: Sequence[Path], staging: Path) -> StoredBlob:
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
        self.removals = asyncio.Lock()
        """Held while a Blob file leaves blobs/ (collection, purge) and by a backup, which copies the files its
        snapshot names, so none of them goes between the snapshot and the copy."""

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
            fsync_directory(final.parent)
        try:
            os.chmod(final, READ_ONLY)
        except FileNotFoundError:
            raise BlobVanished(final) from None

    def _store_from(self, sources: Sequence[Path], expect: StoredBlob | None) -> StoredBlob:
        staging = self._new_staging_path()
        try:
            stored = copy_hashing(sources, staging)
            if expect is not None and stored != expect:
                raise HashMismatch(f"the bytes are {stored.sha256} and {stored.size} bytes long, not {expect.sha256} and {expect.size}")
            final = self.path_of(stored.sha256)
            self._retry_while_locked(lambda: self._move_into_place(staging, final))
        except BaseException:
            self._discard_quietly(staging)
            raise
        return stored

    def _register(self, tx: Tx, stored: StoredBlob, ending: str | None) -> None:
        if not self.path_of(stored.sha256).exists():
            raise BlobVanished(self.path_of(stored.sha256))
        tx.execute(
            "INSERT INTO blobs (sha256, size, stored_at) VALUES (?, ?, ?) ON CONFLICT (sha256) DO UPDATE SET stored_at = excluded.stored_at",
            (stored.sha256, stored.size, now_instant()),
        )
        if ending is not None:
            _end_sessions(tx, "id = ?", (ending,))

    async def _store_and_register(self, sources: Sequence[Path], expect: StoredBlob | None = None, *, ending: str | None = None) -> StoredBlob:
        for attempt in range(STORE_ATTEMPTS):
            try:
                stored = await asyncio.to_thread(self._store_from, sources, expect)
                await self._db.write(lambda tx, stored=stored: self._register(tx, stored, ending))
                return stored
            except BlobVanished:
                if attempt == STORE_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable: the last attempt returns or raises")

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
            self._files.rmdir(directory)
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

        A new upload must fit twice in the space free on the data root's volume when it opens: finalize writes
        the combined file while the chunks are still there. Answering that the Blob is stored restarts its
        collection clock, so it outlives the commit the device sends next.
        """
        _refuse_outside_contract("blob/upload_request", {"sha256": sha256, "size": size})

        def open_or_resume(tx: Tx) -> StoredBlob | UploadSession:
            stored = _stored_blob(tx, sha256)
            if stored is not None:
                if stored.size != size:
                    raise InvalidUpload(f"the stored Blob {sha256} is {stored.size} bytes, not {size}")
                _restart_collection_clock(tx, sha256)
                return stored
            session = _open_session(tx, device, sha256)
            if session is None:
                free = self._free_space(self._uploads_dir)
                if 2 * size > free:
                    raise InvalidUpload(f"{size} bytes and their combined copy do not fit in the {free} bytes free on the data root's volume")
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
        """Converges however often the chunk is sent: the file ends up holding `data`, unless it holds other bytes.

        Of two sends of one index at once, the first to place its file wins and the other is compared with it.
        """
        if not chunk.exists():
            staging = self._write_staging(data)
            try:
                chunk.parent.mkdir(parents=True, exist_ok=True)
                self._retry_while_locked(lambda: self._files.place_new(staging, chunk))
                fsync_directory(chunk.parent)
                return
            except FileExistsError:
                pass
            finally:
                self._discard_quietly(staging)
        if chunk.read_bytes() != data:
            raise ChunkMismatch(f"other bytes are already stored as chunk {chunk.name}")

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
        try:
            await asyncio.to_thread(self._place_chunk, self._session_dir(session.id) / str(index), data)
        except FileNotFoundError:
            raise NoUploadSession(f"the upload of {sha256} ended while chunk {index} was placed") from None

        def record(tx: Tx) -> None:
            if tx.execute("UPDATE upload_sessions SET touched_at = ? WHERE id = ?", (now_instant(), session.id)).rowcount == 0:
                raise NoUploadSession(f"the upload of {sha256} ended while chunk {index} was stored")

        await self._db.write(record)

    async def finalize(self, device: DeviceId, sha256: str) -> StoredBlob:
        """Combine the device's chunks of `sha256` and store the Blob (D7).

        A Blob already stored answers at once, ends the device's session for it and restarts its collection clock.
        """
        _refuse_outside_contract("common/sha256", sha256)

        def settle(tx: Tx) -> tuple[StoredBlob | None, _Session | None]:
            stored, session = _stored_blob(tx, sha256), _open_session(tx, device, sha256)
            if stored is not None:
                _restart_collection_clock(tx, sha256)
                if session is not None:
                    _end_sessions(tx, "id = ?", (session.id,))
            return stored, session

        stored, session = await self._db.write(settle)
        if stored is not None:
            if session is not None:
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

    def _put_back(self, trash: Path, final: Path) -> None:
        """Undo a collection whose transaction failed after the move, since its row is back."""
        if trash.exists() and not final.exists():
            self._files.replace(trash, final)
            os.chmod(final, READ_ONLY)

    async def _take_out(self, sha256: str, removable: Callable[[Tx], bool]) -> list[KeptLeftover]:
        """Remove the Blob's row and file while `removable` holds in the transaction that moves the file out."""
        final = self.path_of(sha256)
        trash = self._new_staging_path()

        def take(tx: Tx) -> bool:
            if not removable(tx):
                return False
            tx.execute("DELETE FROM blobs WHERE sha256 = ?", (sha256,))
            if not final.exists():
                return False
            os.chmod(final, stat.S_IRUSR | stat.S_IWUSR)
            try:
                self._files.replace(final, trash)
            except OSError as error:
                os.chmod(final, READ_ONLY)
                if is_sharing_violation(error):
                    raise _InUse(KeptLeftover(final, f"in use, left for the next collection: {error}")) from error
                raise
            return True

        try:
            moved = await self._db.write(take)
        except _InUse as in_use:
            return [in_use.leftover]
        except BaseException:
            await asyncio.to_thread(self._put_back, trash, final)
            raise
        return await asyncio.to_thread(self._unlink_once, trash) if moved else []

    async def collect_garbage(self, now: datetime) -> list[KeptLeftover]:
        """Remove the Blobs no BLOB_REFERENCES column names whose bytes last arrived UNCOMMITTED_LIFETIME before `now` (D2).

        The file leaves blobs/ in the same transaction as its row, so a store that found it in place
        sees it gone and stores the bytes again. Each Blob is tried once; one in use stays for the next
        collection.
        """
        cutoff = instant(now - UNCOMMITTED_LIFETIME)

        def still_collectable(sha256: str) -> Callable[[Tx], bool]:
            return lambda tx: tx.execute(f"{COLLECTABLE} AND sha256 = ?", (cutoff, sha256)).fetchone() is not None

        async with self.removals:
            candidates = await self._db.read(lambda snap: [sha256 for (sha256,) in snap.execute(COLLECTABLE, (cutoff,))])
            kept = []
            for sha256 in candidates:
                kept += await self._take_out(sha256, still_collectable(sha256))
            return kept

    async def discard(self, sha256: str) -> list[KeptLeftover]:
        """Remove the file of a Blob whose row a purge deleted (U2); a Blob stored again since keeps its file.

        Finding the file gone already is done, so a purge finished again after a crash ends the same way. The caller
        holds `removals`."""
        return await self._take_out(sha256, lambda tx: not has_blob(tx, sha256))
