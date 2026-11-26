import asyncio
import http.client
import http.server
import io
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager

import pytest

from campuslife_hub import admin_client, admin_token
from campuslife_hub.admin_auth import hub_proof
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


def served(root):
    """A `campuslife-hub serve` process on root, its output going to a file, once its admin listener has left its port."""
    with open(root.parent / "serve.out", "wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "campuslife_hub.cli", "--root", str(root), "serve", "--admin-port", "0"], stdout=output, stderr=output,
        )
    deadline = time.monotonic() + 30
    while not (root / "admin.port").exists():
        if process.poll() is not None or time.monotonic() > deadline:
            process.kill()
            process.wait()
            pytest.fail(f"serve did not start: {(root.parent / 'serve.out').read_text(encoding='utf-8', errors='replace')}")
        time.sleep(0.05)
    return process


def test_while_the_hub_serves_writes_go_through_its_admin_api_and_stop_ends_it(tmp_path, capsys, monkeypatch):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    hub_id = capsys.readouterr().out.split()[1]
    process = served(root)
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    try:
        assert hub(root, "init") == 0
        assert capsys.readouterr().out.split()[1] == hub_id
        assert hub(root, "import", "--policy", "blocked", str(source(tmp_path, "week4.txt"))) == 0
        capture = capsys.readouterr().out.split()[1]
        assert hub(root, "ls") == 0
        assert capture in capsys.readouterr().out
        assert hub(root, "status") == 0
        status = capsys.readouterr().out
        assert f"hub_id {hub_id}" in status and "pending=" in status and "runner restarts 0" in status
        assert hub(root, "open", "--no-browser") == 0
        url = capsys.readouterr().out.strip()
        port = int((root / "admin.port").read_text())
        assert url.startswith(f"http://127.0.0.1:{port}/login?code=")
        browser = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        browser.request("GET", url.removeprefix(f"http://127.0.0.1:{port}"))
        assert browser.getresponse().status == 303
        browser.close()

        assert hub(root, "stop") == 0
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()

    assert not (root / "admin.port").exists()
    token, code = admin_token.read(root / "admin.token"), url.partition("code=")[2]
    for written in ((root / "logs" / "hub.log").read_text(encoding="utf-8"), (tmp_path / "serve.out").read_text(encoding="utf-8")):
        assert "serving the admin listener on http://127.0.0.1:" in written
        assert "GET /login?code=[redacted]" in written
        assert token not in written and code not in written


@pytest.mark.skipif(sys.platform == "win32", reason="Ctrl+Break reaches only a process sharing a console; `stop` covers Windows")
@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_a_signal_stops_the_serving_hub_as_stop_does(tmp_path, signal_number):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    process = served(root)
    try:
        process.send_signal(signal_number)
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    assert not (root / "admin.port").exists()


@pytest.mark.parametrize("command", [["open", "--no-browser"], ["status"], ["stop"]])
def test_commands_for_a_serving_hub_say_when_none_serves(tmp_path, capsys, command):
    root = tmp_path / "CampusLife"
    hub(root, "init")

    assert hub(root, *command) != 0
    assert "no Hub is serving" in capsys.readouterr().err


def test_serve_needs_an_initialised_root_and_a_free_port(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    assert hub(root, "serve", "--admin-port", "0") != 0
    assert "no Hub at" in capsys.readouterr().err

    hub(root, "init")
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port, handlers = taken.getsockname()[1], signal.getsignal(signal.SIGINT)
    try:
        assert hub(root, "serve", "--admin-port", str(port)) != 0
    finally:
        taken.close()
    assert f"127.0.0.1:{port} cannot be listened on" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGINT) is handlers


@contextmanager
def program_on_the_port(root, *, token=None, answer=None, stall=None, hello_status=200):
    """An HTTP server on 127.0.0.1 whose port root's admin.port names, recording each request it gets. With token it
    proves itself as the Hub would, as a program relaying to the Hub could; every other request gets answer."""
    seen = []

    class Answering(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.answer()

        def do_POST(self):
            self.answer()

        def answer(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            seen.append((self.command, self.path, str(self.headers), body))
            if self.path == "/api/hello":
                nonce = json.loads(body)["nonce"]
                reply = {"nonce": "hub-nonce", "proof": hub_proof(token, nonce, "hub-nonce") if token else "0" * 64}
            else:
                if stall is not None:
                    stall.wait(30)
                reply = {"url": "http://evil.example/", "hub_id": "evil", "data_root": "evil", "jobs": {}, "stopping": True}
                reply |= answer(base) if answer is not None else {}
            data = json.dumps(reply).encode()
            self.send_response(hello_status if self.path == "/api/hello" else 200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answering)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    (root / "admin.port").write_text(f"{server.server_address[1]}\n")
    try:
        yield base, seen
    finally:
        if stall is not None:
            stall.set()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("hello_status", [200, 404], ids=["wrong proof", "hello refused"])
@pytest.mark.parametrize("command", [["status"], ["stop"], ["open", "--no-browser"]])
def test_a_program_that_holds_the_port_but_cannot_prove_it_is_the_hub_gets_no_secret(tmp_path, capsys, command, hello_status):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    hub(root, "import", str(source(tmp_path, "week4.txt")))
    token = admin_token.load_or_create(root / "admin.token")
    capsys.readouterr()

    with HubLock.acquire(root / "hub.lock"), program_on_the_port(root, hello_status=hello_status) as (_, seen):
        assert hub(root, *command) != 0

    out, err = capsys.readouterr()
    assert "is not the Hub" in err and "evil" not in out
    assert [(method, path) for method, path, *_ in seen] == [("POST", "/api/hello")]
    assert all(token not in headers and token.encode() not in body and "authorization" not in headers.lower() for _, _, headers, body in seen)


@pytest.mark.parametrize(
    "link",
    ["http://evil.example/login?code=abc", "{base}.evil.example/login?code=abc", "{base}/login?code=abc\x1b]0;evil\x07", "{base}/elsewhere?code=abc", 7],
)
def test_open_opens_only_a_login_link_to_the_hub_it_asked(tmp_path, capsys, monkeypatch, link):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    token = admin_token.load_or_create(root / "admin.token")
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    capsys.readouterr()

    with HubLock.acquire(root / "hub.lock"), program_on_the_port(root, token=token, answer=lambda base: {"url": link if isinstance(link, int) else link.format(base=base)}) as (_, seen):
        assert hub(root, "open") != 0

    out, err = capsys.readouterr()
    assert (opened, out) == ([], "")
    assert "not its own" in err
    assert [(method, path) for method, path, *_ in seen] == [("POST", "/api/hello"), ("POST", "/api/login")]
    assert all(token not in headers and token.encode() not in body for _, _, headers, body in seen)


def test_an_import_the_hub_does_not_answer_in_time_says_it_may_still_be_running(tmp_path, capsys, monkeypatch):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    token = admin_token.load_or_create(root / "admin.token")
    monkeypatch.setattr(admin_client, "REQUEST_TIMEOUT_S", 0.5)
    capsys.readouterr()

    with HubLock.acquire(root / "hub.lock"), program_on_the_port(root, token=token, stall=threading.Event()) as (_, seen):
        assert hub(root, "import", str(source(tmp_path, "week4.txt"))) != 0

    err = capsys.readouterr().err
    assert "did not answer POST /api/import" in err
    assert "may still be doing it" in err and "campuslife-hub ls" in err


def test_a_port_a_killed_hub_left_is_removed_and_nothing_is_sent_to_whatever_holds_it(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    process = served(root)
    process.kill()
    process.wait()
    assert (root / "admin.port").exists()
    capsys.readouterr()

    with program_on_the_port(root) as (_, seen):
        assert hub(root, "status") != 0
        assert hub(root, "stop") != 0

    assert "no Hub is serving" in capsys.readouterr().err
    assert seen == []
    assert not (root / "admin.port").exists()


def test_serve_refuses_an_admin_token_file_that_holds_no_token(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    (root / "admin.token").write_bytes(b"")
    (root / "admin.token").chmod(0o600)
    capsys.readouterr()

    assert hub(root, "serve", "--admin-port", "0") != 0

    err = capsys.readouterr().err
    assert "admin token" in err and "Traceback" not in err
