"""Immediate purge of one Blob (U2, E16, D2), for a deletion an instructor or participant asks for.

One transaction removes:
- every Attachment of the Blob;
- the Artifacts holding it or made from it, and from those in turn (an input Artifact counts by its
  content Blob, artifact.input_hashes), and the Suggestions they proposed;
- the content Blobs of those Artifacts that nothing it keeps still names; an Artifact made from bytes a
  kept row still names stays, since those bytes stay;
- every Relation with an end at what it removes;
- the fork Notes the user chose to remove with them.
A fork Note it does not remove stays, marked with evidence_purged_at, and so does one created later
from an Artifact it removed. Jobs still to run on what it removes end cancelled. Each removed row
leaves its tombstone (E5).

The purge's journal row commits with those deletions. What lives outside the database goes after the
commit: each registered hook drops what it keeps (an index, #9), and the Blob files are removed. A purge
a crash, a failing hook or a file in use left unfinished is finished again when the Hub opens and at
each maintenance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, fields

from . import jobs, store
from .blobs import BLOB_REFERENCES, BlobStore, KeptLeftover, has_blob
from .clock import now_instant
from .db import Database, Snapshot, Tx
from .ingest import INGEST
from .jobs import JobState
from .store import Entity, EntityRef

log = logging.getLogger(__name__)

UNFINISHED_JOBS = (JobState.PENDING.value, JobState.RETRY_WAIT.value, JobState.RUNNING.value)


class PurgeRefused(Exception):
    pass


@dataclass(frozen=True)
class PurgeTarget:
    """What one purge removed: the Blobs whose files go, and the ids of the rows that went."""

    blobs: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    items: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps({member.name: list(getattr(self, member.name)) for member in fields(self)}, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> PurgeTarget:
        data = json.loads(text)
        return cls(**{member.name: tuple(data.get(member.name, ())) for member in fields(cls)})


@dataclass(frozen=True)
class ForkNote:
    id: str
    title: str
    deleted: bool


@dataclass(frozen=True)
class PurgePlan:
    """What purging `blob` removes, and the fork Notes it keeps and marks unless the user chooses them."""

    blob: str
    target: PurgeTarget
    fork_notes: tuple[ForkNote, ...]
    filenames: tuple[str, ...]


@dataclass(frozen=True)
class Finish:
    journal_id: str
    failed_hooks: tuple[str, ...] = ()
    kept: list[KeptLeftover] = field(default_factory=list)
    wal_kept: bool = False
    """The write-ahead log could not be emptied because a reader held it."""

    @property
    def finished(self) -> bool:
        return not self.failed_hooks and not self.kept and not self.wal_kept


@dataclass(frozen=True)
class Purged(Finish):
    target: PurgeTarget = PurgeTarget()
    marked: tuple[str, ...] = ()
    """The fork Notes kept and marked with evidence_purged_at."""
    cancelled: tuple[str, ...] = ()
    """The jobs it cancelled. One that was running goes on until its runner stops it (JobRunner.cancel)."""


Hook = Callable[[PurgeTarget], None]


class PurgeHooks:
    def __init__(self) -> None:
        self._hooks: dict[str, Hook] = {}

    def register(self, name: str, fn: Hook) -> None:
        """fn drops what it keeps outside the Hub database for the entities and Blobs of a target.

        It runs on a worker thread after the purge commits, and again after a crash or its own failure, so it
        accepts a target it has already seen."""
        if name in self._hooks:
            raise ValueError(f"a purge hook named {name} is registered already")
        self._hooks[name] = fn

    def items(self) -> list[tuple[str, Hook]]:
        return list(self._hooks.items())


HOOKS = PurgeHooks()
register = HOOKS.register


def _listed(values: Iterable[str]) -> str:
    return json.dumps(sorted(values))


def _ids(runner: Tx | Snapshot, sql: str, params: Sequence[object] | dict[str, object]) -> set[str]:
    return {identifier for (identifier,) in runner.execute(sql, params)}


def _doomed(runner: Tx | Snapshot, sha256: str, attachments: set[str]) -> tuple[set[str], set[str]]:
    """The Artifacts that go with sha256 and the Blobs that go. An Artifact goes when it holds a Blob that goes or
    was made from one; a content Blob goes, with sha256, once no row the purge keeps names it. The two grow together:
    a Blob a kept Artifact names stays only until that Artifact turns out to go as well."""
    made: dict[str, str] = {}
    blobs: set[str] = set()
    frontier = {sha256}
    while frontier:
        blobs |= frontier
        made.update(
            runner.execute(
                "SELECT id, content_blob FROM artifacts WHERE content_blob IN (SELECT value FROM json_each(:frontier))"
                " OR EXISTS (SELECT 1 FROM json_each(artifacts.data, '$.input_hashes') WHERE value IN (SELECT value FROM json_each(:frontier)))",
                {"frontier": _listed(frontier)},
            ).fetchall()
        )
        removed = {"attachments": attachments, "artifacts": set(made)}
        frontier = {content for content in set(made.values()) - blobs if not _named_elsewhere(runner, content, removed)}
    return set(made), blobs


def _named_elsewhere(runner: Tx | Snapshot, sha256: str, removed: dict[str, set[str]]) -> bool:
    """Whether a row the purge keeps names the Blob through BLOB_REFERENCES. A Relation does not keep it: the
    Relations at a Blob the purge removes go with it."""
    return any(
        runner.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ? AND id NOT IN (SELECT value FROM json_each(?))", (sha256, _listed(removed.get(table, ())))
        ).fetchone()
        for table, column in BLOB_REFERENCES
        if table != "relations"
    )


def _fork_notes(runner: Tx | Snapshot, artifacts: set[str]) -> list[dict]:
    return [
        json.loads(data)
        for (data,) in runner.execute(
            "SELECT data FROM items WHERE json_extract(data, '$.kind') = 'note'"
            " AND json_extract(data, '$.fields.forked_from_artifact') IN (SELECT value FROM json_each(?)) ORDER BY rowid",
            (_listed(artifacts),),
        )
    ]


def _reach(runner: Tx | Snapshot, sha256: str, items: Iterable[str]) -> tuple[PurgeTarget, list[dict]]:
    """The target of purging sha256 with the fork Notes in items, and every fork Note it reaches."""
    attachments = _ids(runner, "SELECT id FROM attachments WHERE blob_sha256 = ?", (sha256,))
    artifacts, blobs = _doomed(runner, sha256, attachments)
    if not (attachments or artifacts or has_blob(runner, sha256)):
        raise PurgeRefused(f"the Hub holds no Blob {sha256}")
    suggestions = _ids(runner, "SELECT id FROM suggestions WHERE source_artifact_id IN (SELECT value FROM json_each(?))", (_listed(artifacts),))
    items = set(items)
    ends = (
        {f"blob:{sha}" for sha in blobs} | {f"attachment:{i}" for i in attachments} | {f"artifact:{i}" for i in artifacts}
        | {f"suggestion:{i}" for i in suggestions} | {f"item:{i}" for i in items}
    )
    relations = _ids(
        runner,
        "SELECT id FROM relations WHERE json_extract(data, '$.from') IN (SELECT value FROM json_each(:ends))"
        " OR json_extract(data, '$.to') IN (SELECT value FROM json_each(:ends))",
        {"ends": _listed(ends)},
    )
    target = PurgeTarget(*(tuple(sorted(each)) for each in (blobs, attachments, artifacts, suggestions, relations, items)))
    return target, _fork_notes(runner, artifacts)


def plan(runner: Tx | Snapshot, sha256: str) -> PurgePlan:
    """What purging sha256 would remove, for the user to confirm; PurgeRefused when the Hub holds nothing of it."""
    target, notes = _reach(runner, sha256, ())
    filenames = [
        name
        for (name,) in runner.execute(
            "SELECT json_extract(data, '$.original_filename') FROM attachments WHERE id IN (SELECT value FROM json_each(?)) ORDER BY rowid",
            (_listed(target.attachments),),
        )
        if name is not None
    ]
    forks = tuple(ForkNote(note["id"], note["title"], "deleted_at" in note) for note in notes)
    return PurgePlan(sha256, target, forks, tuple(filenames))


def _cancel_jobs_on(tx: Tx, target: PurgeTarget) -> tuple[str, ...]:
    """Cancel the jobs still to run on what the purge removes: those whose subject goes, and those reading a Blob that
    goes. Not an ingest: it reads the Capture as it is when it runs, not its inputs, and a Capture left without
    Attachments still has its link or note to plan from."""
    subjects = [*(f"attachment:{i}" for i in target.attachments), *(f"artifact:{i}" for i in target.artifacts)]
    waiting = [
        job_id
        for (job_id,) in tx.execute(
            "SELECT id FROM processing_jobs WHERE state IN (SELECT value FROM json_each(:unfinished))"
            " AND (subject_ref IN (SELECT value FROM json_each(:subjects))"
            " OR (job_type != :ingest AND EXISTS (SELECT 1 FROM json_each(inputs) WHERE value IN (SELECT value FROM json_each(:blobs)))))"
            " ORDER BY rowid",
            {"unfinished": json.dumps(UNFINISHED_JOBS), "subjects": _listed(subjects), "ingest": INGEST, "blobs": _listed(target.blobs)},
        )
    ]
    for job_id in waiting:
        jobs.cancel(tx, job_id)
    return tuple(waiting)


def _purge(tx: Tx, sha256: str, chosen: tuple[str, ...]) -> tuple[str, PurgeTarget, tuple[str, ...], tuple[str, ...]]:
    target, notes = _reach(tx, sha256, chosen)
    not_reached = set(chosen) - {note["id"] for note in notes}
    if not_reached:
        raise PurgeRefused(f"purging {sha256} does not reach the Notes {sorted(not_reached)}; only its fork Notes can go with it")
    now = now_instant()
    cancelled = _cancel_jobs_on(tx, target)
    for entity, ids in (
        (Entity.RELATION, target.relations), (Entity.SUGGESTION, target.suggestions), (Entity.ARTIFACT, target.artifacts),
        (Entity.ATTACHMENT, target.attachments), (Entity.ITEM, target.items),
    ):
        for identifier in ids:
            store.write(tx, EntityRef(entity, identifier), None)
    tx.execute("DELETE FROM conflict_stash WHERE entity_id IN (SELECT value FROM json_each(?))", (_listed([*target.attachments, *target.items]),))
    marked = tuple(note["id"] for note in notes if note["id"] not in target.items)
    for note in notes:
        if note["id"] in marked:
            store.write(tx, EntityRef(Entity.ITEM, note["id"]), {**note, "evidence_purged_at": now})
    tx.execute("DELETE FROM blobs WHERE sha256 IN (SELECT value FROM json_each(?))", (_listed(target.blobs),))
    journal_id = str(uuid.uuid4())
    tx.execute("INSERT INTO purge_journal (id, target, registered_at) VALUES (?, ?, ?)", (journal_id, target.to_json(), now))
    return journal_id, target, marked, cancelled


async def _finish(db: Database, blobs: BlobStore, hooks: PurgeHooks, journal_id: str, target: PurgeTarget) -> Finish:
    """Run the hooks this purge has not yet run and remove its Blob files; finished once both are done."""
    (done,) = await db.read(lambda snap: snap.execute("SELECT hooks_done FROM purge_journal WHERE id = ?", (journal_id,)).fetchone())
    failed = []
    for name, hook in hooks.items():
        if name in json.loads(done):
            continue
        try:
            await asyncio.to_thread(hook, target)
        except Exception:
            log.exception("purge hook %s failed on purge %s; it runs again at the next maintenance", name, journal_id)
            failed.append(name)
            continue
        await db.write(
            lambda tx, name=name: tx.execute("UPDATE purge_journal SET hooks_done = json_insert(hooks_done, '$[#]', ?) WHERE id = ?", (name, journal_id))
        )
    kept: list[KeptLeftover] = []
    async with blobs.removals:
        for sha256 in target.blobs:
            kept += await blobs.discard(sha256)
    emptied = await db.truncate_wal()
    if not failed and not kept and emptied:
        await db.write(lambda tx: tx.execute("UPDATE purge_journal SET finished_at = ? WHERE id = ?", (now_instant(), journal_id)))
    return Finish(journal_id, tuple(failed), kept, not emptied)


async def purge_blob(db: Database, blobs: BlobStore, sha256: str, *, delete_notes: Iterable[str] = (), hooks: PurgeHooks = HOOKS) -> Purged:
    """Purge sha256 and what was made from it, with the fork Notes in delete_notes; PurgeRefused, writing nothing,
    when the Hub holds nothing of it or delete_notes names a Note it does not reach."""
    chosen = tuple(delete_notes)
    journal_id, target, marked, cancelled = await db.write(lambda tx: _purge(tx, sha256, chosen))
    finish = await _finish(db, blobs, hooks, journal_id, target)
    return Purged(journal_id, finish.failed_hooks, finish.kept, finish.wal_kept, target, marked, cancelled)


def purged_at(runner: Tx | Snapshot, artifact_id: str) -> str | None:
    """When a purge removed the Artifact; None when none did. Only a purge removes an Artifact, so this is when its
    tombstone was written."""
    (registered_at,) = runner.execute(
        "SELECT min(registered_at) FROM purge_journal WHERE EXISTS (SELECT 1 FROM json_each(target, '$.artifacts') WHERE value = ?)", (artifact_id,)
    ).fetchone()
    return registered_at


async def finish_unfinished(db: Database, blobs: BlobStore, hooks: PurgeHooks = HOOKS) -> list[Finish]:
    """Finish the purges left unfinished, oldest first; what each finish did."""
    rows = await db.read(lambda snap: snap.execute("SELECT id, target FROM purge_journal WHERE finished_at IS NULL ORDER BY registered_at, rowid").fetchall())
    return [await _finish(db, blobs, hooks, journal_id, PurgeTarget.from_json(target)) for journal_id, target in rows]
