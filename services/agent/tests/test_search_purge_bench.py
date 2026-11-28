"""The purge measurement script still runs, and in both table shapes its purge leaves no marker behind."""

import importlib.util
import sys
import tempfile
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "tools" / "search_purge_bench.py"


def load_bench():
    spec = importlib.util.spec_from_file_location("search_purge_bench", BENCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_markers_use_only_syllables_the_corpus_does_not():
    bench = load_bench()
    texts = bench.corpus(500)
    kept = "".join(texts)

    with_markers, markers = bench.marked(texts, 50)

    assert len(markers) == 50
    assert not set("".join(markers.values())) & set(kept)
    assert all(with_markers[i].endswith(marker) for i, marker in markers.items())


def test_each_shape_holds_the_markers_before_its_purge_and_none_after_it(tmp_path):
    bench = load_bench()
    texts, markers = bench.marked(bench.corpus(3000), 100)
    total = len(bench.bigrams(markers))

    for shape in bench.SHAPES:
        result = bench.measure(shape, texts, markers, tmp_path)

        assert (result.before, result.after, result.terms_after) == (total, 0, 0), shape


def test_without_optimize_the_contentless_shape_keeps_markers(tmp_path):
    bench = load_bench()
    texts, markers = bench.marked(bench.corpus(3000), 100)

    result = bench.measure("contentless", texts, markers, tmp_path, optimize=False)

    assert result.after > 0


def test_the_bench_reports_its_source_and_removes_its_indexes_unless_given_a_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    load_bench().main(["--blocks", "200", "--marked", "10"])
    assert capsys.readouterr().out.startswith("출처: 로컬 합성")
    assert list(tmp_path.iterdir()) == []
