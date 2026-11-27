"""Starting the Hub at logon (D16, E21): a Task Scheduler task that runs the venv's gui-script launcher.

The launcher is the `campuslife-hubw` gui-script `uv sync` puts next to the venv's python; it runs
pythonw, so the Hub has no console window, and its output goes to logs/hub.log. The task starts
it when this user logs on, never stops it for time or battery, and ignores a second start while
it runs. Until the user logs on after a reboot there is no Hub (D16); reminders do not need it (D19).

A Microsoft Store Python (installed under WindowsApps) redirects writes under %LOCALAPPDATA%, the
default data root, into its own package folder, so the Hub refuses to run on it.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .db import StartupRefused

TASK_NAME = "CampusLife Hub"
LAUNCHER = "campuslife-hubw"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
STORE_PYTHON_FOLDER = "windowsapps"


class AutostartFailed(Exception):
    pass


@dataclass(frozen=True)
class LogonTask:
    launcher: str
    arguments: Sequence[str]
    working_directory: str
    user: str
    """Whose logon starts the task, and who it runs as: DOMAIN\\name or name."""


def _element(parent: ET.Element, tag: str, text: str | None = None, **attributes: str) -> ET.Element:
    child = ET.SubElement(parent, tag, attributes)
    child.text = text
    return child


def task_xml(task: LogonTask, *, declared: bool = True) -> str:
    """The Task Scheduler task (schema 1.2) for task, declared as the UTF-16 write_task writes it in; undeclared, for
    text whose encoding is not known here, such as what a console shows or a shell redirects."""
    root = ET.Element("Task", {"version": "1.2", "xmlns": TASK_NAMESPACE})
    _element(_element(root, "RegistrationInfo"), "Description", "Starts the CampusLife Hub when you log on (campuslife-hub autostart).")
    trigger = _element(_element(root, "Triggers"), "LogonTrigger")
    _element(trigger, "Enabled", "true")
    _element(trigger, "UserId", task.user)
    principal = _element(_element(root, "Principals"), "Principal", id="Author")
    _element(principal, "UserId", task.user)
    _element(principal, "LogonType", "InteractiveToken")
    _element(principal, "RunLevel", "LeastPrivilege")
    settings = _element(root, "Settings")
    _element(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _element(settings, "DisallowStartIfOnBatteries", "false")
    _element(settings, "StopIfGoingOnBatteries", "false")
    _element(settings, "AllowStartOnDemand", "true")
    _element(settings, "Enabled", "true")
    _element(settings, "ExecutionTimeLimit", "PT0S")
    run = _element(_element(root, "Actions", Context="Author"), "Exec")
    _element(run, "Command", task.launcher)
    _element(run, "Arguments", subprocess.list2cmdline(task.arguments))
    _element(run, "WorkingDirectory", task.working_directory)
    ET.indent(root)
    document = ET.tostring(root, encoding="unicode") + "\n"
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + document if declared else document


def write_task(path: Path, task: LogonTask) -> None:
    """The file Task Scheduler reads: UTF-16 with a byte order mark."""
    path.write_text(task_xml(task), encoding="utf-16")


def launcher() -> Path:
    """The gui-script beside the python running this code, which is the venv's when run through uv."""
    return Path(sys.executable).with_name(f"{LAUNCHER}.exe" if sys.platform == "win32" else LAUNCHER)


def this_user(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    name = env.get("USERNAME") or getpass.getuser()
    return f"{env['USERDOMAIN']}\\{name}" if env.get("USERDOMAIN") else name


def store_python(paths: Iterable[str]) -> str | None:
    """The first of paths inside a WindowsApps folder, where the Microsoft Store installs Python and its aliases."""
    for path in paths:
        if any(part.lower() == STORE_PYTHON_FOLDER for part in PureWindowsPath(path).parts):
            return path
    return None


def refuse_store_python() -> None:
    running = [sys.executable, getattr(sys, "_base_executable", ""), sys.base_prefix]
    found = store_python(path for path in running if path)
    if found is not None:
        raise StartupRefused([
            f"{found} is a Microsoft Store Python, which redirects what the Hub writes under %LOCALAPPDATA% into its own"
            " package folder; install Python 3.12 with `uv python install 3.12` or from python.org and run `uv sync --frozen` again"
        ])


def _on_windows() -> None:
    if sys.platform != "win32":
        raise AutostartFailed("Task Scheduler is on Windows only; `campuslife-hub autostart xml` prints the task")


def _schtasks(*arguments: str) -> None:
    finished = subprocess.run(["schtasks", *arguments], capture_output=True, text=True, errors="replace")
    if finished.returncode != 0:
        said = (finished.stderr or finished.stdout).strip()
        raise AutostartFailed(f"schtasks {arguments[0]} failed: {said}")


def register(task: LogonTask, name: str = TASK_NAME) -> None:
    """Add the task under name, replacing a task of that name."""
    _on_windows()
    if not Path(task.launcher).is_file():
        raise AutostartFailed(f"no {task.launcher}; run `uv sync --frozen` in services/agent")
    with tempfile.TemporaryDirectory() as folder:
        written = Path(folder) / "task.xml"
        write_task(written, task)
        _schtasks("/Create", "/TN", name, "/XML", str(written), "/F")


def unregister(name: str = TASK_NAME) -> None:
    _on_windows()
    _schtasks("/Delete", "/TN", name, "/F")
