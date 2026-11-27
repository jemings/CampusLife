"""What it costs to purge one document from the search index, for each FTS5 table shape, on synthetic text.

    uv run python tools/search_purge_bench.py [--blocks 100000] [--dir DIR]

Every number comes from local synthetic text (tools/search_bench.py's corpus), never from real data.

Shapes, both under hangul-bigram@1 with the folded original in a plain table beside them, PRAGMA
secure_delete on, WAL, and FTS5's own secure-delete option on:
  stored       the FTS5 table keeps the analyzed text it indexed
  contentless  content='' with contentless_delete=1; a delete leaves token bytes until 'optimize'
               rewrites the whole index, so its purge is delete + optimize

A purge deletes one document holding a marker and empties the WAL (checkpoint TRUNCATE). The script reports the
time of that purge and how many marker tokens are left in the database and WAL files after it.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from campuslife_hub.search.analyzer import HANGUL_BIGRAM

MARKER = "비밀표식 ZQXJ7731"
MARKER_TOKENS = ("zqxj7731", "비밀", "밀표", "표식")
SHAPES = {
    "stored": "",
    "contentless": ", content='', contentless_delete=1",
}


def corpus(blocks: int) -> list[str]:
    spec = importlib.util.spec_from_file_location("search_bench", Path(__file__).with_name("search_bench.py"))
    bench = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bench  # dataclasses look their module up here
    spec.loader.exec_module(bench)
    return bench.corpus(blocks)


def _left(path: Path) -> int:
    raw = path.read_bytes() + (wal.read_bytes() if (wal := path.with_name(path.name + "-wal")).exists() else b"")
    return sum(raw.count(token.encode()) for token in MARKER_TOKENS)


def measure(shape: str, texts: list[str], directory: Path) -> tuple[float, int, float, int, int]:
    """Build seconds, size, purge seconds, marker tokens left after the purge, and before it (the control)."""
    path = directory / f"{shape}.sqlite3"
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA secure_delete = ON")
    con.execute(f"CREATE VIRTUAL TABLE fts USING fts5(body{SHAPES[shape]}, tokenize='{HANGUL_BIGRAM.fts5_tokenize}')")
    con.execute("INSERT INTO fts(fts, rank) VALUES ('secure-delete', 1)")
    con.execute("CREATE TABLE original(id INTEGER PRIMARY KEY, folded TEXT NOT NULL)")
    started = time.perf_counter()
    con.execute("BEGIN")
    for i, text in enumerate([*texts, MARKER]):
        con.execute("INSERT INTO fts(rowid, body) VALUES (?, ?)", (i, HANGUL_BIGRAM.analyze(text)))
        con.execute("INSERT INTO original(id, folded) VALUES (?, ?)", (i, HANGUL_BIGRAM.fold_original(text)))
    con.execute("COMMIT")
    con.execute("INSERT INTO fts(fts) VALUES ('optimize')")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    built = time.perf_counter() - started
    size = path.stat().st_size
    before = _left(path)

    marker = len(texts)
    started = time.perf_counter()
    con.execute("BEGIN")
    con.execute("DELETE FROM fts WHERE rowid = ?", (marker,))
    con.execute("DELETE FROM original WHERE id = ?", (marker,))
    con.execute("COMMIT")
    if shape == "contentless":
        con.execute("INSERT INTO fts(fts) VALUES ('optimize')")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    purged = time.perf_counter() - started
    con.close()
    return built, size, purged, _left(path), before


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--blocks", type=int, default=100_000)
    parser.add_argument(
        "--dir", type=Path, help="build the indexes in a new directory under DIR and keep them (default: a temporary directory, removed)"
    )
    args = parser.parse_args(argv)
    texts = corpus(args.blocks)
    print(
        f"출처: 로컬 합성 (blocks {len(texts)}, chars {sum(map(len, texts))}) · "
        f"Python {sys.version.split()[0]} · SQLite {sqlite3.sqlite_version} · {platform.system()} {platform.machine()}"
    )
    print("| shape | build s | size MB | purge s | marker tokens before → after |")
    print("|---|---|---|---|---|")
    if args.dir is None:
        with tempfile.TemporaryDirectory() as directory:
            _report(texts, Path(directory))
    else:
        _report(texts, Path(tempfile.mkdtemp(dir=args.dir)))


def _report(texts: list[str], directory: Path) -> None:
    for shape in SHAPES:
        built, size, purged, after, before = measure(shape, texts, directory)
        print(f"| {shape} | {built:.2f} | {size / 1e6:.1f} | {purged:.3f} | {before} → {after} |")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    main()
