"""The search index (DOMAIN-MODEL §11, D27): a cache beside the Hub database, built again from it whenever in doubt.

`<root>/cache/search.sqlite3` keeps a document for each text the Hub can show: an Item's title and body, an
Attachment's file name, and each block of the current extraction of a Blob with its Locators. A document holds the
text as stored, the folded text a LIKE scan reads, and, in an FTS5 table that keeps it, the analyzed text.

The index follows the database's changes feed past a cursor (`catch_up`) and writes the cursor in the transaction
that writes the documents. A purge hook (`forget`) drops what a purge removed. One lock keeps a batch of the follower,
the hook and a search apart, and a batch reads the database only once it holds the lock, so what a purge removed is
never written back after the hook ran.

The meta table names the Hub, its epoch and the index version (schema, analyzer, the Artifact types indexed). When
one of them is not what the database and this code say, the file is dropped and built again from since=0; a missing
file is built the same way. A new extractor version is not a reason: its Artifact is a change like any other.

The connection runs with PRAGMA secure_delete on and the FTS5 table with its own secure-delete option, and `forget`
empties the write-ahead log, so the bytes of what a purge removed leave the files (U2).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from campuslife_contracts import canonical_json

from .. import artifacts, store
from ..blobs import BlobStore
from ..data_root import Layout
from ..db import ReadOnlyDatabase, Snapshot
from ..identity import read_identity
from ..purge import PurgeTarget
from ..sqlite_probe import secure_delete_problems
from ..store import Entity, EntityRef
from .analyzer import HANGUL_BIGRAM, LIKE_ESCAPE, plan_query, storable

SCHEMA = 1
ANALYZER = HANGUL_BIGRAM
INDEXED_ARTIFACT_TYPES = ("extraction",)
INDEX_VERSION = f"{SCHEMA}:{ANALYZER.id}:{','.join(INDEXED_ARTIFACT_TYPES)}"
BATCH = 256
"""Feed rows a batch reads; a batch holds the lock, so a purge hook waits for at most one."""

_DDL = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE sources (source TEXT PRIMARY KEY, artifact_id TEXT, version TEXT NOT NULL)",
    "CREATE INDEX sources_by_artifact ON sources (artifact_id)",
    "CREATE TABLE docs (id INTEGER PRIMARY KEY, source TEXT NOT NULL, part TEXT NOT NULL, block_id TEXT, refs TEXT,"
    " original TEXT NOT NULL, folded TEXT NOT NULL)",
    "CREATE INDEX docs_by_source ON docs (source)",
    f"CREATE VIRTUAL TABLE fts USING fts5(body, tokenize='{ANALYZER.fts5_tokenize}')",
    "INSERT INTO fts (fts, rank) VALUES ('secure-delete', 1)",
)


class IndexUnusable(Exception):
    """The index file cannot be kept to the rules it needs (secure_delete)."""


class WalKept(Exception):
    """forget could not empty the write-ahead log; the purge stays unfinished and runs the hook again."""


@dataclass(frozen=True)
class Document:
    """One searchable text of a source: its `title`, `body` or `filename`, or a `block` of an extraction."""

    source: str
    part: str
    text: str
    block_id: str | None = None
    refs: str | None = None
    """The block's Locators as canonical JSON."""


@dataclass(frozen=True)
class Hit:
    source: str
    part: str
    text: str
    block_id: str | None
    refs: list[dict] | None


@dataclass(frozen=True)
class _Source:
    """What a source holds at one snapshot: its own texts, or the extraction Artifact whose blocks are its documents."""

    name: str
    documents: tuple[Document, ...] = ()
    artifact: dict | None = None

    @property
    def version(self) -> str:
        """Equal for equal documents: an Artifact never changes, so its id stands for its blocks."""
        if self.artifact is not None:
            return self.artifact["id"]
        listed = [[document.part, document.text] for document in self.documents]
        return hashlib.sha256(canonical_json(listed)).hexdigest()


def _item(snap: Snapshot, name: str, item_id: str) -> _Source:
    data = store.get(snap, EntityRef(Entity.ITEM, item_id))
    if data is None or "deleted_at" in data:
        return _Source(name)
    texts = [("title", data["title"]), *([("body", data["body"])] if data.get("body") else [])]
    return _Source(name, tuple(Document(name, part, text) for part, text in texts))


def _attachment(snap: Snapshot, name: str, attachment_id: str) -> _Source:
    data = store.get(snap, EntityRef(Entity.ATTACHMENT, attachment_id))
    if data is None or not data.get("original_filename"):
        return _Source(name)
    return _Source(name, (Document(name, "filename", data["original_filename"]),))


_READERS = {Entity.ITEM.value: _item, Entity.ATTACHMENT.value: _attachment}


def _subjects(snap: Snapshot, names: list[str]) -> dict[str, _Source]:
    found: dict[str, dict] = {}
    for artifact_type in reversed(INDEXED_ARTIFACT_TYPES):
        found |= artifacts.current_of(snap, names, artifact_type)
    return {name: _Source(name, artifact=found.get(name)) for name in names}


def _changed(snap: Snapshot, entity: str, identifier: str, deleted: bool, indexed_from: Callable[[str], list[str]]) -> list[str]:
    """The sources a feed row changes. A row of an entity the index does not hold, or one this code does not know,
    changes none (#11 decision 6)."""
    if entity in _READERS:
        return [f"{entity}:{identifier}"]
    if entity != Entity.ARTIFACT:
        return []
    if deleted:
        return indexed_from(identifier)
    artifact = store.get(snap, EntityRef(Entity.ARTIFACT, identifier))
    return [artifact["subject_ref"]] if artifact is not None and artifact["artifact_type"] in INDEXED_ARTIFACT_TYPES else []


def _read_changes(snap: Snapshot, cursor: int, indexed_from: Callable[[str], list[str]]) -> tuple[int, list[_Source]] | None:
    """The rev of the last feed row after cursor a batch takes, and what the sources those rows change hold now."""
    rows = snap.execute("SELECT entity, id, deleted, rev FROM sync_index WHERE rev > ? ORDER BY rev LIMIT ?", (cursor, BATCH)).fetchall()
    if not rows:
        return None
    names = list(dict.fromkeys(name for entity, identifier, deleted, _ in rows for name in _changed(snap, entity, identifier, bool(deleted), indexed_from)))
    subjects = _subjects(snap, [name for name in names if name.partition(":")[0] not in _READERS])
    sources = []
    for name in names:
        kind, _, identifier = name.partition(":")
        sources.append(_READERS[kind](snap, name, identifier) if kind in _READERS else subjects[name])
    return rows[-1][3], sources


def _expected_meta(snap: Snapshot) -> dict[str, str]:
    identity = read_identity(snap)
    return {"hub_id": identity.hub_id, "epoch": identity.epoch, "index_version": INDEX_VERSION}


def _documents_of(source: _Source, blobs: BlobStore) -> tuple[Document, ...]:
    """The documents of source, reading its extraction body when it has one; the Hub checked the body when it recorded it."""
    if source.artifact is None:
        return source.documents
    body = json.loads(blobs.path_of(source.artifact["content_blob"]).read_bytes())
    return tuple(
        Document(source.name, "block", block["text"], block["id"], canonical_json(block["refs"]).decode("utf-8"))
        for block in body["blocks"]
        if block.get("text")
    )


class SearchIndex:
    """The index file at `path`. Nothing is opened until it is first used, so the Core makes it before it finishes the
    purges a stopped Hub left, with its `forget` among the purge hooks."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @classmethod
    def open(cls, layout: Layout) -> SearchIndex:
        return cls(layout.search_index)

    # Under self._lock, on a worker thread.

    def _open(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                if problems := secure_delete_problems(conn):
                    raise IndexUnusable("; ".join(problems))
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() is None:
                    for statement in _DDL:
                        conn.execute(statement)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def _held(self) -> sqlite3.Connection | None:
        """The connection, or None when there is no index yet."""
        return None if self._conn is None and not self._path.exists() else self._open()

    def _drop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                self._path.with_name(self._path.name + suffix).unlink()

    def _for(self, expected: dict[str, str]) -> tuple[sqlite3.Connection, int]:
        """The connection to an index built for expected, and its cursor; a file built for anything else starts again."""
        conn = self._open()
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        if {key: meta.get(key) for key in expected} == expected:
            return conn, int(meta["cursor"])
        self._drop()
        conn = self._open()
        with _transaction(conn):
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [*expected.items(), ("cursor", "0")])
        return conn, 0

    def _indexed_from(self, conn: sqlite3.Connection) -> Callable[[str], list[str]]:
        return lambda artifact_id: [name for (name,) in conn.execute("SELECT source FROM sources WHERE artifact_id = ?", (artifact_id,))]

    @staticmethod
    def _holds(conn: sqlite3.Connection, source: _Source) -> bool:
        return conn.execute("SELECT 1 FROM sources WHERE source = ? AND version = ?", (source.name, source.version)).fetchone() is not None

    @staticmethod
    def _replace(conn: sqlite3.Connection, source: _Source, documents: Iterable[Document]) -> None:
        conn.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM docs WHERE source = ?)", (source.name,))
        conn.execute("DELETE FROM docs WHERE source = ?", (source.name,))
        conn.execute("DELETE FROM sources WHERE source = ?", (source.name,))
        if source.artifact is None and not source.documents:
            return
        artifact_id = None if source.artifact is None else source.artifact["id"]
        conn.execute("INSERT INTO sources (source, artifact_id, version) VALUES (?, ?, ?)", (source.name, artifact_id, source.version))
        for document in documents:
            text = storable(document.text)
            (doc_id,) = conn.execute(
                "INSERT INTO docs (source, part, block_id, refs, original, folded) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (source.name, document.part, document.block_id, document.refs, text, ANALYZER.fold_original(text)),
            ).fetchone()
            conn.execute("INSERT INTO fts (rowid, body) VALUES (?, ?)", (doc_id, ANALYZER.analyze(text)))

    def _batch(self, read: Callable[[Callable[[Snapshot], object]], object], blobs: BlobStore) -> int:
        with self._lock:
            conn, cursor = self._for(read(_expected_meta))
            changes = read(lambda snap: _read_changes(snap, cursor, self._indexed_from(conn)))
            if changes is None:
                return 0
            last_rev, sources = changes
            changed = [(source, _documents_of(source, blobs)) for source in sources if not self._holds(conn, source)]
            with _transaction(conn):
                for source, documents in changed:
                    self._replace(conn, source, documents)
                conn.execute("UPDATE meta SET value = ? WHERE key = 'cursor'", (str(last_rev),))
            return last_rev - cursor

    def _find(self, query: str, limit: int) -> list[Hit]:
        plan = plan_query(query, ANALYZER)
        with self._lock:
            conn = self._held()
            if plan is None or conn is None:
                return []
            scans = "".join(f" AND docs.folded LIKE ? ESCAPE '{LIKE_ESCAPE}'" for _ in plan.like)
            if plan.match is None:
                sql, params = f"SELECT source, part, original, block_id, refs FROM docs WHERE 1{scans} ORDER BY id LIMIT ?", (*plan.like, limit)
            else:
                sql = f"SELECT source, part, original, block_id, refs FROM fts JOIN docs ON docs.id = fts.rowid WHERE fts MATCH ?{scans} ORDER BY rank LIMIT ?"
                params = (plan.match, *plan.like, limit)
            return [Hit(source, part, text, block_id, None if refs is None else json.loads(refs)) for source, part, text, block_id, refs in conn.execute(sql, params)]

    def _documents(self) -> list[Document]:
        with self._lock:
            conn = self._held()
            return [] if conn is None else [Document(*row) for row in conn.execute("SELECT source, part, original, block_id, refs FROM docs ORDER BY source, id")]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def forget(self, target: PurgeTarget) -> None:
        """Drop every document of what the purge removed and empty the write-ahead log (U2); a purge hook, so it runs
        on a worker thread."""
        with self._lock:
            conn = self._held()
            if conn is None:
                return
            names = {
                *(f"blob:{sha}" for sha in target.blobs),
                *(f"attachment:{identifier}" for identifier in target.attachments),
                *(f"item:{identifier}" for identifier in target.items),
                *(name for artifact_id in target.artifacts for name in self._indexed_from(conn)(artifact_id)),
            }
            with _transaction(conn):
                for name in sorted(names):
                    self._replace(conn, _Source(name), ())
            (busy, _, _) = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise WalKept(f"the write-ahead log of {self._path} could not be emptied")

    # On the event loop; the work runs on worker threads.

    async def catch_up(self, db: ReadOnlyDatabase, blobs: BlobStore, *, stop: asyncio.Event | None = None) -> int:
        """Index what changed in db since the cursor, batch by batch, until the feed or stop ends it; the feed revs it passed."""
        loop = asyncio.get_running_loop()

        def read(fn):
            return asyncio.run_coroutine_threadsafe(db.read(fn), loop).result()

        passed = 0
        while not (stop is not None and stop.is_set()) and (advanced := await asyncio.to_thread(self._batch, read, blobs)):
            passed += advanced
        return passed

    async def find(self, query: str, limit: int = 20) -> list[Hit]:
        """The documents query finds in the whole index, best first; a query with nothing to search for finds none."""
        return await asyncio.to_thread(self._find, query, limit)

    async def documents(self) -> list[Document]:
        """Every document the index holds, by source: what an index built from scratch holds too."""
        return await asyncio.to_thread(self._documents)


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
