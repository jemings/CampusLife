import asyncio
import sqlite3
import time

import pytest

from campuslife_hub.data_root import Layout
from campuslife_hub.db import Database, NotInitialized, ReadOnlyDatabase, SchemaMismatch, StartupRefused
from campuslife_hub.lock import HubLock, LockNotHeld
from campuslife_hub.migrations import MIGRATIONS


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def layout(tmp_path):
    return Layout(tmp_path)


@pytest.fixture
def lock(layout):
    with HubLock.acquire(layout.lock_file) as held:
        yield held


async def opened(layout, lock, **options):
    return await Database.open(layout, lock=lock, create=True, **options)


def test_a_write_commits_and_a_read_sees_it(layout, lock):
    async def scenario():
        db = await opened(layout, lock)
        try:
            await db.write(lambda tx: tx.execute("CREATE TABLE t (x)"))
            await db.write(lambda tx: tx.execute("INSERT INTO t VALUES (1)"))
            return await db.read(lambda snap: snap.execute("SELECT x FROM t").fetchall())
        finally:
            await db.close()

    assert run(scenario()) == [(1,)]


def test_a_failing_write_rolls_back_and_raises(layout, lock):
    async def scenario():
        db = await opened(layout, lock)
        try:
            await db.write(lambda tx: tx.execute("CREATE TABLE t (x)"))

            def half_then_fail(tx):
                tx.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")

            with pytest.raises(RuntimeError):
                await db.write(half_then_fail)
            return await db.read(lambda snap: snap.execute("SELECT count(*) FROM t").fetchone()[0])
        finally:
            await db.close()

    assert run(scenario()) == 0


def test_concurrent_writes_are_serialised(layout, lock):
    def increment(tx):
        (value,) = tx.execute("SELECT n FROM counter").fetchone()
        tx.execute("UPDATE counter SET n = ?", (value + 1,))

    async def scenario():
        db = await opened(layout, lock)
        try:
            await db.write(lambda tx: tx.execute("CREATE TABLE counter (n)"))
            await db.write(lambda tx: tx.execute("INSERT INTO counter VALUES (0)"))
            await asyncio.gather(*(db.write(increment) for _ in range(200)))
            return await db.read(lambda snap: snap.execute("SELECT n FROM counter").fetchone()[0])
        finally:
            await db.close()

    assert run(scenario()) == 200


def test_a_write_takes_the_write_lock_before_it_reads(layout, lock):
    """Two writer connections on one file: the second waits instead of failing or losing an update."""

    def slow_increment(tx):
        (value,) = tx.execute("SELECT n FROM counter").fetchone()
        time.sleep(0.3)
        tx.execute("UPDATE counter SET n = ?", (value + 1,))

    async def scenario():
        first = await opened(layout, lock)
        second = await opened(layout, lock)
        try:
            await first.write(lambda tx: tx.execute("CREATE TABLE counter (n)"))
            await first.write(lambda tx: tx.execute("INSERT INTO counter VALUES (0)"))

            async def later():
                await asyncio.sleep(0.1)
                await second.write(slow_increment)

            await asyncio.gather(first.write(slow_increment), later())
            return await first.read(lambda snap: snap.execute("SELECT n FROM counter").fetchone()[0])
        finally:
            await first.close()
            await second.close()

    assert run(scenario()) == 2


def test_writes_use_wal_with_full_sync(layout, lock):
    async def scenario():
        db = await opened(layout, lock)
        try:
            return await db.write(
                lambda tx: (tx.execute("PRAGMA journal_mode").fetchone()[0], tx.execute("PRAGMA synchronous").fetchone()[0])
            )
        finally:
            await db.close()

    assert run(scenario()) == ("wal", 2)


def test_a_read_cannot_write(layout, lock):
    async def scenario():
        db = await opened(layout, lock)
        try:
            await db.write(lambda tx: tx.execute("CREATE TABLE t (x)"))
            await db.read(lambda snap: snap.execute("INSERT INTO t VALUES (1)"))
        finally:
            await db.close()

    with pytest.raises(Exception, match="readonly"):
        run(scenario())


def released_lock(layout):
    with HubLock.acquire(layout.lock_file) as lock:
        pass
    return lock


@pytest.fixture
def another_roots_lock(tmp_path):
    other = Layout(tmp_path / "other")
    other.root.mkdir()
    with HubLock.acquire(other.lock_file) as held:
        yield held


@pytest.mark.parametrize("not_this_roots_held_lock", ["none", "object", "released", "another root"])
def test_the_writer_is_opened_only_under_this_roots_held_hub_lock(layout, request, not_this_roots_held_lock):
    lock = {
        "none": lambda: None,
        "object": object,
        "released": lambda: released_lock(layout),
        "another root": lambda: request.getfixturevalue("another_roots_lock"),
    }[not_this_roots_held_lock]()
    with pytest.raises(LockNotHeld):
        run(Database.open(layout, lock=lock, create=True))
    assert not layout.database.exists()


def test_open_refuses_a_sqlite_that_fails_the_probe(layout, lock):
    with pytest.raises(StartupRefused) as refused:
        run(opened(layout, lock, probe=lambda conn: ["SQLite lacks fts5"]))
    assert refused.value.problems == ["SQLite lacks fts5"]


def test_open_without_create_needs_an_existing_database(layout, lock):
    with pytest.raises(NotInitialized):
        run(Database.open(layout, lock=lock, create=False))
    assert not layout.database.exists()


def schema_state(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0], conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0]


def test_a_read_only_open_reads_a_migrated_database_and_writes_nothing(layout, lock):
    path = layout.database

    async def scenario():
        db = await opened(layout, lock)
        try:
            await db.write(lambda tx: tx.execute("CREATE TABLE t (x)"))
            watcher = sqlite3.connect(path)
            before = watcher.execute("PRAGMA data_version").fetchone()[0]
            reader = await ReadOnlyDatabase.open(path)
            try:
                rows = await reader.read(lambda snap: snap.execute("SELECT count(*) FROM t").fetchone()[0])
            finally:
                await reader.close()
            after = watcher.execute("PRAGMA data_version").fetchone()[0]
            watcher.close()
            return rows, before, after
        finally:
            await db.close()

    rows, before, after = run(scenario())
    assert rows == 0
    assert before == after


@pytest.mark.parametrize("version", [0, len(MIGRATIONS) + 1])
def test_a_read_only_open_refuses_another_schema_version_and_leaves_it(tmp_path, version):
    path = tmp_path / "hub.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {version}")
    with pytest.raises(SchemaMismatch):
        run(ReadOnlyDatabase.open(path))
    assert schema_state(path) == (version, 0)


def test_a_read_only_open_refuses_an_empty_file(tmp_path):
    path = tmp_path / "hub.sqlite3"
    path.write_bytes(b"")
    with pytest.raises(SchemaMismatch, match="init"):
        run(ReadOnlyDatabase.open(path))
    assert path.read_bytes() == b""


def test_a_read_only_open_needs_the_file(tmp_path):
    with pytest.raises(NotInitialized):
        run(ReadOnlyDatabase.open(tmp_path / "missing.sqlite3"))
    assert not (tmp_path / "missing.sqlite3").exists()


def test_the_writer_turns_secure_delete_on(layout, lock):
    async def scenario():
        db = await opened(layout, lock)
        try:
            return await db.write(lambda tx: tx.execute("PRAGMA secure_delete").fetchone())
        finally:
            await db.close()

    assert run(scenario()) == (1,)
