import asyncio
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_capture_commit import feed

from campuslife_hub import artifacts, backup, changes, fsck, jobs, purge
from campuslife_hub.backup import BackupRefused, RestoreRefused
from campuslife_hub.core import open_core, open_core_readonly
from campuslife_hub.jobs import JobSpec, Pipeline
from campuslife_hub.local_import import import_files

T0 = datetime(2026, 3, 2, 1, 0, tzinfo=UTC)
SUBJECT = "capture:0f8fad5b-d9cb-469f-a165-70867728950e"


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, by=timedelta(days=1)):
        self.now += by
        return self.now


def synthetic(tmp_path, name, content):
    path = tmp_path / "inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


async def imported(core, tmp_path, content, name=None):
    result = await import_files(core, [synthetic(tmp_path, name or f"{hashlib.sha256(content).hexdigest()[:8]}.pdf", content)])
    assert result.result["result"] == "applied"
    return hashlib.sha256(content).hexdigest()


async def made_from(core, tmp_path, sha, content):
    job = await core.db.write(lambda tx: jobs.enqueue(tx, JobSpec("ocr", SUBJECT, (sha,), {}), Pipeline("ocr-1")))
    content_blob = (await core.blobs.put_file(synthetic(tmp_path, "ocr.txt", content))).sha256
    await core.db.write(lambda tx: artifacts.record(tx, job, "ocr", content_blob))
    return content_blob


def manifest_of(target, name):
    return json.loads((target / "generations" / name / "manifest.json").read_text(encoding="utf-8"))


def kept_blobs(target):
    return sorted(path.name for path in (target / "blobs").rglob("*") if path.is_file())


async def attachments(core):
    return {row["data"]["blob_sha256"]: row["data"] for row in await feed(core.db) if row["entity"] == "attachment" and not row["deleted"]}


async def test_a_backup_holds_a_snapshot_the_blobs_it_names_and_a_manifest(tmp_path):
    target = tmp_path / "drive"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        pdf = await imported(core, tmp_path, b"%PDF week 1")
        derived = await made_from(core, tmp_path, pdf, b"week 1 text")
        lonely = (await core.blobs.put_file(synthetic(tmp_path, "lonely", b"never committed"))).sha256

        generation = await backup.back_up(core, target, clock=Clock())

    manifest = manifest_of(target, generation.name)
    assert generation.name == "20260302T010000000000Z"
    assert [blob["sha256"] for blob in manifest["blobs"]] == sorted([pdf, derived])
    assert kept_blobs(target) == sorted([pdf, derived])
    assert all(not os.access(path, os.W_OK) or os.name == "nt" for path in (target / "blobs").rglob("*") if path.is_file())
    snapshot = target / "generations" / generation.name / manifest["database"]["file"]
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == manifest["database"]["sha256"]
    with closing(sqlite3.connect(snapshot)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert conn.execute("SELECT count(*) FROM attachments").fetchone() == (1,)
        assert conn.execute("SELECT sha256 FROM blobs WHERE sha256 = ?", (lonely,)).fetchone() is None
    assert (generation.blobs, generation.copied, generation.missing) == (2, 2, ())
    assert manifest["generation"] == generation.name
    assert manifest["finished_at"] == generation.finished_at


async def test_a_backup_writes_backed_up_at_on_the_attachments_whose_blobs_it_holds_and_records_itself(tmp_path):
    target = tmp_path / "drive"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        pdf = await imported(core, tmp_path, b"%PDF week 1")

        generation = await backup.back_up(core, target, clock=Clock())

        assert (await attachments(core))[pdf]["backed_up_at"] == generation.finished_at
        last = await core.db.read(backup.last_backup)
        assert (last.generation, last.target, last.finished_at) == (generation.name, str(target.resolve()), generation.finished_at)


async def test_a_later_backup_copies_only_what_the_drive_lacks_and_leaves_backed_up_at_as_it_was(tmp_path):
    target, clock = tmp_path / "drive", Clock()
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        first_pdf = await imported(core, tmp_path, b"%PDF week 1")
        first = await backup.back_up(core, target, clock=clock)
        before = await feed(core.db)
        kept_file = (target / "blobs" / first_pdf[0:2] / first_pdf[2:4] / first_pdf).stat()
        clock.advance()
        second_pdf = await imported(core, tmp_path, b"%PDF week 2")

        second = await backup.back_up(core, target, clock=clock)

        assert (second.blobs, second.copied) == (2, 1)
        assert (target / "blobs" / first_pdf[0:2] / first_pdf[2:4] / first_pdf).stat().st_mtime_ns == kept_file.st_mtime_ns
        stamped = await attachments(core)
        assert (stamped[first_pdf]["backed_up_at"], stamped[second_pdf]["backed_up_at"]) == (first.finished_at, second.finished_at)
        resent = [row for row in await feed(core.db) if row not in before and row["entity"] == "attachment"]
        assert [row["data"]["blob_sha256"] for row in resent] == [second_pdf]


async def test_a_backup_keeps_the_newest_generations_and_only_the_blobs_they_name(tmp_path):
    target, clock = tmp_path / "drive", Clock()
    async with open_core(tmp_path / "root", create=True, sync_roots=[], purge_hooks=purge.PurgeHooks()) as core:
        gone = await imported(core, tmp_path, b"%PDF to be purged")
        first = await backup.back_up(core, target, keep=2, clock=clock)
        await purge.purge_blob(core.db, core.blobs, gone, hooks=core.purge_hooks)
        stays = await imported(core, tmp_path, b"%PDF stays")
        clock.advance()
        second = await backup.back_up(core, target, keep=2, clock=clock)
        assert kept_blobs(target) == sorted([gone, stays])
        clock.advance()

        third = await backup.back_up(core, target, keep=2, clock=clock)

    assert backup.generations(target) == [second.name, third.name]
    assert third.pruned == (first.name,)
    assert kept_blobs(target) == [stays]


async def test_a_generation_a_backup_did_not_finish_is_skipped_by_restore_and_removed_by_the_next_backup(tmp_path):
    target, clock = tmp_path / "drive", Clock()
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        await imported(core, tmp_path, b"%PDF week 1")
        finished = await backup.back_up(core, target, clock=clock)
        unfinished = target / "generations" / "20260303T000000000000Z"
        unfinished.mkdir()
        (unfinished / "campuslife.sqlite3").write_bytes(b"half a snapshot")
        stray = target / "blobs" / ".copy-1.part"
        stray.write_bytes(b"half a copy")

        restored = await backup.restore(target, tmp_path / "restored", sync_roots=[])
        assert restored.generation == finished.name

        clock.advance(timedelta(days=2))
        await backup.back_up(core, target, clock=clock)

    assert not unfinished.exists()
    assert not stray.exists()


async def test_restoring_into_an_empty_root_keeps_the_hub_issues_a_new_epoch_and_leaves_nothing_for_fsck(tmp_path):
    target = tmp_path / "drive"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        pdf = await imported(core, tmp_path, b"%PDF week 1")
        derived = await made_from(core, tmp_path, pdf, b"week 1 text")
        identity = core.identity
        generation = await backup.back_up(core, target, clock=Clock())

    restored = await backup.restore(target, tmp_path / "restored", sync_roots=[])

    assert restored.epoch != identity.epoch
    assert (restored.generation, restored.taken_at) == (generation.name, generation.started_at)
    async with open_core(tmp_path / "restored", create=False, sync_roots=[]) as core:
        assert (core.identity.hub_id, core.identity.epoch) == (identity.hub_id, restored.epoch)
        assert (await attachments(core))[pdf]["backed_up_at"] == generation.finished_at
        assert all(core.blobs.path_of(sha).is_file() for sha in (pdf, derived))
        assert (await core.db.read(backup.last_backup)).generation == generation.name
        assert (await core.db.read(lambda snap: changes.page(snap, 0, 10)))["epoch"] == restored.epoch
    async with open_core_readonly(tmp_path / "restored", sync_roots=[]) as core:
        assert await fsck.check(core) == []


async def test_a_restore_names_the_devices_it_knows_and_says_when_the_backup_was_taken(tmp_path):
    target = tmp_path / "drive"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        await core.db.write(lambda tx: tx.execute(
            "INSERT INTO devices (id, platform, device_name, app_version, state, created_at)"
            " VALUES ('6ba7b811-9dad-41d1-80b4-00c04fd430c8', 'android', '휴대폰', '1.0', 'active', '2026-03-01T00:00:00.000Z')"
        ))
        await backup.back_up(core, target, clock=Clock())

    restored = await backup.restore(target, tmp_path / "restored", sync_roots=[])

    assert restored.devices == (backup.KnownDevice("휴대폰", "android", "2026-03-01T00:00:00.000Z"),)
    assert restored.taken_at == "2026-03-02T01:00:00.000Z"


@pytest.mark.parametrize("leftover", ["file", "directory"])
async def test_a_restore_into_a_root_that_is_not_empty_is_refused_and_writes_nothing(tmp_path, leftover):
    target, root = tmp_path / "drive", tmp_path / "restored"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        await backup.back_up(core, target, clock=Clock())
    root.mkdir()
    (root / "old").mkdir() if leftover == "directory" else (root / "old").write_bytes(b"x")

    with pytest.raises(RestoreRefused):
        await backup.restore(target, root, sync_roots=[])

    assert [path.name for path in root.iterdir()] == ["old"]


async def test_a_restore_from_a_drive_without_a_finished_generation_is_refused(tmp_path):
    (tmp_path / "drive" / "generations" / "20260302T010000000000Z").mkdir(parents=True)

    with pytest.raises(RestoreRefused):
        await backup.restore(tmp_path / "drive", tmp_path / "restored", sync_roots=[])

    assert not (tmp_path / "restored").exists()


async def test_a_restore_refuses_a_blob_the_drive_holds_damaged(tmp_path):
    target = tmp_path / "drive"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        pdf = await imported(core, tmp_path, b"%PDF week 1")
        await backup.back_up(core, target, clock=Clock())
    damaged = target / "blobs" / pdf[0:2] / pdf[2:4] / pdf
    os.chmod(damaged, stat.S_IRUSR | stat.S_IWUSR)
    damaged.write_bytes(b"%PDF week 7")

    with pytest.raises(RestoreRefused, match=pdf):
        await backup.restore(target, tmp_path / "restored", sync_roots=[])

    assert [path.name for path in (tmp_path / "restored").iterdir()] == ["hub.lock"]
    damaged.write_bytes(b"%PDF week 1")
    assert (await backup.restore(target, tmp_path / "restored", sync_roots=[])).generation == backup.generations(target)[-1]


async def test_a_restore_refuses_a_snapshot_the_drive_holds_damaged(tmp_path):
    target = tmp_path / "drive"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        generation = await backup.back_up(core, target, clock=Clock())
    with open(target / "generations" / generation.name / "campuslife.sqlite3", "ab") as snapshot:
        snapshot.write(b"\0")

    with pytest.raises(RestoreRefused, match="snapshot"):
        await backup.restore(target, tmp_path / "restored", sync_roots=[])

    assert [path.name for path in (tmp_path / "restored").iterdir()] == ["hub.lock"]


async def test_a_backup_holds_blob_removals_from_the_snapshot_to_the_copy(tmp_path, monkeypatch):
    held = []
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        await imported(core, tmp_path, b"%PDF week 1")
        for step in ("_snapshot", "_copy_blobs"):
            real = getattr(backup, step)
            monkeypatch.setattr(backup, step, lambda *args, real=real, step=step: (held.append((step, core.blobs.removals.locked())), real(*args))[1])

        await backup.back_up(core, tmp_path / "drive", clock=Clock())

    assert held == [("_snapshot", True), ("_copy_blobs", True)]


async def test_collection_waits_while_a_backup_holds_blob_removals(tmp_path):
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        async with core.blobs.removals:
            collecting = asyncio.create_task(core.blobs.collect_garbage(datetime.now(UTC)))
            await asyncio.sleep(0.05)
            assert not collecting.done()
        assert await collecting == []


async def test_a_backup_into_the_data_root_is_refused(tmp_path):
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        with pytest.raises(BackupRefused):
            await backup.back_up(core, tmp_path / "root" / "backup", clock=Clock())


async def test_a_blob_missing_from_the_hub_is_named_and_gets_no_backed_up_at(tmp_path):
    target = tmp_path / "drive"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        pdf = await imported(core, tmp_path, b"%PDF week 1")
        os.chmod(core.blobs.path_of(pdf), stat.S_IRUSR | stat.S_IWUSR)
        core.blobs.path_of(pdf).unlink()

        generation = await backup.back_up(core, target, clock=Clock())

        assert generation.missing == (pdf,)
        assert manifest_of(target, generation.name)["missing"] == [pdf]
        assert "backed_up_at" not in (await attachments(core))[pdf]


@pytest.mark.parametrize(
    ("last", "overdue"),
    [(None, True), (T0 - timedelta(days=6, hours=23), False), (T0 - timedelta(days=7, seconds=1), True)],
    ids=["never", "within the period", "past the period"],
)
def test_a_backup_is_overdue_once_the_warning_period_passed_since_the_last(last, overdue):
    record = None if last is None else backup.LastBackup("g", "/drive", last.strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    assert backup.is_overdue(record, now=T0, warn_after=timedelta(days=7)) is overdue
