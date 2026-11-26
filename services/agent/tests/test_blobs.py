import asyncio
import errno
import os
import sqlite3
import stat
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest

from campuslife_hub.blobs import BlobStore, FileOps, KeptLeftover, StoredBlob, has_blob, is_sharing_violation, with_lock_retry

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def store_in(tmp_path, db, files=FileOps()):
    return BlobStore(db, blobs_dir=tmp_path / "blobs", staging_dir=tmp_path / "staging", uploads_dir=tmp_path / "uploads", files=files)


@pytest.fixture
def blob_store(tmp_path, db):
    return store_in(tmp_path, db)


def source(tmp_path, name, content: bytes):
    path = tmp_path / "in" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


async def test_a_file_is_stored_once_under_its_sha256(tmp_path, blob_store, db):
    stored = await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))
    assert stored == StoredBlob(ABC_SHA256, 3)
    final = tmp_path / "blobs" / "ba" / "78" / ABC_SHA256
    assert final.read_bytes() == b"abc"
    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))


async def test_a_stored_blob_is_read_only(tmp_path, blob_store):
    await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))
    mode = (tmp_path / "blobs" / "ba" / "78" / ABC_SHA256).stat().st_mode
    assert mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0


async def test_the_same_bytes_twice_keep_one_file_and_leave_no_staging(tmp_path, blob_store):
    first = await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))
    second = await blob_store.put_file(source(tmp_path, "copy of a.txt", b"abc"))
    assert first == second
    assert [path.name for path in (tmp_path / "blobs").rglob("*") if path.is_file()] == [ABC_SHA256]
    assert list((tmp_path / "staging").iterdir()) == []


async def test_an_empty_file_is_a_blob(tmp_path, blob_store):
    assert await blob_store.put_file(source(tmp_path, "empty", b"")) == StoredBlob(EMPTY_SHA256, 0)


async def test_an_unknown_blob_is_absent(db):
    assert not await db.read(lambda snap: has_blob(snap, ABC_SHA256))


async def test_a_missing_source_leaves_nothing_behind(tmp_path, blob_store):
    with pytest.raises(FileNotFoundError):
        await blob_store.put_file(tmp_path / "nope")
    assert not (tmp_path / "blobs").exists() or not any((tmp_path / "blobs").rglob("*"))
    assert not (tmp_path / "staging").exists() or list((tmp_path / "staging").iterdir()) == []


async def test_a_blob_left_writable_by_a_dead_run_is_made_read_only_by_the_next_put(tmp_path, blob_store):
    await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))
    final = tmp_path / "blobs" / "ba" / "78" / ABC_SHA256
    final.chmod(stat.S_IRUSR | stat.S_IWUSR)
    await blob_store.put_file(source(tmp_path, "again.txt", b"abc"))
    assert final.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0


def test_staging_leftovers_are_discarded_and_other_files_kept(tmp_path, blob_store):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "0f8fad5b.part").write_bytes(b"half")
    (staging / "notes.txt").write_bytes(b"not ours")
    blob_store.discard_staging_leftovers()
    assert sorted(path.name for path in staging.iterdir()) == ["notes.txt"]


def test_discarding_without_a_staging_directory_does_nothing(blob_store):
    blob_store.discard_staging_leftovers()


class WindowsError_(OSError):
    def __init__(self, winerror):
        super().__init__(errno.EACCES, "locked")
        self.winerror = winerror


def test_only_windows_access_and_sharing_violations_count_as_transient_locks():
    assert is_sharing_violation(WindowsError_(32))
    assert is_sharing_violation(WindowsError_(5))
    assert not is_sharing_violation(WindowsError_(2))
    assert not is_sharing_violation(PermissionError(errno.EACCES, "denied"))


def test_a_transient_lock_is_retried_until_it_clears():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise WindowsError_(32)
        return "done"

    assert with_lock_retry(flaky, attempts=5, sleep=lambda seconds: None) == "done"
    assert len(attempts) == 3


def test_a_lock_that_does_not_clear_raises_after_the_last_attempt():
    def locked():
        raise WindowsError_(32)

    with pytest.raises(OSError):
        with_lock_retry(locked, attempts=4, sleep=lambda seconds: None)


def refused_at_first(times, step):
    """`step`, except that its first `times` calls fail as a file another handle holds open."""
    calls = []

    def flaky(*args):
        calls.append(args)
        if len(calls) <= times:
            raise WindowsError_(32)
        return step(*args)

    return flaky, calls


async def test_put_file_retries_a_replace_refused_by_a_sharing_violation(tmp_path, db):
    replace, calls = refused_at_first(2, os.replace)
    slept = []
    blob_store = store_in(tmp_path, db, FileOps(replace=replace, sleep=slept.append))

    stored = await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))

    assert stored == StoredBlob(ABC_SHA256, 3)
    assert len(calls) == 3
    assert len(slept) == 2
    final = tmp_path / "blobs" / "ba" / "78" / ABC_SHA256
    assert final.read_bytes() == b"abc"
    assert final.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert list((tmp_path / "staging").iterdir()) == []
    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))


async def test_a_second_put_of_the_same_bytes_retries_discarding_its_staging_file(tmp_path, db):
    await store_in(tmp_path, db).put_file(source(tmp_path, "a.txt", b"abc"))
    unlink, calls = refused_at_first(2, lambda path: path.unlink())
    slept = []
    blob_store = store_in(tmp_path, db, FileOps(unlink=unlink, sleep=slept.append))

    assert await blob_store.put_file(source(tmp_path, "again.txt", b"abc")) == StoredBlob(ABC_SHA256, 3)

    assert len(calls) == 3
    assert len(slept) == 2
    assert list((tmp_path / "staging").iterdir()) == []


def must_not_wait(seconds):
    raise AssertionError(f"waited {seconds}s")


def test_a_leftover_still_in_use_is_tried_once_and_kept_for_the_next_start(tmp_path, db):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "stuck.part").write_bytes(b"half")
    (staging / "free.part").write_bytes(b"half")
    tries = []

    def unlink(path):
        tries.append(path.name)
        if path.name == "stuck.part":
            raise WindowsError_(32)
        path.unlink(missing_ok=True)

    blob_store = store_in(tmp_path, db, FileOps(unlink=unlink, sleep=must_not_wait))
    [kept] = blob_store.discard_staging_leftovers()

    assert kept.path == staging / "stuck.part"
    assert "locked" in kept.reason
    assert sorted(tries) == ["free.part", "stuck.part"]
    assert [path.name for path in staging.iterdir()] == ["stuck.part"]


def test_a_leftover_that_cannot_be_removed_for_another_reason_stops_the_discard(tmp_path, db):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "denied.part").write_bytes(b"half")

    def unlink(path):
        raise PermissionError(errno.EACCES, "denied", str(path))

    blob_store = store_in(tmp_path, db, FileOps(unlink=unlink, sleep=lambda seconds: None))
    with pytest.raises(PermissionError):
        blob_store.discard_staging_leftovers()


def test_a_directory_named_like_a_leftover_is_kept_and_reported(tmp_path, blob_store):
    staging = tmp_path / "staging"
    (staging / "odd.part").mkdir(parents=True)
    (staging / "half.part").write_bytes(b"half")

    assert blob_store.discard_staging_leftovers() == [KeptLeftover(staging / "odd.part", "not a regular file")]
    assert [path.name for path in staging.iterdir()] == ["odd.part"]


def test_other_errors_are_not_retried():
    attempts = []

    def missing():
        attempts.append(1)
        raise FileNotFoundError("gone")

    with pytest.raises(FileNotFoundError):
        with_lock_retry(missing, attempts=5, sleep=lambda seconds: None)
    assert len(attempts) == 1


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="sharing violations are how Windows treats an open handle")


def release_later(handle, seconds=0.3):
    timer = threading.Timer(seconds, handle.close)
    timer.start()
    return timer


@windows_only
async def test_put_file_goes_through_once_another_handle_on_its_staging_file_closes(tmp_path, db):
    holders, attempts = [], []

    def replace_while_another_handle_holds_the_source(src, dst):
        attempts.append(1)
        if not holders:
            holder = open(src, "rb")
            holders.append((holder, release_later(holder)))
        os.replace(src, dst)

    blob_store = store_in(tmp_path, db, FileOps(replace=replace_while_another_handle_holds_the_source))
    try:
        stored = await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))
    finally:
        for holder, timer in holders:
            timer.cancel()
            holder.close()

    assert stored == StoredBlob(ABC_SHA256, 3)
    assert len(attempts) > 1
    assert (tmp_path / "blobs" / "ba" / "78" / ABC_SHA256).read_bytes() == b"abc"
    assert list((tmp_path / "staging").iterdir()) == []


@windows_only
def test_a_staging_leftover_held_open_is_kept_and_discarded_by_the_start_after_the_handle_closes(tmp_path, db):
    staging = tmp_path / "staging"
    staging.mkdir()
    leftover = staging / "held.part"
    leftover.write_bytes(b"half")
    blob_store = store_in(tmp_path, db, FileOps(sleep=must_not_wait))
    with open(leftover, "rb"):
        [kept] = blob_store.discard_staging_leftovers()
    assert kept.path == leftover
    assert blob_store.discard_staging_leftovers() == []
    assert not leftover.exists()


@windows_only
def test_a_handle_that_stays_open_fails_the_replace_with_a_sharing_violation(tmp_path):
    staging = tmp_path / "x.part"
    staging.write_bytes(b"x")
    with open(staging, "rb"):
        with pytest.raises(OSError) as refused:
            with_lock_retry(lambda: os.replace(staging, tmp_path / "final"), attempts=3, sleep=lambda seconds: None)
    assert is_sharing_violation(refused.value)


async def test_a_failed_put_keeps_its_own_error_when_its_staging_file_cannot_be_discarded(tmp_path, db):
    def replace(src, dst):
        raise PermissionError(errno.EACCES, "replace refused", str(dst))

    discarded = []

    def unlink(path):
        discarded.append(path)
        raise WindowsError_(32)

    blob_store = store_in(tmp_path, db, FileOps(replace=replace, unlink=unlink, sleep=must_not_wait))

    with pytest.raises(PermissionError, match="replace refused"):
        await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))

    [staging] = (tmp_path / "staging").iterdir()
    assert discarded == [staging]
    assert not await db.read(lambda snap: has_blob(snap, ABC_SHA256))


def a_week_and_a_day_on():
    return datetime.now(UTC) + timedelta(days=8)


def is_read_only(path):
    return path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0


async def age_blobs(db):
    await db.write(lambda tx: tx.execute("UPDATE blobs SET stored_at = '2000-01-01T00:00:00.000Z'"))


async def test_collection_removes_an_unreferenced_blob_stored_more_than_seven_days_ago(tmp_path, db, blob_store):
    await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))

    assert await blob_store.collect_garbage(a_week_and_a_day_on()) == []

    assert not await db.read(lambda snap: has_blob(snap, ABC_SHA256))
    assert not blob_store.path_of(ABC_SHA256).exists()
    assert list((tmp_path / "staging").iterdir()) == []


async def test_collection_keeps_an_unreferenced_blob_stored_within_seven_days(tmp_path, db, blob_store):
    await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))

    assert await blob_store.collect_garbage(datetime.now(UTC) + timedelta(days=6)) == []

    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))
    assert is_read_only(blob_store.path_of(ABC_SHA256))


async def test_putting_the_same_bytes_again_restarts_the_seven_days(tmp_path, db, blob_store):
    await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))
    await age_blobs(db)

    await blob_store.put_file(source(tmp_path, "again.txt", b"abc"))
    await blob_store.collect_garbage(datetime.now(UTC))

    assert blob_store.path_of(ABC_SHA256).read_bytes() == b"abc"


async def test_collection_drops_the_row_of_a_blob_whose_file_is_already_gone(tmp_path, db, blob_store):
    await blob_store.put_file(source(tmp_path, "a.txt", b"abc"))
    final = blob_store.path_of(ABC_SHA256)
    final.chmod(stat.S_IRUSR | stat.S_IWUSR)
    final.unlink()

    assert await blob_store.collect_garbage(a_week_and_a_day_on()) == []

    assert not await db.read(lambda snap: has_blob(snap, ABC_SHA256))


async def test_a_blob_in_use_is_kept_for_the_next_collection(tmp_path, db):
    held = {True}

    def replace(src, dst):
        if held:
            raise WindowsError_(32)
        os.replace(src, dst)

    blob_store = store_in(tmp_path, db, FileOps(replace=replace, sleep=must_not_wait))
    await store_in(tmp_path, db).put_file(source(tmp_path, "a.txt", b"abc"))

    [kept] = await blob_store.collect_garbage(a_week_and_a_day_on())

    assert kept.path == blob_store.path_of(ABC_SHA256)
    assert "in use" in kept.reason
    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))
    assert is_read_only(kept.path)
    held.clear()
    assert await blob_store.collect_garbage(a_week_and_a_day_on()) == []
    assert not kept.path.exists()


async def test_a_blob_that_fails_to_move_for_another_reason_stops_the_collection_and_stays(tmp_path, db):
    def replace(src, dst):
        raise PermissionError(errno.EACCES, "denied", str(src))

    blob_store = store_in(tmp_path, db, FileOps(replace=replace))
    await store_in(tmp_path, db).put_file(source(tmp_path, "a.txt", b"abc"))

    with pytest.raises(PermissionError):
        await blob_store.collect_garbage(a_week_and_a_day_on())

    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))
    assert is_read_only(blob_store.path_of(ABC_SHA256))


async def test_a_collected_file_still_in_use_stays_writable_in_staging_for_the_next_start(tmp_path, db):
    def unlink(path):
        raise WindowsError_(32)

    blob_store = store_in(tmp_path, db, FileOps(unlink=unlink, sleep=must_not_wait))
    await store_in(tmp_path, db).put_file(source(tmp_path, "a.txt", b"abc"))

    [kept] = await blob_store.collect_garbage(a_week_and_a_day_on())

    assert kept.path.parent == tmp_path / "staging"
    assert kept.path.read_bytes() == b"abc"
    assert not is_read_only(kept.path)
    assert not await db.read(lambda snap: has_blob(snap, ABC_SHA256))
    assert store_in(tmp_path, db).discard_staging_leftovers() == []


class FailsAfterEachWrite:
    """The database, except that every write rolls back after its work, as a failed COMMIT would."""

    def __init__(self, db):
        self._db = db

    async def read(self, fn):
        return await self._db.read(fn)

    async def write(self, fn):
        def then_fail(tx):
            fn(tx)
            raise sqlite3.OperationalError("disk I/O error")

        return await self._db.write(then_fail)


async def test_a_collection_whose_transaction_fails_puts_the_file_back(tmp_path, db):
    await store_in(tmp_path, db).put_file(source(tmp_path, "a.txt", b"abc"))
    blob_store = store_in(tmp_path, FailsAfterEachWrite(db))

    with pytest.raises(sqlite3.OperationalError):
        await blob_store.collect_garbage(a_week_and_a_day_on())

    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))
    final = blob_store.path_of(ABC_SHA256)
    assert final.read_bytes() == b"abc"
    assert is_read_only(final)


async def test_a_put_whose_file_a_collection_took_after_it_was_found_stores_the_bytes_again(tmp_path, db):
    await store_in(tmp_path, db).put_file(source(tmp_path, "a.txt", b"abc"))
    loop = asyncio.get_running_loop()
    collections = []

    def collect_then_unlink(path):
        if not collections:
            collections.append("running")
            collections[0] = asyncio.run_coroutine_threadsafe(blob_store.collect_garbage(a_week_and_a_day_on()), loop).result()
        path.unlink(missing_ok=True)

    blob_store = store_in(tmp_path, db, FileOps(unlink=collect_then_unlink))

    assert await blob_store.put_file(source(tmp_path, "again.txt", b"abc")) == StoredBlob(ABC_SHA256, 3)

    assert collections == [[]]
    assert blob_store.path_of(ABC_SHA256).read_bytes() == b"abc"
    assert is_read_only(blob_store.path_of(ABC_SHA256))
    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))


class CollectsBeforeTheFirstWrite:
    """The database, except that a collection runs just before the first write."""

    def __init__(self, db, collect):
        self._db = db
        self._collect = collect

    async def read(self, fn):
        return await self._db.read(fn)

    async def write(self, fn):
        if self._collect is not None:
            collect, self._collect = self._collect, None
            await collect()
        return await self._db.write(fn)


async def test_a_put_whose_file_a_collection_took_before_its_row_was_written_stores_the_bytes_again(tmp_path, db):
    collector = store_in(tmp_path, db)
    await collector.put_file(source(tmp_path, "a.txt", b"abc"))
    racing = store_in(tmp_path, CollectsBeforeTheFirstWrite(db, lambda: collector.collect_garbage(a_week_and_a_day_on())))

    assert await racing.put_file(source(tmp_path, "again.txt", b"abc")) == StoredBlob(ABC_SHA256, 3)

    assert racing.path_of(ABC_SHA256).read_bytes() == b"abc"
    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))


@windows_only
async def test_a_blob_another_handle_holds_open_is_kept_and_collected_once_it_closes(tmp_path, db):
    await store_in(tmp_path, db).put_file(source(tmp_path, "a.txt", b"abc"))
    blob_store = store_in(tmp_path, db, FileOps(sleep=must_not_wait))
    final = blob_store.path_of(ABC_SHA256)

    with open(final, "rb"):
        [kept] = await blob_store.collect_garbage(a_week_and_a_day_on())

    assert kept.path == final
    assert is_read_only(final)
    assert await db.read(lambda snap: has_blob(snap, ABC_SHA256))
    assert await blob_store.collect_garbage(a_week_and_a_day_on()) == []
    assert not final.exists()
