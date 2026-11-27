import asyncio
import http.client
import http.server
import io
import json
import os
import re
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

from campuslife_hub import admin_client, admin_token, backup, host
from campuslife_hub.admin_auth import hub_proof
from campuslife_hub.cli import main
from campuslife_hub.core import open_core
from campuslife_hub.data_root import Layout
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


NO_CONSOLE = """
import sys
sys.stdin = sys.stdout = sys.stderr = None
{setup}
from campuslife_hub.cli import main
raise SystemExit(main({argv!r}))
"""
STORE = r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0"


def no_console(setup, argv):
    """The CLI run as pythonw runs it: every standard stream None."""
    return subprocess.run([sys.executable, "-c", NO_CONSOLE.format(setup=setup, argv=argv)], capture_output=True, timeout=60)


@pytest.mark.parametrize(
    ("refusal", "setup", "reason"),
    [("database too new", "", "schema version 999"), ("store python", f"sys.base_prefix = {STORE!r}", "Microsoft Store")],
    ids=["database too new", "store python"],
)
def test_without_a_console_serve_leaves_why_it_refused_in_the_log_once(tmp_path, capsys, refusal, setup, reason):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    if refusal == "database too new":
        with sqlite3.connect(root / "database" / "campuslife.sqlite3") as conn:
            conn.execute("PRAGMA user_version = 999")
        conn.close()

    finished = no_console(setup, ["--root", str(root), "serve", "--admin-port", "0"])

    assert (finished.returncode, finished.stdout, finished.stderr) == (1, b"", b"")
    log = root / "logs" / "hub.log"
    said = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert len([line for line in said if reason in line]) == 1


def test_without_a_console_init_on_a_store_python_creates_nothing(tmp_path):
    root = tmp_path / "CampusLife"

    finished = no_console(f"sys.base_prefix = {STORE!r}", ["--root", str(root), "init"])

    assert finished.returncode == 1
    assert not root.exists(), sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_without_a_console_a_refused_restore_leaves_the_root_empty(tmp_path):
    root = tmp_path / "Restored"

    finished = no_console("", ["--root", str(root), "restore", "--from", str(tmp_path / "no-drive")])

    assert finished.returncode == 1
    assert not root.exists(), sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_without_a_console_a_refused_restore_leaves_an_empty_root_it_was_given_empty(tmp_path):
    root = tmp_path / "Restored"
    root.mkdir()

    finished = no_console("", ["--root", str(root), "restore", "--from", str(tmp_path / "no-drive")])

    assert finished.returncode == 1
    assert list(root.iterdir()) == []


def test_without_a_console_a_failure_on_a_root_without_a_hub_writes_no_log_there(tmp_path):
    root = tmp_path / "Folder"
    root.mkdir()
    (root / "notes.txt").write_text("not a Hub", encoding="utf-8")

    finished = no_console("", ["--root", str(root), "ls"])

    assert finished.returncode == 1
    assert sorted(path.name for path in root.iterdir()) == ["notes.txt"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="chmod 000 keeps out only a POSIX user other than root")
def test_without_a_console_a_root_the_cli_may_not_look_into_still_ends_with_1(tmp_path, monkeypatch):
    shut = tmp_path / "shut"
    shut.mkdir()
    shut.chmod(0)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        assert main(["--root", str(shut / "CampusLife"), "stop"]) == 1
    finally:
        shut.chmod(0o700)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="chmod 000 keeps out only a POSIX user other than root")
def test_a_root_that_cannot_be_looked_into_holds_no_hub_and_says_so_without_raising(tmp_path):
    shut = tmp_path / "shut"
    shut.mkdir()
    shut.chmod(0)
    try:
        assert host.holds_hub(Layout(shut / "CampusLife")) is False
    finally:
        shut.chmod(0o700)


def test_without_a_console_a_serve_refused_for_want_of_a_hub_leaves_the_empty_root_empty(tmp_path):
    root = tmp_path / "Restored"
    root.mkdir()

    finished = no_console("", ["--root", str(root), "serve", "--admin-port", "0"])

    assert finished.returncode == 1
    assert list(root.iterdir()) == []


def test_with_a_console_the_reason_serve_refused_shows_once_and_is_in_the_log(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    with sqlite3.connect(root / "database" / "campuslife.sqlite3") as conn:
        conn.execute("PRAGMA user_version = 999")
    conn.close()
    capsys.readouterr()

    assert hub(root, "serve", "--admin-port", "0") == 1

    shown = [line for line in capsys.readouterr().err.splitlines() if "schema version 999" in line]
    assert len(shown) == 1
    assert shown[0].startswith("campuslife-hub: the database is at schema version 999")
    logged = (root / "logs" / "hub.log").read_text(encoding="utf-8").splitlines()
    assert len([line for line in logged if "schema version 999" in line]) == 1


def test_without_a_console_an_unexpected_error_that_ends_serve_is_in_the_log_once(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    setup = (
        "import campuslife_hub.host as h\n"
        "async def ended(*args, **kwargs):\n"
        "    raise RuntimeError('the admin listener ended')\n"
        "h.serve = ended"
    )

    finished = no_console(setup, ["--root", str(root), "serve", "--admin-port", "0"])

    assert finished.returncode == 1
    said = (root / "logs" / "hub.log").read_text(encoding="utf-8").splitlines()
    assert len([line for line in said if "the admin listener ended" in line]) == 1


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
        assert "backup never (overdue)" in status
        assert hub(root, "backup", "--to", str(tmp_path / "drive"), "--keep", "2") == 0
        assert capsys.readouterr().out.startswith("generation ")
        assert hub(root, "status") == 0
        assert re.search(rf"backup last \S+ {re.escape(str((tmp_path / 'drive').resolve()))}\n", capsys.readouterr().out)
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


def test_a_serve_refused_for_want_of_a_hub_leaves_the_empty_root_empty_for_restore(tmp_path, capsys):
    source_root, restored, drive = tmp_path / "CampusLife", tmp_path / "Restored", tmp_path / "drive"
    hub(source_root, "init")
    assert hub(source_root, "backup", "--to", str(drive)) == 0
    restored.mkdir()
    capsys.readouterr()

    assert hub(restored, "serve", "--admin-port", "0") == 1
    assert "no Hub at" in capsys.readouterr().err
    assert list(restored.iterdir()) == []

    assert hub(restored, "restore", "--from", str(drive)) == 0


@contextmanager
def program_on_the_port(root, *, token=None, answer=None, stall=None, hello_status=200):
    """An HTTP server on 127.0.0.1 whose port root's admin.port names, recording each request it gets. With token it
    proves itself as a Hub listening there would; every other request gets answer."""
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
                reply = {"nonce": "hub-nonce", "proof": hub_proof(token, base.removeprefix("http://"), nonce, "hub-nonce") if token else "0" * 64}
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


@contextmanager
def relaying_to(port):
    """An HTTP server on 127.0.0.1 that passes each request on to the listener on port as if it had been sent
    there, recording what that listener answers."""
    answered = []

    class Relaying(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.relay()

        def do_POST(self):
            self.relay()

        def relay(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            headers = {name: value for name, value in self.headers.items() if name.lower() not in ("host", "connection")}
            onward = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            onward.request(self.command, self.path, body=body or None, headers={**headers, "host": f"127.0.0.1:{port}"})
            response = onward.getresponse()
            data = response.read()
            onward.close()
            answered.append((self.command, self.path, response.status, data))
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in ("connection", "content-length", "date", "server", "transfer-encoding"):
                    self.send_header(name, value)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relaying)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield server.server_address[1], answered
    finally:
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


def test_a_program_that_relays_to_the_hub_from_the_port_admin_port_names_gets_nothing_it_can_use(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    process = served(root)
    port = int((root / "admin.port").read_text())
    try:
        with relaying_to(port) as (relay, answered):
            (root / "admin.port").write_text(f"{relay}\n")
            assert hub(root, "open", "--no-browser") != 0
            (root / "admin.port").write_text(f"{port}\n")
        logins = [login.decode() for *_, data in answered for login in re.findall(rb"/login\?code=[A-Za-z0-9_-]+", data)]
        sessions = []
        for login in logins:
            browser = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            browser.request("GET", login)
            sessions.append(browser.getresponse().status)
            browser.close()

        assert sessions == []
        assert [(method, path, status) for method, path, status, _ in answered] == [("POST", "/api/hello", 401)]
        assert "is not the Hub" in capsys.readouterr().err
        assert hub(root, "stop") == 0
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_a_call_to_a_percent_encoded_path_is_signed_as_the_hub_reads_it(tmp_path):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    process = served(root)
    try:
        status = admin_client.call(Layout(root), "GET", "/api/st%61tus")
        assert hub(root, "stop") == 0
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()

    assert status["data_root"] == str(root.resolve())


def test_a_hub_that_takes_the_lock_the_moment_the_cli_lets_it_go_keeps_the_port_it_leaves(tmp_path, monkeypatch):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    (root / "admin.port").write_text("9\n")
    release = HubLock.release

    def and_a_hub_takes_it(lock):
        release(lock)
        (root / "admin.port").write_text("8731\n")

    monkeypatch.setattr(HubLock, "release", and_a_hub_takes_it)

    with pytest.raises(admin_client.HubNotServing):
        admin_client.call(Layout(root), "GET", "/api/status")

    assert (root / "admin.port").read_text() == "8731\n"


def test_serve_refuses_an_admin_token_file_that_holds_no_token(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    (root / "admin.token").write_bytes(b"")
    (root / "admin.token").chmod(0o600)
    capsys.readouterr()

    assert hub(root, "serve", "--admin-port", "0") != 0

    err = capsys.readouterr().err
    assert "admin token" in err and "Traceback" not in err


def test_backup_restore_and_fsck_from_the_cli(tmp_path, capsys):
    root, restored, drive = tmp_path / "CampusLife", tmp_path / "Restored", tmp_path / "drive"
    hub(root, "init")
    hub(root, "import", str(source(tmp_path, "week4.txt")))
    capsys.readouterr()

    assert hub(root, "backup", "--to", str(drive)) == 0
    made = capsys.readouterr().out
    assert hub(root, "fsck") == 0
    checked = capsys.readouterr().out
    assert hub(restored, "restore", "--from", str(drive)) == 0
    said = capsys.readouterr().out
    assert hub(restored, "fsck") == 0
    capsys.readouterr()
    assert hub(restored, "ls") == 0

    generation = made.split()[1]
    assert "blobs 1 copied 1" in made
    assert checked.strip() == "no findings"
    assert f"restored generation {generation}" in said
    assert "Every device must be approved again; do not approve a device that was lost or revoked." in said
    assert "purge those files again" in said
    assert "week4.txt" in capsys.readouterr().out


def test_a_backup_with_no_hub_serving_removes_no_generation_unless_told_how_many_to_keep(tmp_path, capsys):
    root, drive = tmp_path / "CampusLife", tmp_path / "drive"
    hub(root, "init")
    capsys.readouterr()

    for _ in range(backup.DEFAULT_KEEP + 1):
        assert hub(root, "backup", "--to", str(drive)) == 0
    unpruned = capsys.readouterr().out
    assert len(backup.generations(drive)) == backup.DEFAULT_KEEP + 1
    assert hub(root, "backup", "--to", str(drive), "--keep", "2") == 0
    pruned = capsys.readouterr().out

    assert unpruned.count("prune skipped") == backup.DEFAULT_KEEP + 1 and "removed generation" not in unpruned
    assert len(backup.generations(drive)) == 2
    assert pruned.count("removed generation") == backup.DEFAULT_KEEP and "prune skipped" not in pruned


def test_fsck_names_each_finding_and_exits_non_zero(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    stray = root / "blobs" / "ab" / "ab" / ("ab" * 32)
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"left by a crash")
    capsys.readouterr()

    assert hub(root, "fsck") == 1
    out, err = capsys.readouterr()
    assert f"orphan  {stray}  no blobs row names this file" in out
    assert "1 findings" in err


def exit_code(*args):
    try:
        return hub(*args)
    except SystemExit as exited:
        return exited.code


@pytest.mark.parametrize(("option", "said"), [
    ("--admin-port", "argument --admin-port: 'abc' is not a port (0 to 65535)"),
    ("--backup-keep", "argument --backup-keep: 'abc' is not a whole number of at least 1"),
])
def test_a_serve_option_that_is_not_a_number_is_refused_in_words_a_person_reads(tmp_path, capsys, option, said):
    assert exit_code(tmp_path / "CampusLife", "serve", option, "abc") == 2

    shown = capsys.readouterr().err
    assert said in shown
    assert "_port" not in shown and "_at_least_one" not in shown


@pytest.mark.parametrize(("command", "code", "said"), [
    (["restore", "--from", "{drive}"], 1, "is not empty"),
    (["backup", "--to", "{root}/inside"], 1, "inside the data root"),
    (["backup", "--to", "{drive}", "--keep", "0"], 2, "at least 1"),
    (["serve", "--backup-warn-days", "0"], 2, "at least 1"),
])
def test_backup_and_restore_say_why_they_refuse(tmp_path, capsys, command, code, said):
    root, drive = tmp_path / "CampusLife", tmp_path / "drive"
    hub(root, "init")
    hub(root, "backup", "--to", str(drive))
    capsys.readouterr()

    assert exit_code(root, *[part.format(drive=drive, root=root) for part in command]) == code

    assert said in capsys.readouterr().err
