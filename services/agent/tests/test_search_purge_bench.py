"""The purge measurement script still runs, and in both table shapes it measures leaves no marker token behind."""

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


def test_each_shape_holds_the_marker_before_the_purge_and_none_of_it_after(tmp_path):
    bench = load_bench()
    texts = bench.corpus(200)

    for shape in bench.SHAPES:
        _, _, _, after, before = bench.measure(shape, texts, tmp_path)

        assert (before > 0, after) == (True, 0), shape


def test_the_bench_reports_its_source_and_removes_its_indexes_unless_given_a_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    load_bench().main(["--blocks", "50"])
    assert capsys.readouterr().out.startswith("출처: 로컬 합성")
    assert list(tmp_path.iterdir()) == []
