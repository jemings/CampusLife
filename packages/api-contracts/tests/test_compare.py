import unicodedata

import pytest

from campuslife_contracts.cli import main
from campuslife_contracts.compare import compare_dirs


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def dirs(tmp_path):
    expected, actual = tmp_path / "expected", tmp_path / "actual"
    expected.mkdir()
    actual.mkdir()
    return expected, actual


def mismatches(dirs, expected_text, actual_text):
    expected, actual = dirs
    write(expected, "common/x/case.json", expected_text)
    write(actual, "common/x/case.json", actual_text)
    return [str(m) for m in compare_dirs(expected, actual)]


EQUIVALENT = [
    ('{"box": [0, 0.1, 1, 0.3]}', '{"box":[0.0,0.1,1.0,0.3]}'),
    ('{"a": 1, "b": 2}', '{"b": 2, "a": 1}'),
    ('{"n": 100}', '{"n": 1e2}'),
    ('{"n": 0}', '{"n": -0.0}'),
    ('{"n": 1.5}', '{"n": 1.50}'),
    ('{"mono_ns": 9007199254740993}', '{"mono_ns":9007199254740993}'),
    ('{"t": "\\uac15"}', '{"t": "강"}'),
]
EQUIVALENT_IDS = ["int-vs-float", "key-order", "exponent", "negative-zero", "trailing-zero",
                  "beyond-2^53", "escape-vs-literal"]

DIFFERENT = [
    ('{"box": [0.1]}', '{"box": [0.10000000149011612]}', "/box/0"),
    ('{"mono_ns": 9007199254740993}', '{"mono_ns": 9007199254740992}', "/mono_ns"),
    ('{"a": 1}', '{"a": 1, "body": null}', "/body"),
    ('{"a": 1, "body": null}', '{"a": 1}', "/body"),
    ('{"flag": true}', '{"flag": 1}', "/flag"),
    ('{"n": 0}', '{"n": false}', "/n"),
    ('{"list": [1, 2]}', '{"list": [1]}', "/list"),
    ('{"list": [1, 2]}', '{"list": [2, 1]}', "/list/0"),
    ('{"s": "1"}', '{"s": 1}', "/s"),
    ('{"t": "강"}', '{"t": "' + unicodedata.normalize("NFD", "강") + '"}', "/t"),
    ('{"a/b": {"c~d": 1}}', '{"a/b": {"c~d": 2}}', "/a~1b/c~0d"),
    ('{"n": 12345678901234567890123456789012345}', '{"n": 12345678901234567890123456789012346}', "/n"),
    ('{"n": 0.12345678901234567890123456789012345}', '{"n": 0.12345678901234567890123456789012346}', "/n"),
    ('{"n": 1.0000000000000000000000000000001e40}', '{"n": 1e40}', "/n"),
]
DIFFERENT_IDS = ["float32-drift", "int64-exact", "null-added", "null-dropped", "bool-vs-int",
                 "int-vs-bool", "length", "order", "string-vs-number", "nfc-vs-nfd", "pointer-escaping",
                 "35-digit-integer", "35-digit-fraction", "long-mantissa-exponent"]


@pytest.mark.parametrize(("expected_text", "actual_text"), EQUIVALENT, ids=EQUIVALENT_IDS)
def test_same_json_value_is_not_a_mismatch(dirs, expected_text, actual_text):
    assert mismatches(dirs, expected_text, actual_text) == []


@pytest.mark.parametrize(("expected_text", "actual_text", "where"), DIFFERENT, ids=DIFFERENT_IDS)
def test_changed_json_value_is_reported_at_its_pointer(dirs, expected_text, actual_text, where):
    found = mismatches(dirs, expected_text, actual_text)
    assert found and found[0].startswith(f"common/x/case.json#{where}:")


def test_missing_output_is_a_mismatch(dirs):
    expected, actual = dirs
    write(expected, "common/x/case.json", "1")
    assert [str(m) for m in compare_dirs(expected, actual)] == ["common/x/case.json: missing output"]


def test_output_without_an_expected_file_is_ignored(dirs):
    expected, actual = dirs
    write(expected, "common/x/case.json", "1")
    write(actual, "common/x/case.json", "1")
    write(actual, "common/x/extra.json", "2")
    assert compare_dirs(expected, actual) == []


@pytest.mark.parametrize(
    "actual_text",
    ['{"a": 1, "a": 1}', '{"a": NaN}', '{"a": 1', '\ufeff{"a": 1}', '{"a": "\\ud800"}', '{"\\udfff": 1}'],
    ids=["duplicate-member", "nan", "truncated", "bom", "lone-surrogate-value", "lone-surrogate-name"],
)
def test_output_that_is_not_strict_json_is_a_mismatch(dirs, actual_text):
    (found,) = mismatches(dirs, '{"a": 1}', actual_text)
    assert found.startswith("common/x/case.json: not strict JSON")


def test_cli_exits_zero_when_every_output_matches(dirs, capsys):
    expected, actual = dirs
    write(expected, "common/x/case.json", '{"a": 1}')
    write(actual, "common/x/case.json", '{"a": 1.0}')
    assert main(["compare", str(expected), str(actual)]) == 0


def test_cli_lists_mismatches_and_exits_one(dirs, capsys):
    expected, actual = dirs
    write(expected, "common/x/a.json", '{"a": 1}')
    write(expected, "common/x/b.json", '{"a": 1}')
    write(actual, "common/x/a.json", '{"a": 2}')
    assert main(["compare", str(expected), str(actual)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("common/x/a.json#/a:")
    assert out[1] == "common/x/b.json: missing output"


def test_cli_rejects_an_expected_directory_without_json(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit) as exit_info:
        main(["compare", str(tmp_path / "empty"), str(tmp_path)])
    assert exit_info.value.code == 2
    assert "no *.json" in capsys.readouterr().err


def test_cli_rejects_a_missing_expected_directory(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["compare", str(tmp_path / "nope"), str(tmp_path)])
    assert exit_info.value.code == 2
