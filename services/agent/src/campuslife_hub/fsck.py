"""hub fsck (D16, E18): where the database and blobs/ disagree. It reads and writes nothing else.

- missing: a blobs row whose file is not there, or a BLOB_REFERENCES column naming a Blob with no row;
- corrupt: a file whose length or SHA-256 is not what its row and name say;
- orphan: a file under blobs/ no row names, or one not at blobs/<sha[0:2]>/<sha[2:4]>/<sha256>;
- unfinished_purge: a purge whose hooks or file removal are still to finish (E16).
A Hub serving the data root meanwhile can show a Blob it is storing or collecting at that moment.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .blobs import BLOB_REFERENCES
from .core import Core, ReadOnlyCore
from .db import Snapshot

SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    detail: str


@dataclass(frozen=True)
class _Recorded:
    sizes: dict[str, int]
    references: list[tuple[str, str]]
    unfinished_purges: list[tuple[str, str]]


def _recorded(snap: Snapshot) -> _Recorded:
    sizes = dict(snap.execute("SELECT sha256, size FROM blobs ORDER BY sha256").fetchall())
    references = [
        (f"{table}.{column}", sha256)
        for table, column in BLOB_REFERENCES
        for (sha256,) in snap.execute(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY 1")
    ]
    unfinished = snap.execute("SELECT id, registered_at FROM purge_journal WHERE finished_at IS NULL ORDER BY registered_at, rowid").fetchall()
    return _Recorded(sizes, references, unfinished)


def _digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _blob_of(blobs: Path, path: Path) -> str | None:
    """The sha256 a file under blobs/ is kept as, or None when it is not where a Blob is kept."""
    parts = path.relative_to(blobs).parts
    if len(parts) != 3 or not SHA256.fullmatch(parts[2]) or parts[:2] != (parts[2][0:2], parts[2][2:4]):
        return None
    return parts[2]


def _check_files(blobs: Path, recorded: _Recorded) -> list[Finding]:
    found = []
    for sha256, size in recorded.sizes.items():
        path = blobs / sha256[0:2] / sha256[2:4] / sha256
        if not path.is_file():
            found.append(Finding("missing", sha256, "the blobs row has no file"))
        elif path.stat().st_size != size:
            found.append(Finding("corrupt", sha256, f"the file is {path.stat().st_size} bytes, the row says {size}"))
        elif (actual := _digest(path)) != sha256:
            found.append(Finding("corrupt", sha256, f"the file hashes to {actual}"))
    reported = set()
    for column, sha256 in recorded.references:
        if sha256 not in recorded.sizes and sha256 not in reported:
            reported.add(sha256)
            found.append(Finding("missing", sha256, f"{column} names it but the Hub holds no such Blob"))
    files = sorted(path for path in blobs.rglob("*") if path.is_file()) if blobs.is_dir() else []
    for path in files:
        sha256 = _blob_of(blobs, path)
        if sha256 is None:
            found.append(Finding("orphan", str(path), "not where a Blob is kept"))
        elif sha256 not in recorded.sizes:
            found.append(Finding("orphan", str(path), "no blobs row names this file"))
    return found


async def check(core: Core | ReadOnlyCore) -> list[Finding]:
    """Every finding, rows and references first, then files, then purges; none for a sound data root."""
    recorded = await core.db.read(_recorded)
    found = await asyncio.to_thread(_check_files, core.layout.blobs, recorded)
    return found + [
        Finding("unfinished_purge", journal_id, f"registered {registered_at}; the Hub finishes it when it opens")
        for journal_id, registered_at in recorded.unfinished_purges
    ]
