"""What it costs to purge documents from the search index, for each FTS5 table shape, on synthetic text.

    uv run python tools/search_purge_bench.py [--blocks 100000] [--marked 300] [--dir DIR]

Every number comes from local synthetic text (tools/search_bench.py's corpus), never from real data.

Shapes, both under hangul-bigram@1 with a docs table beside them holding each text as stored and folded, as the
search index does, PRAGMA secure_delete on, WAL, and FTS5's own secure-delete option on:
  stored       the FTS5 table keeps the analyzed text it indexed
  contentless  content='' with contentless_delete=1

`--marked` blocks spread over the corpus each end in a marker word made only of Hangul syllables the corpus does not
use, so its bigrams fall between the corpus's terms across many leaves. A purge deletes those blocks in one
transaction, runs 'optimize' (rewriting the whole index), and empties the WAL (checkpoint TRUNCATE), as `forget`
does. Each shape is measured again without 'optimize' as a control.

The script reports the size, the time of the purge, how many marker bigrams the database and WAL files hold before and
after it, and how many terms of the FTS5 index still hold a marker syllable.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import random
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from campuslife_hub.search.analyzer import HANGUL_BIGRAM

SHAPES = {
    "stored": "",
    "contentless": ", content='', contentless_delete=1",
}
SEED = 29
MARKER_SYLLABLES = 6
BATCH = 256


def corpus(blocks: int) -> list[str]:
    spec = importlib.util.spec_from_file_location("search_bench", Path(__file__).with_name("search_bench.py"))
    bench = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bench  # dataclasses look their module up here
    spec.loader.exec_module(bench)
    return bench.corpus(blocks)


def marked(texts: list[str], count: int) -> tuple[list[str], dict[int, str]]:
    """texts with a marker word ending `count` blocks spread over them, and the markers by block."""
    used = set("".join(texts))
    rare = [chr(code) for code in range(0xAC00, 0xD7A4) if chr(code) not in used]
    rng = random.Random(SEED)
    step = max(1, len(texts) // count)
    markers = {i: "".join(rng.choice(rare) for _ in range(MARKER_SYLLABLES)) for i in range(0, len(texts), step)[:count]}
    return [f"{text} {markers[i]}" if i in markers else text for i, text in enumerate(texts)], markers


def bigrams(markers: dict[int, str]) -> set[str]:
    return {marker[j : j + 2] for marker in markers.values() for j in range(len(marker) - 1)}


def _file_bytes(path: Path) -> bytes:
    return b"".join(part.read_bytes() for part in (path, path.with_name(path.name + "-wal")) if part.exists())


@dataclass(frozen=True)
class Result:
    size: int
    """Of the files before the purge."""
    purge_s: float
    before: int
    after: int
    terms_after: int
    """Terms of the FTS5 index that still hold a marker syllable."""


def measure(shape: str, texts: list[str], markers: dict[int, str], directory: Path, *, optimize: bool = True) -> Result:
    path = directory / f"{shape}{'' if optimize else '-unoptimized'}.sqlite3"
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA secure_delete = ON")
    con.execute(f"CREATE VIRTUAL TABLE fts USING fts5(body{SHAPES[shape]}, tokenize='{HANGUL_BIGRAM.fts5_tokenize}')")
    con.execute("INSERT INTO fts(fts, rank) VALUES ('secure-delete', 1)")
    con.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, original TEXT NOT NULL, folded TEXT NOT NULL)")
    for start in range(0, len(texts), BATCH):
        con.execute("BEGIN")
        for i in range(start, min(start + BATCH, len(texts))):
            con.execute("INSERT INTO fts(rowid, body) VALUES (?, ?)", (i, HANGUL_BIGRAM.analyze(texts[i])))
            con.execute("INSERT INTO docs(id, original, folded) VALUES (?, ?, ?)", (i, texts[i], HANGUL_BIGRAM.fold_original(texts[i])))
        con.execute("COMMIT")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = path.stat().st_size
    wanted = [piece.encode() for piece in bigrams(markers)]
    held = _file_bytes(path)
    before = sum(piece in held for piece in wanted)

    started = time.perf_counter()
    con.execute("BEGIN")
    con.executemany("DELETE FROM fts WHERE rowid = ?", [(i,) for i in markers])
    con.executemany("DELETE FROM docs WHERE id = ?", [(i,) for i in markers])
    con.execute("COMMIT")
    if optimize:
        con.execute("INSERT INTO fts(fts) VALUES ('optimize')")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    purged = time.perf_counter() - started
    syllables = {syllable.encode() for marker in markers.values() for syllable in marker}
    terms_after = sum(any(syllable in bytes(term) for syllable in syllables) for (term,) in con.execute("SELECT term FROM fts_idx"))
    con.close()
    left = _file_bytes(path)
    return Result(size, purged, before, sum(piece in left for piece in wanted), terms_after)


def report(texts: list[str], markers: dict[int, str], directory: Path) -> None:
    total = len(bigrams(markers))
    for shape in SHAPES:
        for optimize in (True, False):
            r = measure(shape, texts, markers, directory, optimize=optimize)
            name = shape if optimize else f"{shape}, optimize 없음(대조)"
            print(f"| {name} | {r.size / 1e6:.1f} | {r.purge_s:.3f} | {r.before}/{total} → {r.after}/{total} | {r.terms_after} |")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--blocks", type=int, default=100_000)
    parser.add_argument("--marked", type=int, default=300)
    parser.add_argument(
        "--dir", type=Path, help="build the indexes in a new directory under DIR and keep them (default: a temporary directory, removed)"
    )
    args = parser.parse_args(argv)
    texts, markers = marked(corpus(args.blocks), args.marked)
    print(
        f"출처: 로컬 합성 (blocks {len(texts)}, chars {sum(map(len, texts))}, marked {len(markers)}) · "
        f"Python {sys.version.split()[0]} · SQLite {sqlite3.sqlite_version} · {platform.system()} {platform.machine()}"
    )
    print("| shape | size MB | purge s | marker bigrams before → after | index terms with a marker syllable |")
    print("|---|---|---|---|---|")
    if args.dir is None:
        with tempfile.TemporaryDirectory() as directory:
            report(texts, markers, Path(directory))
    else:
        report(texts, markers, Path(tempfile.mkdtemp(dir=args.dir)))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    main()
