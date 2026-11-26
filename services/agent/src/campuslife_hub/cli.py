"""campuslife-hub: operate the Hub on one data root.

  init     create the data root, database and Hub identity (safe to repeat)
  import   store files as Blobs and commit them as one Capture
  ls       list Captures and their Attachments

init and import take the data root's hub.lock, so they refuse to run while
another Hub process holds it. ls takes no lock and writes no data.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from .core import UnsafeDataRoot, open_core, open_core_readonly
from .data_root import default_data_root
from .db import NotInitialized, SchemaMismatch, StartupRefused
from .listing import list_captures
from .local_import import UnimportableFile, import_files
from .lock import AlreadyRunning
from .migrations import DatabaseTooNew

ROOT_ENV = "CAMPUSLIFE_ROOT"
EXPECTED_FAILURES = (
    AlreadyRunning, NotInitialized, SchemaMismatch, UnsafeDataRoot, StartupRefused, DatabaseTooNew, UnimportableFile,
    OSError, UnicodeError, sqlite3.Error,
)


class CommandFailed(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="campuslife-hub", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, help=f"data root (default: ${ROOT_ENV}, else the platform default)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the data root and database")
    importing = commands.add_parser("import", help="import files as one Capture")
    importing.add_argument("files", nargs="+", type=Path)
    importing.add_argument("--link", action="append", default=[], metavar="item:<id>", help="course or lecture session Item to link")
    importing.add_argument("--policy", choices=["blocked", "local_only", "cloud_allowed"], help="cloud_policy for every file")
    commands.add_parser("ls", help="list Captures")
    return parser


async def _init(root: Path) -> None:
    async with open_core(root, create=True) as core:
        print(f"hub_id {core.identity.hub_id}")
        print(f"data root {core.layout.root}")


async def _import(root: Path, files: list[Path], links: list[str], policy: str | None) -> None:
    async with open_core(root, create=False) as core:
        imported = await import_files(core, files, links=links, cloud_policy=policy)
    if imported.result["result"] != "applied":
        raise CommandFailed(f"import {imported.result['result']}: {imported.result.get('reason') or imported.result}")
    print(f"capture {imported.capture_id}")


async def _ls(root: Path) -> None:
    async with open_core_readonly(root) as core:
        captures = await core.db.read(list_captures)
    for capture in captures:
        print(f"{capture.created_at}  {capture.id}  {capture.origin_type}")
        for attachment in capture.attachments:
            print(f"    {attachment.blob_sha256[:12]}  {attachment.cloud_policy:<13}  {attachment.original_filename or '-'}")


def _run(arguments: argparse.Namespace, root: Path) -> None:
    match arguments.command:
        case "init":
            asyncio.run(_init(root))
        case "import":
            asyncio.run(_import(root, arguments.files, arguments.link, arguments.policy))
        case "ls":
            asyncio.run(_ls(root))


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.root or Path(os.environ.get(ROOT_ENV) or default_data_root())
    try:
        _run(arguments, root)
    except (*EXPECTED_FAILURES, CommandFailed) as error:
        print(f"campuslife-hub: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
