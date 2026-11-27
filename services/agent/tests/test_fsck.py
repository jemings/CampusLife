import hashlib
import json
import os
import stat
import subprocess
import sys

from campuslife_hub import fsck
from campuslife_hub.core import open_core, open_core_readonly
from campuslife_hub.local_import import import_files

CRASH = 72


def synthetic(tmp_path, name, content):
    path = tmp_path / "inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


async def a_hub(tmp_path, *contents):
    root = tmp_path / "root"
    async with open_core(root, create=True, sync_roots=[]) as core:
        for index, content in enumerate(contents):
            await import_files(core, [synthetic(tmp_path, f"{index}.pdf", content)])
        shas = await core.db.read(lambda snap: [sha for (sha,) in snap.execute("SELECT blob_sha256 FROM attachments ORDER BY rowid")])
    return root, shas


async def findings(root):
    async with open_core_readonly(root, sync_roots=[]) as core:
        return await fsck.check(core)


def blob_path(root, sha):
    return root / "blobs" / sha[0:2] / sha[2:4] / sha


async def test_a_sound_hub_has_no_findings(tmp_path):
    root, _ = await a_hub(tmp_path, b"%PDF one", b"%PDF two")

    assert await findings(root) == []


async def test_a_file_no_row_names_is_an_orphan(tmp_path):
    root, _ = await a_hub(tmp_path, b"%PDF one")
    stray = blob_path(root, "ab" * 32)
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"left by a crash")
    misfiled = root / "blobs" / "00" / "00" / ("ab" * 32)
    misnamed = root / "blobs" / "ab" / "cd" / "abcd-not-a-sha256"
    shallow = root / "blobs" / "zz" / "loose"
    for path in (misfiled, misnamed, shallow):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"?")

    assert await findings(root) == [fsck.Finding("orphan", str(misfiled), "not where a Blob is kept"),
                                    fsck.Finding("orphan", str(stray), "no blobs row names this file"),
                                    fsck.Finding("orphan", str(misnamed), "not where a Blob is kept"),
                                    fsck.Finding("orphan", str(shallow), "not where a Blob is kept")]


async def test_a_row_or_a_reference_without_its_file_is_missing(tmp_path):
    root, [first, second] = await a_hub(tmp_path, b"%PDF one", b"%PDF two")
    os.chmod(blob_path(root, first), stat.S_IRUSR | stat.S_IWUSR)
    blob_path(root, first).unlink()
    async with open_core(root, create=False, sync_roots=[]) as core:
        await core.db.write(lambda tx: tx.execute("DELETE FROM blobs WHERE sha256 = ?", (second,)))

    assert await findings(root) == [fsck.Finding("missing", first, "the blobs row has no file"),
                                    fsck.Finding("missing", second, "attachments.blob_sha256 names it but the Hub holds no such Blob"),
                                    fsck.Finding("orphan", str(blob_path(root, second)), "no blobs row names this file")]


async def test_a_file_whose_bytes_changed_is_corrupt(tmp_path):
    root, [sha] = await a_hub(tmp_path, b"%PDF one")
    path = blob_path(root, sha)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    path.write_bytes(b"%PDF eno")

    [finding] = await findings(root)

    assert (finding.kind, finding.subject) == ("corrupt", sha)
    assert "hashes to" in finding.detail


async def test_a_file_of_another_length_is_corrupt(tmp_path):
    root, [sha] = await a_hub(tmp_path, b"%PDF one")
    path = blob_path(root, sha)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    path.write_bytes(b"%PDF one and more")

    assert await findings(root) == [fsck.Finding("corrupt", sha, "the file is 17 bytes, the row says 8")]


async def test_an_unfinished_purge_is_a_finding(tmp_path):
    root, _ = await a_hub(tmp_path, b"%PDF one")
    async with open_core(root, create=False, sync_roots=[]) as core:
        await core.db.write(lambda tx: tx.execute(
            "INSERT INTO purge_journal (id, target, registered_at) VALUES ('j1', ?, '2026-03-02T00:30:00.000Z')", (json.dumps({"blobs": []}),)
        ))

    assert await findings(root) == [fsck.Finding("unfinished_purge", "j1", "registered 2026-03-02T00:30:00.000Z; the Hub finishes it when it opens")]


CRASHING_IMPORT = f"""
import asyncio, os, sys
from pathlib import Path
from campuslife_hub.blobs import BlobStore
from campuslife_hub.core import open_core
from campuslife_hub.local_import import import_files

def crash(self, tx, stored, ending):
    os._exit({CRASH})

async def main(root, source):
    BlobStore._register = crash
    async with open_core(Path(root), create=False, sync_roots=[]) as core:
        await import_files(core, [Path(source)])

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


async def test_after_a_crash_between_placing_a_file_and_recording_it_there_is_no_half_capture_and_fsck_reports_the_orphan(tmp_path):
    root, _ = await a_hub(tmp_path)
    source = synthetic(tmp_path, "crash.pdf", b"%PDF placed then crashed")

    crashed = subprocess.run([sys.executable, "-c", CRASHING_IMPORT, str(root), str(source)], capture_output=True, timeout=120)
    assert crashed.returncode == CRASH, crashed.stderr.decode(errors="replace")

    async with open_core(root, create=False, sync_roots=[]) as core:
        rows = await core.db.read(lambda snap: [snap.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("captures", "attachments", "blobs")])
    assert rows == [0, 0, 0]
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert await findings(root) == [fsck.Finding("orphan", str(blob_path(root, sha)), "no blobs row names this file")]
