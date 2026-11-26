import errno
import os
import stat
import sys
import threading

import pytest

from campuslife_hub.blobs import BlobStore, FileOps, KeptLeftover, StoredBlob, has_blob, is_sharing_violation, with_lock_retry

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def store_in(tmp_path, db, files=FileOps()):
    return BlobStore(db, blobs_dir=tmp_path / "blobs", staging_dir=tmp_path / "staging", files=files)


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


def must_not_wait(seconds):
    raise AssertionError(f"waited {seconds}s")


def test_a_leftover_still_locked_after_the_retries_is_kept_and_reported(tmp_path, db):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "stuck.part").write_bytes(b"half")
    (staging / "free.part").write_bytes(b"half")

    def unlink(path):
        if path.name == "stuck.part":
            raise WindowsError_(32)
        path.unlink(missing_ok=True)

    blob_store = store_in(tmp_path, db, FileOps(unlink=unlink, sleep=lambda seconds: None))
    [kept] = blob_store.discard_staging_leftovers()

    assert kept.path == staging / "stuck.part"
    assert "locked" in kept.reason
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
