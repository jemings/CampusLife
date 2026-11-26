import asyncio
import io
import os
import sqlite3
import subprocess
import sys

import pytest

from campuslife_hub.cli import main
from campuslife_hub.core import open_core
from campuslife_hub.lock import HubLock


def hub(root, *args):
    return main(["--root", str(root), *args])


def source(tmp_path, name, content=b"synthetic handout"):
    path = tmp_path / "inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


def test_init_then_import_then_ls_shows_the_capture(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    assert hub(root, "init") == 0
    assert hub(root, "import", str(source(tmp_path, "week4.txt")), str(source(tmp_path, "week5.txt", b"5"))) == 0
    capsys.readouterr()

    assert hub(root, "ls") == 0
    listing = capsys.readouterr().out
    assert "week4.txt" in listing and "week5.txt" in listing
    assert "import" in listing


def test_init_twice_keeps_the_hub_id(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    first = capsys.readouterr().out
    hub(root, "init")
    assert capsys.readouterr().out == first


def test_commands_other_than_init_need_an_initialised_root(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    assert hub(root, "ls") != 0
    assert "init" in capsys.readouterr().err
    assert not (root / "database").exists()


def test_import_of_a_missing_file_fails_without_a_capture(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    assert hub(root, "import", str(tmp_path / "nope.txt")) != 0
    capsys.readouterr()
    hub(root, "ls")
    assert capsys.readouterr().out == ""


def test_a_rejected_import_exits_non_zero_with_the_reason(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    missing_item = "item:7c9e6679-7425-40de-944b-e07fc1f90ae7"
    assert hub(root, "import", "--link", missing_item, str(source(tmp_path, "a.txt"))) != 0
    assert "link_target_missing" in capsys.readouterr().err


def test_a_second_writer_is_refused_while_the_hub_holds_the_lock(tmp_path):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    with HubLock.acquire(root / "hub.lock"):
        finished = subprocess.run(
            [sys.executable, "-m", "campuslife_hub.cli", "--root", str(root), "import", str(source(tmp_path, "a.txt"))],
            capture_output=True,
            text=True,
        )
    assert finished.returncode != 0
    assert "hub.lock" in finished.stderr


def test_init_refuses_a_root_inside_a_sync_folder(tmp_path, capsys, monkeypatch):
    onedrive = tmp_path / "OneDrive"
    onedrive.mkdir()
    monkeypatch.setenv("OneDrive", str(onedrive))
    assert hub(onedrive / "CampusLife", "init") != 0
    assert "sync folder" in capsys.readouterr().err


def database_of(root):
    return root / "database" / "campuslife.sqlite3"


def table_counts(conn):
    tables = [name for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")]
    return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


def test_ls_while_the_hub_holds_the_lock_reads_without_writing(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    hub(root, "import", str(source(tmp_path, "week4.txt")))
    capsys.readouterr()
    watcher = sqlite3.connect(database_of(root))
    before = (watcher.execute("PRAGMA user_version").fetchone()[0], table_counts(watcher), watcher.execute("PRAGMA data_version").fetchone()[0])

    with HubLock.acquire(root / "hub.lock"):
        assert hub(root, "ls") == 0
    after = (watcher.execute("PRAGMA user_version").fetchone()[0], table_counts(watcher), watcher.execute("PRAGMA data_version").fetchone()[0])
    watcher.close()

    assert "week4.txt" in capsys.readouterr().out
    assert after == before


def test_ls_refuses_an_empty_database_and_leaves_it_empty(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    database_of(root).parent.mkdir(parents=True)
    database_of(root).write_bytes(b"")
    with HubLock.acquire(root / "hub.lock"):
        assert hub(root, "ls") != 0
    assert "init" in capsys.readouterr().err
    with sqlite3.connect(database_of(root)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert table_counts(conn) == {}


def test_ls_refuses_a_database_of_an_older_schema(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    with sqlite3.connect(database_of(root)) as conn:
        conn.execute("PRAGMA user_version = 0")
    capsys.readouterr()
    assert hub(root, "ls") != 0
    assert "schema version 0" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names are bytes; Windows names are UTF-16")
def test_import_of_a_file_name_that_is_not_unicode_fails_with_a_message_and_no_blob(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    (tmp_path / "inbox").mkdir()
    bad = os.fsencode(tmp_path / "inbox") + b"/bad\xff.txt"
    with open(bad, "wb") as raw:
        raw.write(b"synthetic")
    capsys.readouterr()

    assert hub(root, "import", os.fsdecode(bad)) != 0
    assert capsys.readouterr().err.startswith("campuslife-hub: ")
    with sqlite3.connect(database_of(root)) as conn:
        assert conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0


def test_import_of_a_directory_fails_with_a_message(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    (tmp_path / "inbox").mkdir()
    capsys.readouterr()
    assert hub(root, "import", str(tmp_path / "inbox")) != 0
    assert capsys.readouterr().err.startswith("campuslife-hub: ")


async def test_import_is_refused_while_an_open_core_holds_the_lock(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    path = source(tmp_path, "a.txt")
    async with open_core(root, create=True, sync_roots=[]):
        capsys.readouterr()
        assert await asyncio.to_thread(hub, root, "import", str(path)) == 1
    assert "hub.lock" in capsys.readouterr().err


posix_mode_bits = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX mode bits stop only a user other than root"
)


@posix_mode_bits
def test_import_of_a_file_it_cannot_read_fails_with_a_message(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    unreadable = source(tmp_path, "unreadable.txt")
    unreadable.chmod(0)
    capsys.readouterr()
    try:
        assert hub(root, "import", str(unreadable)) == 1
    finally:
        unreadable.chmod(0o600)
    assert capsys.readouterr().err.startswith("campuslife-hub: ")


def test_ls_to_an_output_that_cannot_encode_a_file_name_fails_with_a_message(tmp_path, capsys, monkeypatch):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    hub(root, "import", str(source(tmp_path, "자료구조.txt")))
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="ascii"))

    assert hub(root, "ls") == 1
    assert capsys.readouterr().err.startswith("campuslife-hub: ")


def test_a_database_file_that_is_not_sqlite_fails_with_a_message(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    database_of(root).parent.mkdir(parents=True)
    database_of(root).write_bytes(b"synthetic bytes that are not a SQLite database\n" * 100)
    for command in (["ls"], ["import", str(source(tmp_path, "a.txt"))]):
        assert hub(root, *command) == 1
        assert capsys.readouterr().err.startswith("campuslife-hub: ")


@posix_mode_bits
def test_ls_in_a_read_only_database_directory_without_wal_files_fails_with_a_message(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    directory = database_of(root).parent
    assert [path.name for path in directory.iterdir()] == ["campuslife.sqlite3"]
    directory.chmod(0o555)
    capsys.readouterr()
    try:
        assert hub(root, "ls") == 1
    finally:
        directory.chmod(0o755)
    assert capsys.readouterr().err.startswith("campuslife-hub: ")


def test_ls_refuses_a_root_inside_a_sync_folder_it_detects_itself(tmp_path, capsys, monkeypatch):
    onedrive = tmp_path / "OneDrive"
    root = onedrive / "CampusLife"
    hub(root, "init")
    capsys.readouterr()
    monkeypatch.setenv("OneDrive", str(onedrive))

    assert hub(root, "ls") == 1
    assert "sync folder" in capsys.readouterr().err
