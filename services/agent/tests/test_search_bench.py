"""The analyzer measurement script still runs, and on its synthetic corpus the bigram index misses nothing."""

import importlib.util
import math
import sys
import tempfile
from pathlib import Path

from campuslife_hub.search.analyzer import SearchPlan

BENCH = Path(__file__).resolve().parents[1] / "tools" / "search_bench.py"


def load_bench():
    spec = importlib.util.spec_from_file_location("search_bench", BENCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def test_the_bigram_index_finds_every_block_that_holds_the_query_and_nothing_else(tmp_path, capsys):
    results = load_bench().main(["--blocks", "300", "--dir", str(tmp_path)])

    assert capsys.readouterr().out.startswith("출처: 로컬 합성")
    for name in ("bigram", "bigram-contentless"):
        for category, (at_10, recall, false_positives) in results[name].per_category.items():
            assert (at_10, recall, false_positives) == (1.0, 1.0, 0) or math.isnan(recall), (name, category)
    assert results["unicode61"].per_category["particle"][1] < 1.0


def test_the_bench_counts_false_positives_and_keeps_scans_apart_from_matches(tmp_path):
    bench = load_bench()
    texts = bench.corpus(50)
    folded = [text.casefold() for text in texts]
    queries = sum(len(qs) for qs in bench.QUERIES.values())
    returns_everything = bench.Variant(
        "everything",
        "CREATE VIRTUAL TABLE fts USING fts5(text);",
        lambda i, t: (i, t),
        "INSERT INTO fts(rowid, text) VALUES (?, ?)",
        lambda query: SearchPlan(None, ("%",)),
        True,
    )

    scanned = bench.measure(returns_everything, texts, folded, tmp_path)
    matched = bench.measure(bench.VARIANTS[0], texts, folded, tmp_path)

    assert all(false_positives > 0 for _, _, false_positives in scanned.per_category.values())
    assert (len(scanned.match_ms), len(scanned.scan_ms)) == (0, bench.REPEATS * queries)
    assert (len(matched.match_ms), len(matched.scan_ms)) == (bench.REPEATS * queries, 0)


def test_the_bench_removes_its_indexes_unless_given_a_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    load_bench().main(["--blocks", "50"])
    assert list(tmp_path.iterdir()) == []
