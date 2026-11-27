import asyncio
import errno
import hashlib
import shutil
import stat
import threading
import uuid
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from campuslife_contracts import validate

from campuslife_hub.blobs import (
    BlobStore,
    ChunkMismatch,
    FileOps,
    HashMismatch,
    HubStorageFull,
    Incomplete,
    InvalidUpload,
    NoUploadSession,
    StoredBlob,
    UploadSession,
    has_blob,
)
from campuslife_hub.clock import now_instant
from campuslife_hub.devices import DeviceId
from test_blobs import WindowsError_, must_not_wait, refused_at_first, windows_only

CHUNK = 4
DATA = b"0123456789"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def chunks(data):
    return [data[start : start + CHUNK] for start in range(0, len(data), CHUNK)]


def uploads_in(tmp_path, db, files=FileOps(), **options):
    return BlobStore(
        db,
        blobs_dir=tmp_path / "blobs",
        staging_dir=tmp_path / "staging",
        uploads_dir=tmp_path / "uploads",
        files=files,
        chunk_size=CHUNK,
        **options,
    )


def new_device(db):
    device = str(uuid.uuid4())
    asyncio.run(
        db.write(
            lambda tx: tx.execute(
                "INSERT INTO devices (id, platform, device_name, app_version, state, created_at)"
                " VALUES (?, 'android', 'phone', '0.1.0', 'active', ?)",
                (device, now_instant()),
            )
        )
    )
    return DeviceId(device)


@pytest.fixture
def store(tmp_path, db):
    return uploads_in(tmp_path, db)


@pytest.fixture
def phone(db):
    return new_device(db)


@pytest.fixture
def tablet(db):
    return new_device(db)


def body(schema, answer):
    assert validate(schema, answer.body()) == []
    return answer.body()


async def put_all(store, device, data, order=None):
    pieces = chunks(data)
    for index in order if order is not None else range(len(pieces)):
        await store.put_chunk(device, sha(data), index, pieces[index])


async def upload(store, device, data):
    await store.open_upload(device, sha(data), len(data))
    await put_all(store, device, data)
    return await store.finalize(device, sha(data))


def session_dirs(tmp_path):
    uploads = tmp_path / "uploads"
    return sorted(path.name for path in uploads.iterdir()) if uploads.exists() else []


def staging_files(tmp_path):
    staging = tmp_path / "staging"
    return list(staging.iterdir()) if staging.exists() else []


async def test_a_new_upload_opens_a_session_with_the_hubs_chunk_size_and_nothing_received(store, phone):
    session = await store.open_upload(phone, sha(DATA), len(DATA))

    assert body("blob/upload_session", session) == {"sha256": sha(DATA), "size": len(DATA), "chunk_size": CHUNK, "received": []}


async def test_finalizing_every_chunk_stores_the_blob_read_only_and_ends_the_session(tmp_path, db, store, phone):
    stored = await upload(store, phone, DATA)

    assert body("blob/stored", stored) == {"sha256": sha(DATA), "size": len(DATA)}
    final = store.path_of(sha(DATA))
    assert final.read_bytes() == DATA
    assert final.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert await db.read(lambda snap: has_blob(snap, sha(DATA)))
    assert session_dirs(tmp_path) == []
    assert staging_files(tmp_path) == []
    with pytest.raises(NoUploadSession):
        await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])


async def test_chunks_arrive_in_any_order(store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[2, 0, 1])

    assert await store.finalize(phone, sha(DATA)) == StoredBlob(sha(DATA), len(DATA))


async def test_opening_again_resumes_with_the_chunks_already_received(store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[2, 0])

    resumed = await store.open_upload(phone, sha(DATA), len(DATA))

    assert body("blob/upload_session", resumed)["received"] == [0, 2]
    assert resumed.chunk_size == CHUNK


async def test_opening_again_with_another_size_is_an_invalid_request(store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))

    with pytest.raises(InvalidUpload) as refused:
        await store.open_upload(phone, sha(DATA), len(DATA) + 1)

    assert body("common/error", refused.value)["code"] == "invalid_request"


async def test_opening_a_blob_already_stored_answers_stored_and_opens_nothing(tmp_path, store, phone, tablet):
    await upload(store, phone, DATA)

    assert body("blob/stored", await store.open_upload(tablet, sha(DATA), len(DATA))) == {"sha256": sha(DATA), "size": len(DATA)}
    with pytest.raises(NoUploadSession):
        await store.put_chunk(tablet, sha(DATA), 0, chunks(DATA)[0])


async def test_opening_a_blob_another_device_stored_meanwhile_ends_this_devices_own_upload_of_it(tmp_path, store, phone, tablet):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[0])
    await upload(store, tablet, DATA)

    assert await store.open_upload(phone, sha(DATA), len(DATA)) == StoredBlob(sha(DATA), len(DATA))
    assert session_dirs(tmp_path) == []
    with pytest.raises(NoUploadSession):
        await store.put_chunk(phone, sha(DATA), 1, chunks(DATA)[1])


async def test_opening_a_blob_already_stored_with_another_size_is_an_invalid_request(store, phone):
    await upload(store, phone, DATA)

    with pytest.raises(InvalidUpload):
        await store.open_upload(phone, sha(DATA), len(DATA) - 1)


@pytest.mark.parametrize(("sha256", "size"), [("A" * 64, 1), ("../" + "0" * 61, 1), (EMPTY_SHA256, -1), (EMPTY_SHA256, True), (EMPTY_SHA256, 1.0)])
async def test_an_upload_request_outside_the_contract_is_invalid(tmp_path, store, phone, sha256, size):
    with pytest.raises(InvalidUpload) as refused:
        await store.open_upload(phone, sha256, size)

    assert body("common/error", refused.value)["code"] == "invalid_request"
    assert session_dirs(tmp_path) == []


async def test_a_chunk_or_finalize_naming_no_sha256_is_invalid(store, phone):
    with pytest.raises(InvalidUpload):
        await store.put_chunk(phone, "../escape", 0, b"x")
    with pytest.raises(InvalidUpload):
        await store.finalize(phone, "../escape")


async def test_sending_the_same_bytes_again_changes_nothing(store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA)
    await put_all(store, phone, DATA)

    assert await store.finalize(phone, sha(DATA)) == StoredBlob(sha(DATA), len(DATA))


async def test_other_bytes_for_a_received_chunk_are_a_chunk_mismatch_and_the_first_bytes_stay(tmp_path, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA)

    with pytest.raises(ChunkMismatch) as refused:
        await store.put_chunk(phone, sha(DATA), 1, b"XXXX")

    assert body("common/error", refused.value)["code"] == "chunk_mismatch"
    assert staging_files(tmp_path) == []
    assert await store.finalize(phone, sha(DATA)) == StoredBlob(sha(DATA), len(DATA))



def stage_both_before_either_places(monkeypatch):
    """Hold each chunk send after its staging file is written until a second send has written one too."""
    both_staged = threading.Barrier(2, timeout=5)
    real = BlobStore._write_staging

    def stage_then_wait(self, data):
        staging = real(self, data)
        both_staged.wait()
        return staging

    monkeypatch.setattr(BlobStore, "_write_staging", stage_then_wait)


async def test_two_sends_of_one_chunk_with_other_bytes_at_once_keep_the_first_placed_and_refuse_the_other(tmp_path, store, phone, monkeypatch):
    await store.open_upload(phone, sha(DATA), len(DATA))
    stage_both_before_either_places(monkeypatch)
    sent = [b"0123", b"XXXX"]

    results = await asyncio.gather(*(store.put_chunk(phone, sha(DATA), 0, data) for data in sent), return_exceptions=True)

    [kept] = [data for data, result in zip(sent, results, strict=True) if result is None]
    [refused] = [result for result in results if result is not None]
    assert isinstance(refused, ChunkMismatch)
    [directory] = (tmp_path / "uploads").iterdir()
    assert (directory / "0").read_bytes() == kept
    assert staging_files(tmp_path) == []


async def test_two_sends_of_one_chunk_with_the_same_bytes_at_once_both_succeed(tmp_path, store, phone, monkeypatch):
    await store.open_upload(phone, sha(DATA), len(DATA))
    stage_both_before_either_places(monkeypatch)

    assert await asyncio.gather(*(store.put_chunk(phone, sha(DATA), 0, b"0123") for _ in range(2))) == [None, None]

    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (0,)
    assert staging_files(tmp_path) == []

@pytest.mark.parametrize(
    ("index", "data"),
    [
        (3, b"x"),
        (-1, b"0123"),
        (True, b"4567"),
        (0, b"012"),
        (0, b"01234"),
        (2, b"89x"),
        (2, b"8"),
    ],
)
async def test_a_chunk_past_the_last_index_or_of_the_wrong_length_is_invalid(tmp_path, store, phone, index, data):
    await store.open_upload(phone, sha(DATA), len(DATA))

    with pytest.raises(InvalidUpload) as refused:
        await store.put_chunk(phone, sha(DATA), index, data)

    assert body("common/error", refused.value)["code"] == "invalid_request"
    assert body("blob/upload_session", await store.open_upload(phone, sha(DATA), len(DATA)))["received"] == []
    assert staging_files(tmp_path) == []


async def test_a_chunk_without_an_open_session_is_not_found(store, phone):
    with pytest.raises(NoUploadSession) as refused:
        await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])

    assert body("common/error", refused.value)["code"] == "not_found"


async def test_finalizing_without_a_session_or_a_stored_blob_is_not_found(store, phone):
    with pytest.raises(NoUploadSession):
        await store.finalize(phone, sha(DATA))


async def test_finalizing_a_blob_already_stored_answers_stored_without_a_session(store, phone, tablet):
    await upload(store, phone, DATA)

    assert await store.finalize(phone, sha(DATA)) == StoredBlob(sha(DATA), len(DATA))
    assert await store.finalize(tablet, sha(DATA)) == StoredBlob(sha(DATA), len(DATA))


async def test_finalizing_before_every_chunk_arrived_names_the_missing_ones(store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await store.put_chunk(phone, sha(DATA), 1, chunks(DATA)[1])

    with pytest.raises(Incomplete) as refused:
        await store.finalize(phone, sha(DATA))

    assert body("common/error", refused.value)["details"] == {"missing_chunks": [0, 2]}
    assert body("blob/upload_session", await store.open_upload(phone, sha(DATA), len(DATA)))["received"] == [1]


async def session_count(db):
    return await db.read(lambda snap: snap.execute("SELECT count(*) FROM upload_sessions").fetchone()[0])


async def test_an_upload_needing_more_than_the_free_space_for_its_chunks_and_their_combination_is_refused_as_storage_full(tmp_path, db, phone):
    store = uploads_in(tmp_path, db, free_space=lambda path: 2 * len(DATA) - 1)

    with pytest.raises(HubStorageFull) as refused:
        await store.open_upload(phone, sha(DATA), len(DATA))

    assert body("common/error", refused.value)["code"] == "hub_storage_full"
    assert await session_count(db) == 0


async def test_an_upload_whose_chunks_and_combination_just_fit_the_free_space_on_the_data_roots_volume_opens(tmp_path, db, phone):
    measured = []
    store = uploads_in(tmp_path, db, free_space=lambda path: measured.append(path) or 2 * len(DATA))

    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == ()
    assert measured == [tmp_path / "uploads"]


async def test_the_free_space_is_measured_on_the_nearest_directory_that_exists_above_the_uploads(tmp_path, db, phone, monkeypatch):
    measured = []
    usage = namedtuple("usage", "total used free")

    def disk_usage(path):
        measured.append(Path(path))
        return usage(total=10**12, used=10**12 - (2 * len(DATA) - 1), free=2 * len(DATA) - 1)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    uploads = tmp_path / "not yet made" / "uploads"
    store = BlobStore(db, blobs_dir=tmp_path / "blobs", staging_dir=tmp_path / "staging", uploads_dir=uploads, chunk_size=CHUNK)

    with pytest.raises(HubStorageFull):
        await store.open_upload(phone, sha(DATA), len(DATA))
    uploads.mkdir(parents=True)
    with pytest.raises(HubStorageFull):
        await store.open_upload(phone, sha(DATA), len(DATA))

    assert measured == [tmp_path, uploads]

async def test_chunk_length_is_what_put_chunk_will_take_for_that_index(store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))

    assert [await store.chunk_length(phone, sha(DATA), index) for index in range(3)] == [4, 4, 2]
    with pytest.raises(InvalidUpload):
        await store.chunk_length(phone, sha(DATA), 3)
    with pytest.raises(NoUploadSession):
        await store.chunk_length(phone, sha(b"other"), 0)


async def test_resuming_is_not_measured_against_the_free_space_again(tmp_path, db, phone):
    free = [2 * len(DATA)]
    store = uploads_in(tmp_path, db, free_space=lambda path: free[0])
    await store.open_upload(phone, sha(DATA), len(DATA))
    await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])
    free[0] = 0

    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (0,)


async def test_a_chunk_that_fails_to_move_keeps_its_error_and_leaves_no_staging_file(tmp_path, db, phone):
    def place_new(src, dst):
        raise PermissionError(errno.EACCES, "place refused", str(dst))

    store = uploads_in(tmp_path, db, FileOps(place_new=place_new))
    await store.open_upload(phone, sha(DATA), len(DATA))

    with pytest.raises(PermissionError, match="place refused"):
        await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])

    assert staging_files(tmp_path) == []
    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == ()


async def test_a_chunk_is_moved_into_place_while_the_database_writer_stays_free(tmp_path, db, phone):
    loop = asyncio.get_running_loop()

    def place_while_another_write_runs(src, dst):
        asyncio.run_coroutine_threadsafe(db.write(lambda tx: None), loop).result(timeout=5)
        FileOps().place_new(src, dst)

    store = uploads_in(tmp_path, db, FileOps(place_new=place_while_another_write_runs))
    await store.open_upload(phone, sha(DATA), len(DATA))

    await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])

    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (0,)


def finalize_first(monkeypatch, step):
    """Run `step` once, as the next combine of chunks is about to read them."""
    import campuslife_hub.blobs as blobs_module

    loop = asyncio.get_running_loop()
    real = blobs_module.copy_hashing

    def step_then_combine(sources, staging):
        monkeypatch.setattr(blobs_module, "copy_hashing", real)
        asyncio.run_coroutine_threadsafe(step(), loop).result(timeout=5)
        return real(sources, staging)

    monkeypatch.setattr(blobs_module, "copy_hashing", step_then_combine)


async def test_a_finalize_whose_chunks_another_finalize_removed_answers_stored(tmp_path, store, phone, monkeypatch):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA)
    finalize_first(monkeypatch, lambda: store.finalize(phone, sha(DATA)))

    assert body("blob/stored", await store.finalize(phone, sha(DATA))) == {"sha256": sha(DATA), "size": len(DATA)}

    assert staging_files(tmp_path) == []
    assert session_dirs(tmp_path) == []


async def test_a_finalize_whose_session_a_revoke_dropped_is_not_found(tmp_path, db, store, phone, monkeypatch):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA)
    finalize_first(monkeypatch, lambda: store.drop_sessions(phone))

    with pytest.raises(NoUploadSession) as refused:
        await store.finalize(phone, sha(DATA))

    assert body("common/error", refused.value)["code"] == "not_found"
    assert not await db.read(lambda snap: has_blob(snap, sha(DATA)))
    assert staging_files(tmp_path) == []


async def test_chunks_that_combine_to_another_sha256_are_discarded_and_nothing_is_stored(tmp_path, db, store, phone):
    claimed = sha(DATA)
    await store.open_upload(phone, claimed, len(DATA))
    for index, piece in enumerate(chunks(b"ABCDEFGHIJ")):
        await store.put_chunk(phone, claimed, index, piece)

    with pytest.raises(HashMismatch) as refused:
        await store.finalize(phone, claimed)

    assert body("common/error", refused.value)["code"] == "hash_mismatch"
    assert not await db.read(lambda snap: has_blob(snap, claimed))
    assert not (tmp_path / "blobs").exists() or list((tmp_path / "blobs").rglob("*")) == []
    assert staging_files(tmp_path) == []
    assert session_dirs(tmp_path) == []
    with pytest.raises(NoUploadSession):
        await store.finalize(phone, claimed)
    assert body("blob/upload_session", await store.open_upload(phone, claimed, len(DATA)))["received"] == []


async def test_an_empty_blob_has_no_chunks_and_finalizes_at_once(store, phone):
    session = await store.open_upload(phone, EMPTY_SHA256, 0)

    assert session == UploadSession(EMPTY_SHA256, 0, CHUNK, ())
    with pytest.raises(InvalidUpload):
        await store.put_chunk(phone, EMPTY_SHA256, 0, b"")
    assert body("blob/stored", await store.finalize(phone, EMPTY_SHA256)) == {"sha256": EMPTY_SHA256, "size": 0}
    assert store.path_of(EMPTY_SHA256).read_bytes() == b""


async def test_an_empty_upload_claiming_another_sha256_is_a_hash_mismatch(store, phone):
    await store.open_upload(phone, sha(DATA), 0)

    with pytest.raises(HashMismatch):
        await store.finalize(phone, sha(DATA))


async def test_each_device_has_its_own_session_for_the_same_sha256(tmp_path, store, phone, tablet):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[0])

    assert (await store.open_upload(tablet, sha(DATA), len(DATA))).received == ()
    await put_all(store, tablet, DATA, order=[1])
    assert await upload(store, phone, DATA) == StoredBlob(sha(DATA), len(DATA))
    assert await store.finalize(tablet, sha(DATA)) == StoredBlob(sha(DATA), len(DATA))
    assert session_dirs(tmp_path) == []
    with pytest.raises(NoUploadSession):
        await store.put_chunk(tablet, sha(DATA), 0, chunks(DATA)[0])


async def test_dropping_a_devices_sessions_discards_its_chunks_and_leaves_other_devices_alone(tmp_path, store, phone, tablet):
    other = b"another blob"
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[0, 1])
    await store.open_upload(tablet, sha(other), len(other))
    await put_all(store, tablet, other, order=[0])

    assert await store.drop_sessions(phone) == []

    assert len(session_dirs(tmp_path)) == 1
    with pytest.raises(NoUploadSession):
        await store.put_chunk(phone, sha(DATA), 2, chunks(DATA)[2])
    assert (await store.open_upload(tablet, sha(other), len(other))).received == (0,)


async def test_a_chunk_for_a_dropped_session_is_refused_and_its_staging_file_discarded(tmp_path, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await store.drop_sessions(phone)

    with pytest.raises(NoUploadSession):
        await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])
    assert staging_files(tmp_path) == []


def ended_by(store, device, then):
    """A place_new that ends the device's sessions, as a revoke or an expiry would, before `then` runs."""
    loop = asyncio.get_running_loop()

    def end_then(src, dst):
        asyncio.run_coroutine_threadsafe(store.drop_sessions(device), loop).result(timeout=5)
        then(src, dst)

    return end_then


def placed_by_another_send(src, dst):
    raise FileExistsError(errno.EEXIST, "placed by another send", str(dst))


@pytest.mark.parametrize("then", [FileOps().place_new, placed_by_another_send], ids=["placing", "comparing"])
async def test_a_chunk_whose_session_ends_after_its_directory_was_made_is_not_found(tmp_path, db, store, phone, then):
    racing = uploads_in(tmp_path, db, FileOps(place_new=ended_by(store, phone, then)))
    await store.open_upload(phone, sha(DATA), len(DATA))

    with pytest.raises(NoUploadSession) as refused:
        await racing.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])

    assert body("common/error", refused.value)["code"] == "not_found"
    assert session_dirs(tmp_path) == []
    assert staging_files(tmp_path) == []

async def test_a_chunk_placement_refused_by_a_sharing_violation_is_retried(tmp_path, db, phone):
    place_new, calls = refused_at_first(2, FileOps().place_new)
    slept = []
    store = uploads_in(tmp_path, db, FileOps(place_new=place_new, sleep=slept.append))
    await store.open_upload(phone, sha(DATA), len(DATA))

    await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])

    assert len(calls) == 3
    assert len(slept) == 2
    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (0,)


def a_week_and_a_day_on():
    return datetime.now(UTC) + timedelta(days=8)


async def age_sessions(db, touched_at="2000-01-01T00:00:00.000Z"):
    await db.write(lambda tx: tx.execute("UPDATE upload_sessions SET touched_at = ?", (touched_at,)))


async def test_a_session_untouched_for_seven_days_expires_with_its_chunks(tmp_path, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[0, 1])

    assert await store.expire_sessions(a_week_and_a_day_on()) == []

    assert session_dirs(tmp_path) == []
    with pytest.raises(NoUploadSession):
        await store.put_chunk(phone, sha(DATA), 2, chunks(DATA)[2])


async def test_a_session_touched_within_seven_days_stays(tmp_path, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[0])

    assert await store.expire_sessions(datetime.now(UTC) + timedelta(days=6)) == []

    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (0,)


async def test_a_chunk_keeps_its_session_from_expiring(db, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await age_sessions(db)

    await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])
    await store.expire_sessions(datetime.now(UTC))

    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (0,)


async def test_resuming_keeps_a_session_from_expiring(db, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])
    await age_sessions(db)

    await store.open_upload(phone, sha(DATA), len(DATA))
    await store.expire_sessions(datetime.now(UTC))

    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (0,)


class RunsAfterTheFirstRead:
    """The database, except that `after` runs just after the first read."""

    def __init__(self, db, after):
        self._db = db
        self._after = after

    async def read(self, fn):
        result = await self._db.read(fn)
        if self._after is not None:
            after, self._after = self._after, None
            await after()
        return result

    async def write(self, fn):
        return await self._db.write(fn)


async def test_a_chunk_placed_after_its_session_ended_is_refused_and_swept_as_an_orphan(tmp_path, db, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    racing = uploads_in(tmp_path, RunsAfterTheFirstRead(db, lambda: store.drop_sessions(phone)))

    with pytest.raises(NoUploadSession):
        await racing.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])

    assert len(session_dirs(tmp_path)) == 1
    assert await store.expire_sessions(datetime.now(UTC)) == []
    assert session_dirs(tmp_path) == []
    assert staging_files(tmp_path) == []


@pytest.mark.parametrize("answer", ["open", "finalize without a session", "finalize with a session"])
async def test_answering_stored_restarts_the_seven_days(db, store, phone, tablet, answer):
    from campuslife_hub.clock import instant

    await store.open_upload(tablet, sha(DATA), len(DATA))
    await upload(store, phone, DATA)
    almost_a_week_ago = datetime.now(UTC) - timedelta(days=7) + timedelta(minutes=1)
    await db.write(lambda tx: tx.execute("UPDATE blobs SET stored_at = ?", (instant(almost_a_week_ago),)))
    answers = {
        "open": lambda: store.open_upload(phone, sha(DATA), len(DATA)),
        "finalize without a session": lambda: store.finalize(phone, sha(DATA)),
        "finalize with a session": lambda: store.finalize(tablet, sha(DATA)),
    }

    assert await answers[answer]() == StoredBlob(sha(DATA), len(DATA))
    await store.collect_garbage(datetime.now(UTC) + timedelta(minutes=2))

    assert store.path_of(sha(DATA)).read_bytes() == DATA


async def test_expiring_removes_directories_no_session_owns_and_reports_what_is_not_one(tmp_path, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[1])
    orphan = tmp_path / "uploads" / str(uuid.uuid4())
    orphan.mkdir()
    (orphan / "0").write_bytes(b"0123")
    stray = tmp_path / "uploads" / "notes.txt"
    stray.write_bytes(b"not a session")

    [kept] = await store.expire_sessions(datetime.now(UTC))

    assert kept.path == stray
    assert "not an upload session directory" in kept.reason
    assert not orphan.exists()
    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (1,)


async def test_a_chunk_still_in_use_is_kept_and_removed_by_a_later_sweep(tmp_path, db, phone):
    locked = {"0"}

    def unlink(path):
        if path.name in locked:
            raise WindowsError_(32)
        path.unlink(missing_ok=True)

    store = uploads_in(tmp_path, db, FileOps(unlink=unlink, sleep=must_not_wait))
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[0, 1])

    [kept] = await store.expire_sessions(a_week_and_a_day_on())

    assert kept.path.name == "0"
    assert "in use" in kept.reason
    locked.clear()
    assert await store.expire_sessions(datetime.now(UTC)) == []
    assert session_dirs(tmp_path) == []


async def test_a_session_directory_that_fails_to_go_for_another_reason_stops_the_sweep(tmp_path, db, phone):
    def unlink(path):
        raise PermissionError(errno.EACCES, "denied", str(path))

    store = uploads_in(tmp_path, db, FileOps(unlink=unlink))
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[0])

    with pytest.raises(PermissionError):
        await store.expire_sessions(a_week_and_a_day_on())


@windows_only
async def test_a_chunk_another_handle_holds_open_is_kept_and_swept_once_it_closes(tmp_path, db, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await put_all(store, phone, DATA, order=[0])
    [directory] = (tmp_path / "uploads").iterdir()
    store = uploads_in(tmp_path, db, FileOps(sleep=must_not_wait))

    with open(directory / "0", "rb"):
        [kept] = await store.expire_sessions(a_week_and_a_day_on())

    assert kept.path == directory / "0"
    assert await store.expire_sessions(datetime.now(UTC)) == []
    assert session_dirs(tmp_path) == []


async def test_files_in_a_session_directory_that_are_not_its_chunks_are_not_received(tmp_path, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await store.put_chunk(phone, sha(DATA), 1, chunks(DATA)[1])
    [directory] = (tmp_path / "uploads").iterdir()
    for name in ["desktop.ini", "01", "99"]:
        (directory / name).write_bytes(b"x")

    assert (await store.open_upload(phone, sha(DATA), len(DATA))).received == (1,)


async def test_a_session_directory_still_in_use_is_kept_for_the_next_sweep(tmp_path, db, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])
    [directory] = (tmp_path / "uploads").iterdir()
    held = {True}

    def rmdir(path):
        if held:
            raise WindowsError_(32)
        path.rmdir()

    sweeping = uploads_in(tmp_path, db, FileOps(rmdir=rmdir, sleep=must_not_wait))

    [kept] = await sweeping.expire_sessions(a_week_and_a_day_on())

    assert kept.path == directory
    assert "in use" in kept.reason
    held.clear()
    assert await sweeping.expire_sessions(datetime.now(UTC)) == []
    assert session_dirs(tmp_path) == []


async def test_a_session_directory_the_os_refuses_to_remove_for_another_reason_stops_the_sweep(tmp_path, db, store, phone):
    await store.open_upload(phone, sha(DATA), len(DATA))
    await store.put_chunk(phone, sha(DATA), 0, chunks(DATA)[0])

    def rmdir(path):
        raise PermissionError(errno.EACCES, "denied", str(path))

    sweeping = uploads_in(tmp_path, db, FileOps(rmdir=rmdir))

    with pytest.raises(PermissionError):
        await sweeping.expire_sessions(a_week_and_a_day_on())
