"""campuslife-hub: operate the Hub on one data root.

  init     create the data root, database and Hub identity (safe to repeat)
  serve    run the Hub: the admin listener and local UI on 127.0.0.1, the job runner, maintenance
  open     open the local UI in the browser through a one-time login link
  import   store files as Blobs and commit them as one Capture
  backup   write a backup generation to a folder on a backup drive and keep the newest ones
  restore  restore a backup generation into an empty data root, under a new epoch, every device dormant
  fsck     recompute every Blob's hash and find missing, corrupt and orphan Blobs
  status   what the serving Hub reports about itself
  stop     ask the serving Hub to stop
  ls       list Captures and their Attachments
  autostart  start the Hub at logon through Windows Task Scheduler: xml prints the task, register adds or
             replaces it, unregister removes it

While a Hub serves the data root it holds the data root's hub.lock, and init, import and backup go
through its admin API (E11); otherwise they take the lock themselves. restore takes the lock of the
empty root it fills. open, status and stop need a serving Hub. ls and fsck take no lock and write no data.
Every command refuses to run on a Microsoft Store Python (E21).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import webbrowser
from datetime import timedelta
from pathlib import Path

from . import admin_client, autostart, backup, fsck, host
from .admin_client import AdminRefused, HubDidNotAnswer, HubNotServing, NotTheHub
from .admin_token import AdminTokenUnusable
from .autostart import AutostartFailed, LogonTask
from .backup import DEFAULT_KEEP, DEFAULT_WARN_AFTER, BackupRefused, BackupSettings, RestoreRefused
from .core import UnsafeDataRoot, open_core, open_core_readonly, safe_data_root
from .data_root import Layout, default_data_root
from .db import NotInitialized, SchemaMismatch, StartupRefused
from .host import DEFAULT_ADMIN_PORT, HostConfig, PortTaken
from .listing import list_captures
from .local_import import UnimportableFile, import_files
from .lock import AlreadyRunning
from .migrations import DatabaseTooNew
from .redaction import Redactor, file_handler

ROOT_ENV = "CAMPUSLIFE_ROOT"
EXPECTED_FAILURES = (
    AlreadyRunning, NotInitialized, SchemaMismatch, UnsafeDataRoot, StartupRefused, DatabaseTooNew, UnimportableFile,
    HubNotServing, NotTheHub, AdminRefused, HubDidNotAnswer, AdminTokenUnusable, PortTaken, BackupRefused, RestoreRefused,
    AutostartFailed,
    OSError, UnicodeError, sqlite3.Error,
)


class CommandFailed(Exception):
    pass


def _at_least_one(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("at least 1")
    return value


def _port(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("a port is 0 to 65535")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="campuslife-hub", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, help=f"data root (default: ${ROOT_ENV}, else the platform default)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the data root and database")
    _serve_options(commands.add_parser("serve", help="run the Hub until Ctrl+C, a terminate request or `stop`"), defaults=True)
    opening = commands.add_parser("open", help="open the local UI of the serving Hub")
    opening.add_argument("--no-browser", action="store_true", help="only print the one-time login link")
    importing = commands.add_parser("import", help="import files as one Capture")
    importing.add_argument("files", nargs="+", type=Path)
    importing.add_argument("--link", action="append", default=[], metavar="item:<id>", help="course or lecture session Item to link")
    importing.add_argument("--policy", choices=["blocked", "local_only", "cloud_allowed"], help="cloud_policy for every file")
    backing_up = commands.add_parser("backup", help="write a backup generation to a folder on a backup drive")
    backing_up.add_argument("--to", type=Path, required=True, dest="target", metavar="FOLDER", help="the folder on the backup drive")
    backing_up.add_argument("--keep", type=_at_least_one, help="generations to keep (default: the serving Hub's setting; with no Hub serving, every one)")
    restoring = commands.add_parser("restore", help="restore a backup generation into the empty data root --root names")
    restoring.add_argument("--from", type=Path, required=True, dest="source", metavar="FOLDER", help="the folder on the backup drive")
    restoring.add_argument("--generation", help="the generation to restore (default: the newest finished one)")
    commands.add_parser("fsck", help="check the Blob store against the database")
    commands.add_parser("status", help="what the serving Hub reports")
    commands.add_parser("stop", help="ask the serving Hub to stop")
    commands.add_parser("ls", help="list Captures")
    starting = commands.add_parser("autostart", help="start the Hub at logon through Windows Task Scheduler")
    actions = starting.add_subparsers(dest="action", required=True)
    showing = _serve_options(actions.add_parser("xml", help="print the task register adds"), defaults=False)
    showing.add_argument("--out", type=Path, metavar="FILE", help="write it to FILE in the UTF-16 Task Scheduler reads, instead of printing it")
    _serve_options(_task_name(actions.add_parser("register", help="add the task for this user, or replace it")), defaults=False)
    _task_name(actions.add_parser("unregister", help="remove the task"))
    return parser


def _serve_options(parser: argparse.ArgumentParser, *, defaults: bool) -> argparse.ArgumentParser:
    """serve's options; without defaults, for a task that passes on to serve only the options given."""
    parser.add_argument(
        "--admin-port", type=_port, default=DEFAULT_ADMIN_PORT if defaults else None,
        help=f"port of the admin listener on 127.0.0.1 (default {DEFAULT_ADMIN_PORT})",
    )
    parser.add_argument(
        "--backup-keep", type=_at_least_one, default=DEFAULT_KEEP if defaults else None,
        help=f"generations a backup keeps on the drive (default {DEFAULT_KEEP})",
    )
    parser.add_argument(
        "--backup-warn-days", type=_at_least_one, default=DEFAULT_WARN_AFTER.days if defaults else None,
        help=f"warn when the last backup is older than this many days (default {DEFAULT_WARN_AFTER.days})",
    )
    return parser


def _task_name(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--name", default=autostart.TASK_NAME, help=f"the task's name in Task Scheduler (default {autostart.TASK_NAME!r})")
    return parser


def _layout(root: Path) -> Layout:
    return Layout(safe_data_root(root))


def _through_the_holder(root: Path, held: AlreadyRunning, method: str, path: str, body: object = None) -> dict:
    """Ask the Hub that holds the lock; a holder that serves no admin listener refuses as the lock does."""
    try:
        return admin_client.call(_layout(root), method, path, body)
    except HubNotServing:
        raise held from None


async def _init(root: Path) -> None:
    try:
        async with open_core(root, create=True) as core:
            hub_id, data_root = core.identity.hub_id, core.layout.root
    except AlreadyRunning as held:
        status = _through_the_holder(root, held, "GET", "/api/status")
        hub_id, data_root = status["hub_id"], status["data_root"]
    print(f"hub_id {hub_id}")
    print(f"data root {data_root}")


async def _import(root: Path, files: list[Path], links: list[str], policy: str | None) -> None:
    try:
        async with open_core(root, create=False) as core:
            imported = await import_files(core, files, links=links, cloud_policy=policy)
        capture_id, result = imported.capture_id, imported.result
    except AlreadyRunning as held:
        body = {"paths": [os.path.abspath(path) for path in files], "links": links, "cloud_policy": policy}
        answer = _through_the_holder(root, held, "POST", "/api/import", body)
        capture_id, result = answer["capture_id"], answer["result"]
    if result["result"] != "applied":
        raise CommandFailed(f"import {result['result']}: {result.get('reason') or result}")
    print(f"capture {capture_id}")


async def _backup(root: Path, target: Path, keep: int | None) -> None:
    """With no Hub serving, the generations to keep are known only from --keep: the data root does not hold the
    serving Hub's setting, so without --keep no generation is removed."""
    try:
        async with open_core(root, create=False) as core:
            made = await backup.back_up(core, target, keep=keep)
        report = {"generation": made.name, "target": str(made.target), "blobs": made.blobs, "copied": made.copied,
                  "missing": list(made.missing), "pruned": list(made.pruned)}
        skipped_prune = keep is None
    except AlreadyRunning as held:
        body = {"target": os.path.abspath(target), **({"keep": keep} if keep else {})}
        report = _through_the_holder(root, held, "POST", "/api/backup", body)
        skipped_prune = False
    print(f"generation {report['generation']}")
    print(f"target {report['target']}")
    print(f"blobs {report['blobs']} copied {report['copied']}")
    for name in report["pruned"]:
        print(f"removed generation {name}")
    if skipped_prune:
        print("prune skipped: no Hub serves this data root to say how many generations to keep; pass --keep N to keep only the newest N")
    if report["missing"]:
        raise CommandFailed(f"{len(report['missing'])} Blobs were missing from the Hub and are not in the backup; run `campuslife-hub fsck`")


async def _restore(root: Path, source: Path, generation: str | None) -> None:
    restored = await backup.restore(source, root, generation=generation)
    print(f"restored generation {restored.generation}, taken {restored.taken_at}, into {restored.root}")
    print(f"epoch {restored.epoch}")
    print("Every device must be approved again; do not approve a device that was lost or revoked.")
    print(f"Purges made after {restored.taken_at} are undone: purge those files again.")
    for device in restored.devices:
        print(f"device {device.platform} {device.name} paired {device.paired_at} dormant")


async def _fsck(root: Path) -> None:
    async with open_core_readonly(root) as core:
        found = await fsck.check(core)
    for finding in found:
        print(f"{finding.kind}  {finding.subject}  {finding.detail}")
    if found:
        raise CommandFailed(f"{len(found)} findings")
    print("no findings")


def _open(root: Path, browser: bool) -> None:
    url = admin_client.login_url(_layout(root))
    print(url)
    if browser:
        webbrowser.open(url)


def _status(root: Path) -> None:
    layout = _layout(root)
    status = admin_client.call(layout, "GET", "/api/status")
    print(f"hub_id {status['hub_id']}")
    print(f"data root {status['data_root']}")
    print(f"admin http://{admin_client.admin_address(layout)}")
    print("jobs " + " ".join(f"{state}={count}" for state, count in status["jobs"].items()))
    print(f"runner restarts {status['runner']['restarts']} last error {status['runner']['last_error'] or '-'}")
    last = status["backup"]["last"]
    said = "backup never" if last is None else f"backup last {last['finished_at']} {last['target']}"
    print(said + (" (overdue)" if status["backup"]["overdue"] else ""))


def _stop(root: Path) -> None:
    admin_client.call(_layout(root), "POST", "/api/stop")
    print("stopping")


async def _ls(root: Path) -> None:
    async with open_core_readonly(root) as core:
        captures = await core.db.read(list_captures)
    for capture in captures:
        print(f"{capture.created_at}  {capture.id}  {capture.origin_type}")
        for attachment in capture.attachments:
            print(f"    {attachment.blob_sha256[:12]}  {attachment.cloud_policy:<13}  {attachment.original_filename or '-'}")


def _logon_task(root: Path, arguments: argparse.Namespace) -> LogonTask:
    """The task that serves root with the serve options given; root must hold a Hub."""
    layout = _layout(root)
    if not layout.database.is_file():
        raise NotInitialized(f"no Hub at {layout.root}; run `campuslife-hub init` first")
    options = [(flag, value) for flag, value in (
        ("--admin-port", arguments.admin_port), ("--backup-keep", arguments.backup_keep), ("--backup-warn-days", arguments.backup_warn_days),
    ) if value is not None]
    serve = ["--root", str(layout.root), "serve", *[part for flag, value in options for part in (flag, str(value))]]
    return LogonTask(str(autostart.launcher()), tuple(serve), str(layout.root), autostart.this_user())


def _autostart(root: Path, arguments: argparse.Namespace) -> None:
    match arguments.action:
        case "xml" if arguments.out is not None:
            autostart.write_task(arguments.out, _logon_task(root, arguments))
            print(f"wrote {arguments.out}")
        case "xml":
            print(autostart.task_xml(_logon_task(root, arguments), declared=False), end="")
        case "register":
            task = _logon_task(root, arguments)
            autostart.register(task, arguments.name)
            print(f"registered {arguments.name!r}: {task.launcher} {' '.join(task.arguments)} at logon of {task.user}")
        case "unregister":
            autostart.unregister(arguments.name)
            print(f"unregistered {arguments.name!r}")


def _run(arguments: argparse.Namespace, root: Path) -> None:
    match arguments.command:
        case "init":
            asyncio.run(_init(root))
        case "serve":
            settings = BackupSettings(keep=arguments.backup_keep, warn_after=timedelta(days=arguments.backup_warn_days))
            host.run(host.run_hub(root, HostConfig(admin_port=arguments.admin_port, backup=settings)))
        case "open":
            _open(root, not arguments.no_browser)
        case "import":
            asyncio.run(_import(root, arguments.files, arguments.link, arguments.policy))
        case "backup":
            asyncio.run(_backup(root, arguments.target, arguments.keep))
        case "restore":
            asyncio.run(_restore(root, arguments.source, arguments.generation))
        case "fsck":
            asyncio.run(_fsck(root))
        case "status":
            _status(root)
        case "stop":
            _stop(root)
        case "ls":
            asyncio.run(_ls(root))
        case "autostart":
            _autostart(root, arguments)


def _leave_in_the_log(root: Path, said: str) -> None:
    """Without stderr, as under pythonw, the data root's log is where the reason a command failed can still be read; a
    log that cannot be written leaves it nowhere."""
    try:
        handler = file_handler(Layout(root).logs / host.LOG_FILE, Redactor())
    except OSError:
        return
    try:
        handler.handle(logging.LogRecord(__name__, logging.ERROR, __file__, 0, said, None, None))
    finally:
        handler.close()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.root or Path(os.environ.get(ROOT_ENV) or default_data_root())
    try:
        autostart.refuse_store_python()
        _run(arguments, root)
    except (*EXPECTED_FAILURES, CommandFailed) as error:
        said = f"campuslife-hub: {error}"
        if sys.stderr is not None:
            print(said, file=sys.stderr)
        elif host.LOGGED not in getattr(error, "__notes__", ()):
            _leave_in_the_log(root, said)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
