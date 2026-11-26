import hashlib
import logging
import os
import sys
import unicodedata
from pathlib import Path, PureWindowsPath

import pytest
from campuslife_contracts import validate

from campuslife_hub import changes, store
from campuslife_hub.blobs import StoredBlob
from campuslife_hub.core import UnsafeDataRoot, accepted_root, open_core, open_core_readonly, safe_data_root
from campuslife_hub.data_root import Allowed, DevicePath, InsideSyncFolder, Layout, UncPath
from campuslife_hub.db import Database, NotInitialized, SchemaMismatch, StartupRefused
from campuslife_hub.identity import read_identity
from campuslife_hub.local_import import UnimportableFile, import_files
from campuslife_hub.lock import AlreadyRunning, HubLock
from campuslife_hub.store import Entity, EntityRef

COURSE = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
SESSION = "16fd2706-8baf-433b-82eb-8c7fada847da"


@pytest.fixture
def root(tmp_path):
    return tmp_path / "CampusLife"


def source(tmp_path, name, content=b"synthetic lecture handout"):
    path = tmp_path / "inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


async def feed_rows(core):
    body = await core.db.read(lambda snap: changes.page(snap, 0, 500))
    assert validate("sync/changes_page", body) == []
    return body["changes"]


def of(rows, entity):
    return [row["data"] for row in rows if row["entity"] == entity]


async def test_an_imported_file_becomes_a_capture_from_the_hub_device(root, tmp_path):
    async with open_core(root, create=True, sync_roots=[]) as core:
        imported = await import_files(core, [source(tmp_path, "week4.txt")])
        rows = await feed_rows(core)

    assert imported.result == {"mutation_id": imported.result["mutation_id"], "result": "applied"}
    [capture] = of(rows, "capture")
    assert capture["id"] == imported.capture_id
    assert capture["device_id"] == core.hub_device
    assert capture["origin"] == {"type": "import"}
    [attachment] = of(rows, "attachment")
    assert attachment["role"] == "file"
    assert attachment["original_filename"] == "week4.txt"
    assert "original_filename_raw" not in attachment
    assert attachment["cloud_policy"] == "local_only"


async def test_importing_the_same_file_twice_keeps_one_blob_and_two_attachments(root, tmp_path):
    path = source(tmp_path, "slides.txt")
    async with open_core(root, create=True, sync_roots=[]) as core:
        first = await import_files(core, [path])
        second = await import_files(core, [path])
        rows = await feed_rows(core)

    assert first.capture_id != second.capture_id
    attachments = of(rows, "attachment")
    assert len(attachments) == 2
    assert {attachment["capture_id"] for attachment in attachments} == {first.capture_id, second.capture_id}
    assert len({attachment["blob_sha256"] for attachment in attachments}) == 1
    assert len([path for path in (root / "blobs").rglob("*") if path.is_file()]) == 1


async def test_files_imported_together_share_one_capture(root, tmp_path):
    async with open_core(root, create=True, sync_roots=[]) as core:
        imported = await import_files(core, [source(tmp_path, "a.txt", b"a"), source(tmp_path, "b.txt", b"b")])
        rows = await feed_rows(core)

    attachments = of(rows, "attachment")
    assert sorted(attachment["original_filename"] for attachment in attachments) == ["a.txt", "b.txt"]
    assert {attachment["capture_id"] for attachment in attachments} == {imported.capture_id}


async def test_a_decomposed_file_name_is_stored_as_nfc_with_the_raw_name_kept(root, tmp_path):
    decomposed = unicodedata.normalize("NFD", "자료구조.txt")
    async with open_core(root, create=True, sync_roots=[]) as core:
        await import_files(core, [source(tmp_path, decomposed)])
        [attachment] = of(await feed_rows(core), "attachment")

    assert attachment["original_filename"] == "자료구조.txt"
    assert attachment["original_filename_raw"] == decomposed


async def test_an_explicit_policy_wins(root, tmp_path):
    async with open_core(root, create=True, sync_roots=[]) as core:
        await import_files(core, [source(tmp_path, "a.txt")], cloud_policy="blocked")
        [attachment] = of(await feed_rows(core), "attachment")
    assert attachment["cloud_policy"] == "blocked"


async def test_a_linked_course_or_its_session_gives_the_default_policy(root, tmp_path):
    course = {"id": COURSE, "kind": "course", "schema_version": 1, "title": "자료구조", "fields": {"default_cloud_policy": "cloud_allowed"}, "rev": 0}
    session = {
        "id": SESSION, "kind": "lecture_session", "schema_version": 1, "title": "4주차", "rev": 0,
        "fields": {"course_id": COURSE, "date": {"date": "2026-03-24", "tz": "Asia/Seoul"}},
    }
    async with open_core(root, create=True, sync_roots=[]) as core:
        await core.db.write(lambda tx: [store.write(tx, EntityRef(Entity.ITEM, row["id"]), row) for row in (course, session)])
        via_course = await import_files(core, [source(tmp_path, "a.txt", b"a")], links=[f"item:{COURSE}"])
        via_session = await import_files(core, [source(tmp_path, "b.txt", b"b")], links=[f"item:{SESSION}"])
        rows = await feed_rows(core)

    policies = {attachment["capture_id"]: attachment["cloud_policy"] for attachment in of(rows, "attachment")}
    assert policies == {via_course.capture_id: "cloud_allowed", via_session.capture_id: "cloud_allowed"}
    relation_ids = {relation["id"] for relation in of(rows, "relation")}
    assert len(relation_ids) == 2


async def test_a_root_inside_a_sync_folder_is_refused(tmp_path):
    with pytest.raises(UnsafeDataRoot) as refused:
        async with open_core(tmp_path / "OneDrive" / "CampusLife", create=True, sync_roots=[tmp_path / "OneDrive"]):
            pass
    assert "OneDrive" in str(refused.value)
    assert not (tmp_path / "OneDrive").exists()


def course_item(item_id, policy):
    return {"id": item_id, "kind": "course", "schema_version": 1, "title": "과목", "fields": {"default_cloud_policy": policy}, "rev": 0}


async def test_the_strictest_default_of_several_linked_courses_wins(root, tmp_path):
    strict, loose = "0f8fad5b-d9cb-469f-a165-70867728950e", COURSE
    async with open_core(root, create=True, sync_roots=[]) as core:
        await core.db.write(
            lambda tx: [store.write(tx, EntityRef(Entity.ITEM, row["id"]), row) for row in (course_item(strict, "blocked"), course_item(loose, "cloud_allowed"))]
        )
        strict_first = await import_files(core, [source(tmp_path, "a.txt", b"a")], links=[f"item:{strict}", f"item:{loose}"])
        strict_last = await import_files(core, [source(tmp_path, "b.txt", b"b")], links=[f"item:{loose}", f"item:{strict}"])
        rows = await feed_rows(core)

    policies = {attachment["capture_id"]: attachment["cloud_policy"] for attachment in of(rows, "attachment")}
    assert policies == {strict_first.capture_id: "blocked", strict_last.capture_id: "blocked"}


async def blob_count(core):
    return await core.db.read(lambda snap: snap.execute("SELECT count(*) FROM blobs").fetchone()[0])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names are bytes; Windows names are UTF-16")
async def test_a_file_name_that_is_not_unicode_is_refused_before_any_blob_is_stored(root, tmp_path):
    good = source(tmp_path, "good.txt")
    bad = tmp_path / "inbox" / os.fsdecode(b"bad\xff.txt")
    with open(os.fsencode(bad), "wb") as raw:
        raw.write(b"synthetic")
    async with open_core(root, create=True, sync_roots=[]) as core:
        with pytest.raises(UnimportableFile, match="rename"):
            await import_files(core, [good, bad])
        assert await blob_count(core) == 0
        assert await feed_rows(core) == []


async def test_a_directory_is_refused_before_any_blob_is_stored(root, tmp_path):
    good = source(tmp_path, "good.txt")
    async with open_core(root, create=True, sync_roots=[]) as core:
        with pytest.raises(UnimportableFile, match="not a regular file"):
            await import_files(core, [good, tmp_path / "inbox"])
        assert await blob_count(core) == 0


async def test_opening_the_core_discards_staging_leftovers(root):
    async with open_core(root, create=True, sync_roots=[]):
        pass
    leftover = root / "staging" / "0f8fad5b.part"
    leftover.parent.mkdir(exist_ok=True)
    leftover.write_bytes(b"half a blob")
    async with open_core(root, create=False, sync_roots=[]):
        assert not leftover.exists()


async def test_the_core_opens_past_a_staging_leftover_it_cannot_remove_and_logs_it(root, caplog):
    async with open_core(root, create=True, sync_roots=[]):
        pass
    odd = root / "staging" / "odd.part"
    odd.mkdir(parents=True)

    with caplog.at_level(logging.WARNING, logger="campuslife_hub"):
        async with open_core(root, create=False, sync_roots=[]) as core:
            assert core.hub_device

    assert odd.is_dir()
    assert str(odd) in caplog.text
    assert "not a regular file" in caplog.text


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX mode bits stop only a user other than root")
async def test_the_core_does_not_open_over_a_staging_directory_it_cannot_write(root):
    async with open_core(root, create=True, sync_roots=[]):
        pass
    staging = root / "staging"
    staging.mkdir(exist_ok=True)
    (staging / "0f8fad5b.part").write_bytes(b"half a blob")
    staging.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            async with open_core(root, create=False, sync_roots=[]):
                pass
    finally:
        staging.chmod(0o755)


async def test_the_core_holds_the_hub_lock_until_it_closes(root):
    async with open_core(root, create=True, sync_roots=[]):
        with pytest.raises(AlreadyRunning):
            HubLock.acquire(root / "hub.lock")
        with pytest.raises(AlreadyRunning):
            async with open_core(root, create=False, sync_roots=[]):
                pass
    with HubLock.acquire(root / "hub.lock"):
        pass


async def test_the_core_is_not_opened_while_another_process_holds_the_lock(root):
    async with open_core(root, create=True, sync_roots=[]):
        pass
    with HubLock.acquire(root / "hub.lock"):
        with pytest.raises(AlreadyRunning):
            async with open_core(root, create=False, sync_roots=[]):
                pass


async def test_the_core_without_create_needs_an_initialised_root(root):
    with pytest.raises(NotInitialized):
        async with open_core(root, create=False, sync_roots=[]):
            pass
    assert not root.exists()


async def test_a_read_only_core_refuses_a_root_inside_a_sync_folder(tmp_path):
    onedrive = tmp_path / "OneDrive"
    root = onedrive / "CampusLife"
    async with open_core(root, create=True, sync_roots=[]):
        pass
    with pytest.raises(UnsafeDataRoot, match="OneDrive"):
        async with open_core_readonly(root, sync_roots=[onedrive]):
            pass


async def test_a_read_only_core_reads_the_hub_identity(root):
    async with open_core(root, create=True, sync_roots=[]) as core:
        identity = core.identity
    async with open_core_readonly(root) as reader:
        assert reader.identity == identity
        assert await reader.db.read(read_identity) == identity


async def test_a_read_only_core_refuses_an_empty_database_file(root):
    (root / "database").mkdir(parents=True)
    (root / "database" / "campuslife.sqlite3").write_bytes(b"")
    with pytest.raises(SchemaMismatch, match="init"):
        async with open_core_readonly(root):
            pass


async def test_a_read_only_core_refuses_a_migrated_database_without_identity(root):
    root.mkdir()
    with HubLock.acquire(Layout(root).lock_file) as lock:
        bare = await Database.open(Layout(root), lock=lock, create=True)
        await bare.close()
    with pytest.raises(NotInitialized, match="init"):
        async with open_core_readonly(root):
            pass


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows paths have UNC drives")
async def test_a_unc_root_is_refused_at_startup_for_writing_and_for_reading():
    unc_root = Path(r"\\localhost\C$\CampusLife-unc-test")
    with pytest.raises(StartupRefused, match="UNC"):
        async with open_core(unc_root, create=True, sync_roots=[]):
            pass
    with pytest.raises(StartupRefused, match="UNC"):
        async with open_core_readonly(unc_root, sync_roots=[]):
            pass


def test_a_unc_verdict_refuses_startup_and_names_the_share():
    root = PureWindowsPath(r"\\server\share\CampusLife")
    with pytest.raises(StartupRefused, match=r"UNC share \\\\server\\share"):
        accepted_root(root, UncPath(r"\\server\share"))


def test_a_device_path_verdict_refuses_startup_as_a_drive_starting_with_two_backslashes():
    root = PureWindowsPath(r"\\.\C:\CampusLife")
    with pytest.raises(StartupRefused) as refused:
        accepted_root(root, DevicePath(r"\\.\C:"))
    assert "drive starting with \\" in str(refused.value)
    assert "UNC" not in str(refused.value)


def test_a_sync_folder_verdict_is_an_unsafe_data_root(tmp_path):
    with pytest.raises(UnsafeDataRoot, match="OneDrive"):
        accepted_root(tmp_path / "OneDrive" / "CampusLife", InsideSyncFolder(tmp_path / "OneDrive"))


def test_an_allowed_verdict_gives_the_root_it_carries():
    assert accepted_root(PureWindowsPath(r"\\?\C:\CampusLife"), Allowed(PureWindowsPath(r"C:\CampusLife"))) == PureWindowsPath(r"C:\CampusLife")


def test_a_symlinked_root_is_judged_where_it_leads(tmp_path):
    onedrive = tmp_path / "OneDrive"
    (onedrive / "CampusLife").mkdir(parents=True)
    link = tmp_path / "hub-root"
    try:
        link.symlink_to(onedrive / "CampusLife", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"cannot make a symlink here: {error}")
    with pytest.raises(UnsafeDataRoot):
        safe_data_root(link, [onedrive])


async def test_an_upload_session_and_its_chunks_survive_a_restart(root):
    data = b"synthetic slide deck"
    sha256 = hashlib.sha256(data).hexdigest()
    async with open_core(root, create=True, sync_roots=[]) as core:
        await core.blobs.open_upload(core.hub_device, sha256, len(data))
        await core.blobs.put_chunk(core.hub_device, sha256, 0, data)

    async with open_core(root, create=False, sync_roots=[]) as core:
        assert (await core.blobs.open_upload(core.hub_device, sha256, len(data))).received == (0,)
        assert await core.blobs.finalize(core.hub_device, sha256) == StoredBlob(sha256, len(data))


async def test_opening_the_core_ends_stale_upload_sessions_and_removes_orphan_chunks(root, caplog):
    data = b"synthetic slide deck"
    sha256 = hashlib.sha256(data).hexdigest()
    async with open_core(root, create=True, sync_roots=[]) as core:
        await core.blobs.open_upload(core.hub_device, sha256, len(data))
        await core.blobs.put_chunk(core.hub_device, sha256, 0, data)
        await core.db.write(lambda tx: tx.execute("UPDATE upload_sessions SET touched_at = '2000-01-01T00:00:00.000Z'"))
    orphan = root / "uploads" / "0f8fad5b-d9cb-469f-a165-70867728950e"
    orphan.mkdir(parents=True)
    (orphan / "0").write_bytes(b"orphan chunk")
    stray = root / "uploads" / "notes.txt"
    stray.write_bytes(b"not a session")

    with caplog.at_level(logging.WARNING, logger="campuslife_hub"):
        async with open_core(root, create=False, sync_roots=[]) as core:
            assert (await core.blobs.open_upload(core.hub_device, sha256, len(data))).received == ()

    assert sorted(path.name for path in (root / "uploads").iterdir()) == ["notes.txt"]
    assert str(stray) in caplog.text
