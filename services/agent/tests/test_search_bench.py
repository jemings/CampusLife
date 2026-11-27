"""The analyzer measurement script still runs, and on its synthetic corpus the bigram index misses nothing."""

import importlib.util
import math
import sys
from pathlib import Path

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
