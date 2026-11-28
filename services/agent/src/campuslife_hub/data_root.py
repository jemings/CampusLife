"""Where the Hub keeps its data (D2, E23).

The verdict is a pure function of a path and the known sync roots; finding those
roots on this machine is the thin `detect_sync_roots` below it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath

APP_DIRECTORY = "CampusLife"


@dataclass(frozen=True)
class Allowed:
    root: PurePath


@dataclass(frozen=True)
class InsideSyncFolder:
    sync_root: PurePath


@dataclass(frozen=True)
class UncPath:
    drive: str


@dataclass(frozen=True)
class DevicePath:
    drive: str


DataRootVerdict = Allowed | InsideSyncFolder | UncPath | DevicePath

EXTENDED_DRIVE_LETTER = re.compile(r"\\\\\?\\[A-Za-z]:")
DEVICE_NAMESPACES = ("\\\\?\\", "\\\\.\\")


def _without_extended_prefix(path: PurePath) -> PurePath:
    r"""`\\?\C:\x` names the file `C:\x` names."""
    if EXTENDED_DRIVE_LETTER.fullmatch(path.drive):
        return type(path)(str(path)[len("\\\\?\\"):])
    return path


def _names_a_share(drive: str) -> bool:
    r"""`\\server\share`, `\\?\UNC\server\share` and `\\.\UNC\...` name a share; other device paths do not."""
    if drive.startswith(DEVICE_NAMESPACES):
        return drive[len("\\\\?\\"):].split("\\")[0].upper() == "UNC"
    return True


def check_data_root(path: PurePath, sync_roots: Iterable[PurePath]) -> DataRootVerdict:
    r"""The root to use, or why not: a `\\` drive, or at or under a sync folder.

    SQLite rejects the authority a `\\server\share` or `\\.\` drive puts in a file: URI,
    and SQLite and sync clients fight over files. A `\\?\C:` path is judged, and used, as `C:`.
    """
    path = _without_extended_prefix(path)
    if path.drive.startswith("\\\\"):
        return UncPath(path.drive) if _names_a_share(path.drive) else DevicePath(path.drive)
    for root in sync_roots:
        if path.is_relative_to(root):
            return InsideSyncFolder(root)
    return Allowed(path)


def onedrive_roots(env: Mapping[str, str]) -> list[str]:
    return [value for name, value in env.items() if name.lower().startswith("onedrive") and value]


def dropbox_roots(info_json: str) -> list[str]:
    """Roots listed in a Dropbox info.json: {"personal": {"path": ...}, "business": {...}}."""
    try:
        info = json.loads(info_json)
    except ValueError:
        return []
    if not isinstance(info, dict):
        return []
    accounts = [account for account in info.values() if isinstance(account, dict)]
    return [account["path"] for account in accounts if isinstance(account.get("path"), str)]


def _dropbox_info_files(env: Mapping[str, str]) -> list[Path]:
    # Where the Dropbox client documents its info.json on each platform.
    if sys.platform == "win32":
        return [Path(env[name]) / "Dropbox" / "info.json" for name in ("APPDATA", "LOCALAPPDATA") if env.get(name)]
    return [Path.home() / ".dropbox" / "info.json"]


def detect_sync_roots(env: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    found = onedrive_roots(env)
    for info in _dropbox_info_files(env):
        if info.is_file():
            found.extend(dropbox_roots(info.read_text(encoding="utf-8", errors="replace")))
    return [Path(root).resolve() for root in found]


def default_data_root(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if sys.platform == "win32":
        return Path(env["LOCALAPPDATA"]) / APP_DIRECTORY
    return Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_DIRECTORY


@dataclass(frozen=True)
class Layout:
    """Files and directories under one data root (ARCHITECTURE §6)."""

    root: Path

    @property
    def lock_file(self) -> Path:
        return self.root / "hub.lock"

    @property
    def database(self) -> Path:
        return self.root / "database" / "campuslife.sqlite3"

    @property
    def blobs(self) -> Path:
        return self.root / "blobs"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def uploads(self) -> Path:
        return self.root / "uploads"

    @property
    def admin_token(self) -> Path:
        return self.root / "admin.token"

    @property
    def admin_port(self) -> Path:
        """The port the running Hub's admin listener took; there only while it serves."""
        return self.root / "admin.port"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        """What the Hub rebuilds from the database and the Blobs when it is missing; never backed up (D9, E16)."""
        return self.root / "cache"

    @property
    def search_index(self) -> Path:
        return self.cache / "search.sqlite3"

    @property
    def work(self) -> Path:
        """Each running job's own directory, work/<job id>/, which its handler empties when it ends (S15); what a
        stopped Hub left goes when the Hub opens."""
        return self.root / "work"
