"""The Hub database (D16, E4): one write connection owned by one thread, reads on another.

Every write is `await db.write(fn)`: fn runs on the writer thread inside
BEGIN IMMEDIATE and commits when it returns, so writes are serialised by
construction rather than by callers taking care.

Only a holder of the data root's hub.lock can open the writer, which is also the
only code that migrates (E11). Everyone else opens a `ReadOnlyDatabase`, which
writes no data and refuses a database whose schema is not this code's.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

from .data_root import Layout
from .lock import HubLock, require_held
from .migrations import MIGRATIONS, migrate
from .sqlite_probe import probe as probe_sqlite
from .sqlite_probe import secure_delete_problems

BUSY_TIMEOUT_MS = 5000
CHECKPOINT_BUSY_TIMEOUT_MS = 250
START_THE_HUB = "run `campuslife-hub init` or start the Hub"


class StartupRefused(Exception):
    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


class NotInitialized(Exception):
    pass


class SchemaMismatch(Exception):
    pass


class Tx:
    """A statement runner inside a write transaction; only Database.write makes one."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)


class Snapshot:
    """A read-only statement runner over one consistent view; only a database's read makes one."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)


Probe = Callable[[sqlite3.Connection], list[str]]


def _refuse_unfit_sqlite(probe: Probe) -> None:
    with closing(sqlite3.connect(":memory:")) as probe_conn:
        problems = probe(probe_conn)
    if problems:
        raise StartupRefused(problems)


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise NotInitialized(f"no Hub database at {path}; {START_THE_HUB}")


def _open_writer(path: Path, create: bool) -> sqlite3.Connection:
    if not create:
        _require_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if problems := secure_delete_problems(conn):
            raise StartupRefused(problems)
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _refuse_other_schema(conn: sqlite3.Connection) -> None:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version < len(MIGRATIONS):
        raise SchemaMismatch(f"the database is at schema version {version}, this Hub needs {len(MIGRATIONS)}; {START_THE_HUB}")
    if version > len(MIGRATIONS):
        raise SchemaMismatch(f"the database is at schema version {version}, this Hub knows up to {len(MIGRATIONS)}; run a newer Hub")


def _open_reader(path: Path) -> sqlite3.Connection:
    _require_file(path)
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _refuse_other_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _truncate_wal(conn: sqlite3.Connection) -> bool:
    """Wait only briefly for readers: a retry exists, and the wait blocks every other write."""
    conn.execute(f"PRAGMA busy_timeout = {CHECKPOINT_BUSY_TIMEOUT_MS}")
    try:
        (busy, _, _) = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return not busy


def _run_write[T](conn: sqlite3.Connection, fn: Callable[[Tx], T]) -> T:
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(Tx(conn))
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return result


def _run_read[T](conn: sqlite3.Connection, fn: Callable[[Snapshot], T]) -> T:
    conn.execute("BEGIN")
    try:
        return fn(Snapshot(conn))
    finally:
        conn.execute("ROLLBACK")


async def _open_in(executor: ThreadPoolExecutor, opener: Callable[..., sqlite3.Connection], *args: object) -> sqlite3.Connection:
    try:
        return await asyncio.get_running_loop().run_in_executor(executor, opener, *args)
    except BaseException:
        executor.shutdown()
        raise


class ReadOnlyDatabase:
    def __init__(self, reader: ThreadPoolExecutor, read_conn: sqlite3.Connection):
        self._reader = reader
        self._read_conn = read_conn

    @classmethod
    async def open(cls, path: Path, *, probe: Probe = probe_sqlite) -> ReadOnlyDatabase:
        """A `mode=ro` reader: it writes no data, though SQLite may create the -shm and -wal files beside a WAL database."""
        _refuse_unfit_sqlite(probe)
        reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-db-read")
        return cls(reader, await _open_in(reader, _open_reader, Path(path)))

    async def read[T](self, fn: Callable[[Snapshot], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._reader, _run_read, self._read_conn, fn)

    async def close(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._reader, self._read_conn.close)
        self._reader.shutdown()


class Database(ReadOnlyDatabase):
    def __init__(self, writer: ThreadPoolExecutor, write_conn: sqlite3.Connection, reader: ThreadPoolExecutor, read_conn: sqlite3.Connection):
        super().__init__(reader, read_conn)
        self._writer = writer
        self._write_conn = write_conn

    @classmethod
    async def open(cls, layout: Layout, *, lock: HubLock, create: bool, probe: Probe = probe_sqlite) -> Database:
        """The writer of the database at `layout`, opened only while `lock` holds that data root's hub.lock."""
        require_held(lock, layout.lock_file)
        _refuse_unfit_sqlite(probe)
        writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-db-write")
        write_conn = await _open_in(writer, _open_writer, layout.database, create)
        reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-db-read")
        try:
            read_conn = await _open_in(reader, _open_reader, layout.database)
        except BaseException:
            await asyncio.get_running_loop().run_in_executor(writer, write_conn.close)
            writer.shutdown()
            raise
        return cls(writer, write_conn, reader, read_conn)

    async def write[T](self, fn: Callable[[Tx], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._writer, _run_write, self._write_conn, fn)

    async def truncate_wal(self) -> bool:
        """Checkpoint the WAL into the database file and empty it, so bytes of deleted rows leave it; False when a
        reader kept it from emptying."""
        return await asyncio.get_running_loop().run_in_executor(self._writer, _truncate_wal, self._write_conn)

    async def close(self) -> None:
        await super().close()
        await asyncio.get_running_loop().run_in_executor(self._writer, self._write_conn.close)
        self._writer.shutdown()
