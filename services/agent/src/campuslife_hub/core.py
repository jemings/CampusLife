"""The Hub's core services over one data root, opened together and closed together.

The sync surface (#4) and the admin listener plug into a `Core`; they do not
open the database or the Blob store themselves.

`open_core` holds the data root's hub.lock for as long as the Core is open, so
it is the only way to the writer database and to migrations (E11).
`open_core_readonly` takes no lock and writes no data (see `ReadOnlyDatabase.open`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath

from . import jobs, purge
from .blobs import BlobStore
from .data_root import Allowed, DataRootVerdict, DevicePath, InsideSyncFolder, Layout, UncPath, check_data_root, detect_sync_roots
from .db import START_THE_HUB, Database, NotInitialized, ReadOnlyDatabase, StartupRefused
from .devices import DeviceId, ensure_hub_device
from .identity import HubIdentity, ensure_identity, read_identity
from .lock import HubLock
from .mutations import forget_old_results

log = logging.getLogger(__name__)


class UnsafeDataRoot(Exception):
    pass


@dataclass(frozen=True)
class Core:
    layout: Layout
    db: Database
    blobs: BlobStore
    identity: HubIdentity
    hub_device: DeviceId
    purge_hooks: purge.PurgeHooks = purge.HOOKS


@dataclass(frozen=True)
class ReadOnlyCore:
    layout: Layout
    db: ReadOnlyDatabase
    identity: HubIdentity


def _issue_identity_and_hub_device(tx) -> tuple[HubIdentity, DeviceId]:
    return ensure_identity(tx), ensure_hub_device(tx)


def accepted_root(root: PurePath, verdict: DataRootVerdict) -> PurePath:
    """The root a verdict allows, or the refusal it stands for."""
    match verdict:
        case Allowed(allowed):
            return allowed
        case UncPath(drive):
            raise StartupRefused([f"{root} is on the UNC share {drive}; choose a data root on a local drive letter"])
        case DevicePath(drive):
            raise StartupRefused([rf"{root} is on {drive}, a drive starting with \\; choose a data root on a local drive letter"])
        case InsideSyncFolder(sync_root):
            raise UnsafeDataRoot(f"{root} is inside the sync folder {sync_root}; choose a data root outside it")


def safe_data_root(root: Path, sync_roots: Iterable[Path] | None = None) -> Path:
    r"""The resolved root, refused on a `\\` drive or in a sync folder; sync_roots None detects them."""
    root = Path(root).resolve()
    known = detect_sync_roots() if sync_roots is None else [Path(path).resolve() for path in sync_roots]
    return Path(accepted_root(root, check_data_root(root, known)))


@asynccontextmanager
async def open_core(
    root: Path, *, create: bool, sync_roots: Iterable[Path] | None = None, purge_hooks: purge.PurgeHooks = purge.HOOKS
) -> AsyncIterator[Core]:
    """Open the Hub at `root` under its hub.lock; with create, make it first. Making it twice changes nothing.

    Taking the lock removes the admin.port an earlier Hub left, so the CLI does not ask whatever holds that port
    while this Hub opens. Opening finishes the purges a stopped Hub left unfinished (E16)."""
    layout = Layout(safe_data_root(root, sync_roots))
    if create:
        layout.root.mkdir(parents=True, exist_ok=True)
    elif not layout.database.is_file():
        raise NotInitialized(f"no Hub at {layout.root}; {START_THE_HUB}")
    with HubLock.acquire(layout.lock_file) as lock:
        with suppress(FileNotFoundError):
            layout.admin_port.unlink()
        db = await Database.open(layout, lock=lock, create=create)
        try:
            identity, hub_device = await db.write(_issue_identity_and_hub_device)
            blobs = BlobStore(db, blobs_dir=layout.blobs, staging_dir=layout.staging, uploads_dir=layout.uploads)
            now = datetime.now(UTC)
            kept = blobs.discard_staging_leftovers() + await blobs.expire_sessions(now) + await blobs.collect_garbage(now)
            await forget_old_results(db, now)
            recovered = await db.write(lambda tx: jobs.recover_after_restart(tx, now=now))
            if recovered:
                log.warning("%d jobs were running when the Hub last stopped; they wait again or failed", recovered)
            for finish in await purge.finish_unfinished(db, blobs, purge_hooks):
                kept += finish.kept
                if finish.failed_hooks:
                    log.warning("purge %s is still unfinished: hooks %s failed", finish.journal_id, ", ".join(finish.failed_hooks))
            for leftover in kept:
                log.warning("left %s for a later clean-up: %s", leftover.path, leftover.reason)
            yield Core(layout, db, blobs, identity, hub_device, purge_hooks)
        finally:
            await db.close()


@asynccontextmanager
async def open_core_readonly(root: Path, *, sync_roots: Iterable[Path] | None = None) -> AsyncIterator[ReadOnlyCore]:
    """Read the Hub at `root` without its lock; refuses a database this code did not migrate."""
    layout = Layout(safe_data_root(root, sync_roots))
    db = await ReadOnlyDatabase.open(layout.database)
    try:
        yield ReadOnlyCore(layout, db, await db.read(read_identity))
    finally:
        await db.close()
