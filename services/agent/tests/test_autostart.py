import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from campuslife_hub import autostart
from campuslife_hub.autostart import LogonTask, store_python, task_xml
from campuslife_hub.cli import main

TASK = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler and the gui-script launcher are Windows'")


def hub(root, *args):
    return main(["--root", str(root), *args])


def value(task, path):
    return task.findtext("/".join(f"{TASK}{step}" for step in path.split("/")))


def read_task(xml):
    return ET.fromstring(xml)


def test_the_task_starts_the_launcher_at_this_users_logon_and_never_stops_it():
    task = read_task(task_xml(LogonTask(
        launcher=r"C:\src\CampusLife\services\agent\.venv\Scripts\campuslife-hubw.exe",
        arguments=("--root", r"C:\Users\Kim Student\R&D\CampusLife", "serve", "--admin-port", "8800"),
        working_directory=r"C:\Users\Kim Student\R&D\CampusLife",
        user=r"LAPTOP\kim",
    )))

    assert task.tag == f"{TASK}Task"
    [trigger] = task.find(f"{TASK}Triggers")
    assert trigger.tag == f"{TASK}LogonTrigger"
    assert value(task, "Triggers/LogonTrigger/UserId") == r"LAPTOP\kim"
    assert value(task, "Principals/Principal/UserId") == r"LAPTOP\kim"
    assert value(task, "Principals/Principal/LogonType") == "InteractiveToken"
    assert value(task, "Principals/Principal/RunLevel") == "LeastPrivilege"
    assert value(task, "Settings/ExecutionTimeLimit") == "PT0S"
    assert value(task, "Settings/DisallowStartIfOnBatteries") == "false"
    assert value(task, "Settings/StopIfGoingOnBatteries") == "false"
    assert value(task, "Settings/MultipleInstancesPolicy") == "IgnoreNew"
    [action] = task.find(f"{TASK}Actions")
    assert action.tag == f"{TASK}Exec"
    assert value(task, "Actions/Exec/Command") == r"C:\src\CampusLife\services\agent\.venv\Scripts\campuslife-hubw.exe"
    assert value(task, "Actions/Exec/Arguments") == r'--root "C:\Users\Kim Student\R&D\CampusLife" serve --admin-port 8800'
    assert value(task, "Actions/Exec/WorkingDirectory") == r"C:\Users\Kim Student\R&D\CampusLife"


def test_the_task_declares_the_utf_16_it_is_registered_in():
    xml = task_xml(LogonTask(launcher="hubw.exe", arguments=("serve",), working_directory=r"C:\x", user="kim"))

    assert xml.startswith('<?xml version="1.0" encoding="UTF-16"?>\n')


@pytest.mark.parametrize(
    ("env", "user"), [({"USERDOMAIN": "LAPTOP", "USERNAME": "kim"}, r"LAPTOP\kim"), ({"USERNAME": "kim"}, "kim")], ids=["domain", "no domain"]
)
def test_the_task_runs_as_the_user_windows_names_in_the_environment(env, user):
    assert autostart.this_user(env) == user


def test_autostart_xml_prints_the_task_for_the_initialised_root_with_the_serve_options_given(tmp_path, capsys, monkeypatch):
    root = tmp_path / "Campus Life"
    hub(root, "init")
    capsys.readouterr()
    monkeypatch.setenv("USERDOMAIN", "LAPTOP")
    monkeypatch.setenv("USERNAME", "kim")

    assert hub(root, "autostart", "xml", "--admin-port", "8800", "--backup-keep", "3") == 0

    task = read_task(capsys.readouterr().out)
    launcher = "campuslife-hubw.exe" if sys.platform == "win32" else "campuslife-hubw"
    assert value(task, "Actions/Exec/Command") == str(Path(sys.executable).with_name(launcher))
    assert value(task, "Actions/Exec/Arguments") == f'--root "{root.resolve()}" serve --admin-port 8800 --backup-keep 3'
    assert value(task, "Actions/Exec/WorkingDirectory") == str(root.resolve())
    assert value(task, "Triggers/LogonTrigger/UserId") == r"LAPTOP\kim"


def test_autostart_refuses_a_root_without_a_hub(tmp_path, capsys):
    assert hub(tmp_path / "CampusLife", "autostart", "xml") == 1
    assert "campuslife-hub init" in capsys.readouterr().err


def test_autostart_refuses_a_root_inside_a_sync_folder(tmp_path, capsys, monkeypatch):
    onedrive = tmp_path / "OneDrive"
    root = onedrive / "CampusLife"
    hub(root, "init")
    capsys.readouterr()
    monkeypatch.setenv("OneDrive", str(onedrive))

    assert hub(root, "autostart", "xml") == 1
    assert "sync folder" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="registers for real on Windows")
@pytest.mark.parametrize("command", ["register", "unregister"])
def test_autostart_registers_only_on_windows(tmp_path, capsys, command):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    capsys.readouterr()

    assert hub(root, "autostart", command) == 1
    assert "Windows" in capsys.readouterr().err


@pytest.mark.parametrize(
    "paths",
    [
        [r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0"],
        [r"C:\Users\kim\src\.venv\Scripts\python.exe",
         r"C:\Users\kim\AppData\Local\Microsoft\windowsapps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"],
    ],
    ids=["store install", "venv over the store alias"],
)
def test_a_microsoft_store_python_is_found(paths):
    assert store_python(paths) == paths[-1]


@pytest.mark.parametrize(
    "paths",
    [
        [r"C:\Users\kim\AppData\Local\Programs\Python\Python312"],
        [r"C:\Users\kim\AppData\Roaming\uv\python\cpython-3.12.10-windows-x86_64-none\python.exe"],
        ["/usr", "/usr/bin/python3.12"],
        [r"C:\Users\kim\WindowsAppsBackup\python.exe"],
    ],
    ids=["python.org", "uv managed", "posix", "a lookalike folder"],
)
def test_another_python_is_not_the_store_one(paths):
    assert store_python(paths) is None


def test_every_command_refuses_to_run_on_a_microsoft_store_python(tmp_path, capsys, monkeypatch):
    root = tmp_path / "CampusLife"
    monkeypatch.setattr(sys, "base_prefix", r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0")

    assert hub(root, "init") == 1

    assert "Microsoft Store" in capsys.readouterr().err
    assert not root.exists()


def schtasks_query(name):
    finished = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"], capture_output=True)
    if finished.returncode != 0:
        return None
    out = finished.stdout
    if out[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = out.decode("utf-16")
    elif b"\x00" in out:
        text = out.decode("utf-16-le")
    else:
        text = out.decode("ascii", errors="replace")
    return ET.fromstring(text.strip())


@windows_only
def test_autostart_register_adds_the_task_task_scheduler_reads_back_and_unregister_removes_it(tmp_path, capsys):
    root = tmp_path / "Campus Life"
    name = f"CampusLife Hub test {uuid.uuid4()}"
    hub(root, "init")
    capsys.readouterr()

    assert hub(root, "autostart", "register", "--name", name, "--admin-port", "8800") == 0
    try:
        task = schtasks_query(name)
        assert task is not None
        assert value(task, "Settings/ExecutionTimeLimit") == "PT0S"
        assert value(task, "Settings/DisallowStartIfOnBatteries") == "false"
        assert value(task, "Settings/StopIfGoingOnBatteries") == "false"
        assert value(task, "Settings/MultipleInstancesPolicy") == "IgnoreNew"
        assert task.find(f"{TASK}Triggers/{TASK}LogonTrigger") is not None
        assert value(task, "Principals/Principal/LogonType") == "InteractiveToken"
        assert value(task, "Actions/Exec/Command") == str(Path(sys.executable).with_name("campuslife-hubw.exe"))
        assert value(task, "Actions/Exec/Arguments") == f'--root "{root.resolve()}" serve --admin-port 8800'
        assert hub(root, "autostart", "register", "--name", name) == 0
        assert value(schtasks_query(name), "Actions/Exec/Arguments") == f'--root "{root.resolve()}" serve'
    finally:
        assert hub(root, "autostart", "unregister", "--name", name) == 0
    assert schtasks_query(name) is None


@windows_only
def test_the_gui_script_launcher_serves_the_hub_without_a_console_until_it_is_stopped(tmp_path, capsys):
    root = tmp_path / "CampusLife"
    hub(root, "init")
    process = subprocess.Popen([str(autostart.launcher()), "--root", str(root), "serve", "--admin-port", "0"])
    try:
        deadline = time.monotonic() + 30
        while not (root / "admin.port").exists():
            assert process.poll() is None and time.monotonic() < deadline, f"serve did not start; see {root / 'logs' / 'hub.log'}"
            time.sleep(0.05)
        capsys.readouterr()

        assert hub(root, "status") == 0
        assert hub(root, "stop") == 0
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
