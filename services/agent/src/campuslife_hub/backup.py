"""Backup and restore (E20, D16, U1, U3).

A backup folder on a drive holds:
- campuslife-backup.json: the id of the Hub whose backups the folder holds, written by its first
  backup. A backup refuses a folder that names another Hub, or that holds generations/ or blobs/
  without it, and one that holds the data root or lies in it;
- generations/<name>/campuslife.sqlite3: a snapshot of the database through the SQLite Online
  Backup API, keeping only the blobs rows some BLOB_REFERENCES column names;
- generations/<name>/manifest.json: written last, so a generation without one is a backup that did
  not finish, which restore skips and the next backup removes;
- blobs/<sha[0:2]>/<sha[2:4]>/<sha256>: every Blob a kept generation names, Artifacts' included,
  shared by the generations, so a backup copies only what the drive lacks.
Backups are not encrypted (U1). One runs when the user asks, from the local UI or the CLI (U3). It
holds BlobStore.removals from the snapshot to the end, so no Blob it names leaves blobs/ before it is
copied, and two backups do not overlap. Once the manifest is written, each Attachment whose Blob it
holds gets backed_up_at through store.write (D14): the end of the first generation that held the Blob,
since every later generation holds it as well while anything names it. Then the generations beyond the
newest `keep` go, with the Blobs no kept generation names; only what a backup writes goes, so a file
someone else put in the folder stays.

A restore goes into an empty data root, under its hub.lock. Before the snapshot takes the database's
place it gets a new epoch (D3), so every client syncs again from since=0 and sends again the Captures
the Hub no longer has, and every client device turns dormant (D6), to be approved again: a device
revoked after the backup is not active again, and one paired after it is unknown.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterable
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import store
from .blobs import BLOB_REFERENCES, READ_ONLY, BlobStore, copy_hashing, fsync_directory
from .clock import instant
from .core import Core, safe_data_root
from .data_root import Layout
from .db import BUSY_TIMEOUT_MS, Database, Snapshot, Tx
from .lock import HubLock
from .store import Entity, EntityRef

DEFAULT_KEEP = 5
DEFAULT_WARN_AFTER = timedelta(days=7)
FORMAT = 1
MARKER = "campuslife-backup.json"
GENERATIONS = "generations"
BLOBS = "blobs"
SNAPSHOT = "campuslife.sqlite3"
MANIFEST = "manifest.json"
PARTIAL = ".part"
GENERATION_FILES = (SNAPSHOT, SNAPSHOT + PARTIAL, SNAPSHOT + PARTIAL + "-journal", MANIFEST, MANIFEST + PARTIAL)
"""Every file a backup writes in a generation's folder."""
GENERATION_FORMAT = "%Y%m%dT%H%M%S%fZ"
GENERATION_NAME = re.compile(r"\d{8}T\d{12}Z")
SHA256 = re.compile(r"[0-9a-f]{64}")
FANOUT = re.compile(r"[0-9a-f]{2}")
COPY_PARTIAL = re.compile(r"\.copy-.*\.part")
WRITABLE = stat.S_IRUSR | stat.S_IWUSR


@dataclass(frozen=True)
class BackupSettings:
    """U3: how many generations a backup keeps on the drive, and how long after the last one the Hub warns."""

    keep: int = DEFAULT_KEEP
    warn_after: timedelta = DEFAULT_WARN_AFTER


class BackupRefused(Exception):
    pass


class RestoreRefused(Exception):
    pass


@dataclass(frozen=True)
class Generation:
    name: str
    target: Path
    started_at: str
    finished_at: str
    blobs: int
    """Blobs the generation holds."""
    copied: int
    """Of those, the ones this backup copied; the drive held the others already."""
    missing: tuple[str, ...]
    """Blobs the snapshot names whose files the Hub lacks; fsck says more."""
    pruned: tuple[str, ...]


@dataclass(frozen=True)
class LastBackup:
    generation: str
    target: str
    finished_at: str


@dataclass(frozen=True)
class KnownDevice:
    name: str
    platform: str
    paired_at: str


@dataclass(frozen=True)
class Restored:
    root: Path
    generation: str
    taken_at: str
    """When the snapshot was taken; a device paired after this pairs again."""
    epoch: str
    devices: tuple[KnownDevice, ...]
    """The client devices the restored Hub knows and did not count as revoked when the backup was taken, all dormant now."""


@dataclass(frozen=True)
class _Snapshot:
    hub_id: str
    epoch: str
    change_seq: int
    schema_version: int
    blobs: list[tuple[str, int]]
    sha256: str
    size: int


def _now() -> datetime:
    return datetime.now(UTC)


def _blob_at(directory: Path, sha256: str) -> Path:
    return directory / sha256[0:2] / sha256[2:4] / sha256


def _remove(path: Path) -> None:
    """Remove a file, read-only ones included (E9); one already gone is fine."""
    with suppress(FileNotFoundError):
        os.chmod(path, WRITABLE)
        path.unlink()


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, onexc=lambda _function, failed, _error: _remove(Path(failed)))


def _replace_durably(partial: Path, final: Path) -> None:
    os.replace(partial, final)
    fsync_directory(final.parent)


def _snapshot(database: Path, into: Path) -> _Snapshot:
    partial = into.with_name(into.name + PARTIAL)
    named = " UNION ".join(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL" for table, column in BLOB_REFERENCES)
    source = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    with closing(source), closing(sqlite3.connect(partial, isolation_level=None)) as copy:
        source.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        source.backup(copy)
        copy.execute("PRAGMA journal_mode = DELETE")
        copy.execute(f"DELETE FROM blobs WHERE sha256 NOT IN ({named})")
        blobs = copy.execute("SELECT sha256, size FROM blobs ORDER BY sha256").fetchall()
        hub_id, epoch, change_seq = copy.execute("SELECT hub_id, epoch, change_seq FROM hub_state").fetchone()
        (schema_version,) = copy.execute("PRAGMA user_version").fetchone()
    with open(partial, "rb") as file:
        sha256 = hashlib.file_digest(file, "sha256").hexdigest()
    _replace_durably(partial, into)
    return _Snapshot(hub_id, epoch, change_seq, schema_version, blobs, sha256, into.stat().st_size)


def _copy_blobs(blobs: BlobStore, into: Path, wanted: Iterable[tuple[str, int]]) -> tuple[int, tuple[str, ...]]:
    """Copy each wanted Blob the drive lacks; how many it copied, and the ones the Hub lacks or holds damaged."""
    copied, missing = 0, []
    into.mkdir(parents=True, exist_ok=True)
    for sha256, size in wanted:
        final = _blob_at(into, sha256)
        if final.is_file() and final.stat().st_size == size:
            continue
        source = blobs.path_of(sha256)
        if not source.is_file():
            missing.append(sha256)
            continue
        partial = into / f".copy-{uuid.uuid4()}{PARTIAL}"
        try:
            if copy_hashing([source], partial).sha256 != sha256:
                missing.append(sha256)
                continue
            final.parent.mkdir(parents=True, exist_ok=True)
            _remove(final)
            _replace_durably(partial, final)
        finally:
            _remove(partial)
        os.chmod(final, READ_ONLY)
        copied += 1
    return copied, tuple(missing)


def _write_json(path: Path, data: dict) -> None:
    partial = path.with_name(path.name + PARTIAL)
    with open(partial, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    _replace_durably(partial, path)


def _read_manifest(target: Path, name: str) -> dict:
    return json.loads((target / GENERATIONS / name / MANIFEST).read_text(encoding="utf-8"))


def generations(target: Path) -> list[str]:
    """The finished generations on a backup drive, oldest first."""
    directory = Path(target) / GENERATIONS
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir() if GENERATION_NAME.fullmatch(entry.name) and (entry / MANIFEST).is_file())


def _prune(target: Path, keep: int) -> tuple[str, ...]:
    """Keep the newest `keep` finished generations and the Blobs they name, and remove the other generations, unfinished
    ones too, and the Blobs no kept one names. It removes only what a backup writes: the files of GENERATION_FILES in a
    folder named like a generation, the Blob files under their fan-out folders, and its own copies left unfinished."""
    finished = generations(target)
    kept = finished[-keep:]
    for entry in sorted((target / GENERATIONS).iterdir()):
        if entry.name in kept or not GENERATION_NAME.fullmatch(entry.name) or not entry.is_dir():
            continue
        for name in GENERATION_FILES:
            _remove(entry / name)
        with suppress(OSError):
            entry.rmdir()
    named = {blob["sha256"] for name in kept for blob in _read_manifest(target, name)["blobs"]}
    blobs = target / BLOBS
    for path in sorted(blobs.glob("*/*/*")):
        first, second, name = path.relative_to(blobs).parts
        ours = FANOUT.fullmatch(first) and FANOUT.fullmatch(second) and SHA256.fullmatch(name) and path.is_file()
        if ours and (name not in named or (first, second) != (name[0:2], name[2:4])):
            _remove(path)
    for path in sorted(blobs.iterdir()):
        if COPY_PARTIAL.fullmatch(path.name) and path.is_file():
            _remove(path)
    for directory in [*sorted(blobs.glob("*/*")), *sorted(blobs.glob("*"))]:
        if all(FANOUT.fullmatch(part) for part in directory.relative_to(blobs).parts) and directory.is_dir():
            with suppress(OSError):
                directory.rmdir()
    return tuple(name for name in finished if name not in kept)


def _new_name(target: Path, started: datetime) -> str:
    """The generation's name: when it started, in UTC to the microsecond, and after every name on the drive, so
    pruning by name keeps the newest even after the clock went back or two backups started within one tick."""
    name = started.astimezone(UTC).strftime(GENERATION_FORMAT)
    directory = target / GENERATIONS
    taken = sorted(entry.name for entry in directory.iterdir() if GENERATION_NAME.fullmatch(entry.name)) if directory.is_dir() else []
    if taken and name <= taken[-1]:
        after = datetime.strptime(taken[-1], GENERATION_FORMAT).replace(tzinfo=UTC) + timedelta(microseconds=1)
        name = after.strftime(GENERATION_FORMAT)
    return name


def _record(tx: Tx, name: str, target: Path, finished_at: str, held: list[str], copied: int) -> None:
    tx.execute(
        "INSERT INTO backups (generation, target, finished_at, blob_count, copied_count) VALUES (?, ?, ?, ?, ?)",
        (name, str(target), finished_at, len(held), copied),
    )
    _stamp_backed_up_at(tx, held, finished_at)


def _stamp_backed_up_at(tx: Tx, held: list[str], finished_at: str) -> None:
    rows = tx.execute(
        "SELECT id, data FROM attachments WHERE blob_sha256 IN (SELECT value FROM json_each(?))"
        " AND json_extract(data, '$.backed_up_at') IS NULL ORDER BY rowid",
        (json.dumps(held),),
    ).fetchall()
    for attachment_id, data in rows:
        store.write(tx, EntityRef(Entity.ATTACHMENT, attachment_id), {**json.loads(data), "backed_up_at": finished_at})


def _claim(target: Path, hub_id: str) -> None:
    """Make `target` this Hub's backup folder; BackupRefused when its marker names another Hub, or when it has none
    and holds generations/ or blobs/, which a backup would then prune."""
    marker = target / MARKER
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get("hub_id") != hub_id:
            raise BackupRefused(f"{marker} does not name this Hub ({hub_id}); back up to another folder")
        return
    if marker.exists() or (target / GENERATIONS).exists() or (target / BLOBS).exists():
        raise BackupRefused(f"{target} holds {GENERATIONS} or {BLOBS} that no backup of this Hub wrote; back up to another folder")
    target.mkdir(parents=True, exist_ok=True)
    _write_json(marker, {"format": FORMAT, "hub_id": hub_id})


async def back_up(core: Core, target: Path, *, keep: int | None = DEFAULT_KEEP, clock: Callable[[], datetime] = _now) -> Generation:
    """Write a new generation of the Hub to `target` and keep the newest `keep`, or every generation when keep is
    None. BackupRefused before writing anything when keep is below one, when `target` and the data root lie one in
    the other, or when `target` is not this Hub's backup folder and cannot become one (_claim)."""
    if keep is not None and keep < 1:
        raise BackupRefused("keep at least one generation")
    target, root = Path(target).resolve(), core.layout.root.resolve()
    if target.is_relative_to(root):
        raise BackupRefused(f"{target} is inside the data root {root}; back up to another drive")
    if root.is_relative_to(target):
        raise BackupRefused(f"{target} holds the data root {root}; back up to a folder outside it")
    await asyncio.to_thread(_claim, target, core.identity.hub_id)
    started = clock()
    async with core.blobs.removals:
        name = _new_name(target, started)
        directory = target / GENERATIONS / name
        await asyncio.to_thread(directory.mkdir, parents=True)
        snapshot = await asyncio.to_thread(_snapshot, core.layout.database, directory / SNAPSHOT)
        copied, missing = await asyncio.to_thread(_copy_blobs, core.blobs, target / BLOBS, snapshot.blobs)
        held = [(sha256, size) for sha256, size in snapshot.blobs if sha256 not in missing]
        finished_at = instant(clock())
        manifest = {
            "format": FORMAT,
            "generation": name,
            "hub_id": snapshot.hub_id,
            "epoch": snapshot.epoch,
            "change_seq": snapshot.change_seq,
            "schema_version": snapshot.schema_version,
            "started_at": instant(started),
            "finished_at": finished_at,
            "database": {"file": SNAPSHOT, "sha256": snapshot.sha256, "size": snapshot.size},
            "blobs": [{"sha256": sha256, "size": size} for sha256, size in held],
            "copied": copied,
            "missing": list(missing),
        }
        await asyncio.to_thread(_write_json, directory / MANIFEST, manifest)
        await core.db.write(lambda tx: _record(tx, name, target, finished_at, [sha256 for sha256, _ in held], copied))
        pruned = () if keep is None else await asyncio.to_thread(_prune, target, keep)
    return Generation(name, target, instant(started), finished_at, len(held), copied, missing, pruned)


def last_backup(runner: Tx | Snapshot) -> LastBackup | None:
    row = runner.execute("SELECT generation, target, finished_at FROM backups ORDER BY finished_at DESC, rowid DESC LIMIT 1").fetchone()
    return None if row is None else LastBackup(*row)


def is_overdue(last: LastBackup | None, *, now: datetime, warn_after: timedelta) -> bool:
    """Whether to warn (U3): no backup yet, or none for warn_after."""
    return last is None or now - datetime.fromisoformat(last.finished_at) > warn_after


def _restore_blob(source: Path, layout: Layout, sha256: str) -> None:
    drive_copy = _blob_at(source / BLOBS, sha256)
    if not drive_copy.is_file():
        raise RestoreRefused(f"the backup drive lacks Blob {sha256}")
    layout.staging.mkdir(parents=True, exist_ok=True)
    partial = layout.staging / f"{uuid.uuid4()}{PARTIAL}"
    try:
        if copy_hashing([drive_copy], partial).sha256 != sha256:
            raise RestoreRefused(f"Blob {sha256} on the backup drive is damaged")
        final = _blob_at(layout.blobs, sha256)
        final.parent.mkdir(parents=True, exist_ok=True)
        _replace_durably(partial, final)
    finally:
        _remove(partial)
    os.chmod(final, READ_ONLY)


def _reissue(database: Path, epoch: str) -> None:
    """Give the snapshot its new epoch and make its client devices dormant before it takes the database's place, so a
    crash after that never serves the backup's epoch (D3) or a device revoked since the backup (D6)."""
    with closing(sqlite3.connect(database)) as copy, copy:
        copy.execute("UPDATE hub_state SET epoch = ?", (epoch,))
        copy.execute("UPDATE devices SET state = 'dormant' WHERE platform != 'windows_hub' AND state = 'active'")


def _place(source: Path, name: str, manifest: dict, layout: Layout, epoch: str) -> None:
    snapshot = source / GENERATIONS / name / manifest["database"]["file"]
    layout.database.parent.mkdir(parents=True, exist_ok=True)
    partial = layout.database.with_name(layout.database.name + PARTIAL)
    try:
        if copy_hashing([snapshot], partial).sha256 != manifest["database"]["sha256"]:
            raise RestoreRefused(f"the snapshot of generation {name} is damaged")
        _reissue(partial, epoch)
        for blob in manifest["blobs"]:
            _restore_blob(source, layout, blob["sha256"])
        _replace_durably(partial, layout.database)
    finally:
        _remove(partial)


def _after_restore(tx: Tx, manifest: dict, source: Path) -> tuple[KnownDevice, ...]:
    held = [blob["sha256"] for blob in manifest["blobs"]]
    tx.execute(
        "INSERT OR IGNORE INTO backups (generation, target, finished_at, blob_count, copied_count) VALUES (?, ?, ?, ?, ?)",
        (manifest["generation"], str(source), manifest["finished_at"], len(held), manifest["copied"]),
    )
    _stamp_backed_up_at(tx, held, manifest["finished_at"])
    devices = tx.execute(
        "SELECT device_name, platform, created_at FROM devices WHERE platform != 'windows_hub' AND state != 'revoked' ORDER BY created_at, id"
    ).fetchall()
    return tuple(KnownDevice(*device) for device in devices)


def _holds_only_its_lock(root: Path, layout: Layout) -> bool:
    return not root.exists() or (root.is_dir() and all(entry == layout.lock_file for entry in root.iterdir()))


async def restore(source: Path, root: Path, *, generation: str | None = None, sync_roots: Iterable[Path] | None = None) -> Restored:
    """Restore a generation, the newest finished one by default, into an empty data root under a new epoch, with every
    client device dormant.

    RestoreRefused, writing nothing, when the root is not empty or the drive has no such generation; and when the
    drive holds the generation damaged, after removing what it had placed."""
    source = Path(source).resolve()
    finished = generations(source)
    name = generation or (finished[-1] if finished else None)
    if name is None or name not in finished:
        raise RestoreRefused(f"{source} holds no finished backup generation {generation or ''}".rstrip())
    manifest = _read_manifest(source, name)
    if manifest.get("format") != FORMAT:
        raise RestoreRefused(f"generation {name} is in backup format {manifest.get('format')}; this Hub reads format {FORMAT}")
    layout = Layout(safe_data_root(root, sync_roots))
    if not _holds_only_its_lock(layout.root, layout):
        raise RestoreRefused(f"{layout.root} is not empty; restore into an empty or new data root")
    layout.root.mkdir(parents=True, exist_ok=True)
    epoch = str(uuid.uuid4())
    with HubLock.acquire(layout.lock_file) as lock:
        if not _holds_only_its_lock(layout.root, layout):
            raise RestoreRefused(f"{layout.root} is not empty; restore into an empty or new data root")
        try:
            await asyncio.to_thread(_place, source, name, manifest, layout, epoch)
            db = await Database.open(layout, lock=lock, create=False)
            try:
                devices = await db.write(lambda tx: _after_restore(tx, manifest, source))
            finally:
                await db.close()
        except BaseException:
            for placed in (layout.database.parent, layout.blobs, layout.staging):
                if placed.exists():
                    await asyncio.to_thread(_remove_tree, placed)
            raise
    return Restored(layout.root, name, manifest["started_at"], epoch, devices)
