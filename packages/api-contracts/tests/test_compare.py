import unicodedata
from pathlib import Path

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


def test_cli_reports_too_deep_output_as_a_mismatch(dirs, capsys):
    expected, actual = dirs
    write(expected, "common/x/case.json", "[]")
    write(actual, "common/x/case.json", "[" * 100_000 + "]" * 100_000)
    assert main(["compare", str(expected), str(actual)]) == 1
    assert capsys.readouterr().out.startswith("common/x/case.json: not strict JSON")


def test_cli_exits_two_when_a_golden_file_is_not_strict_json(dirs, capsys):
    expected, actual = dirs
    write(expected, "common/x/case.json", "[" * 100_000 + "]" * 100_000)
    write(actual, "common/x/case.json", "[]")
    assert main(["compare", str(expected), str(actual)]) == 2
    assert "common/x/case.json" in capsys.readouterr().err


def test_cli_rejects_a_missing_expected_directory(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["compare", str(tmp_path / "nope"), str(tmp_path)])
    assert exit_info.value.code == 2


SPELLING_SCHEMA = {
    "common/x": {
        "type": "object",
        "properties": {
            "seq": {"type": "integer"},
            "credits": {"type": "number"},
            "list": {"type": "array", "items": {"type": "integer"}},
        },
    }
}


@pytest.mark.parametrize("spelling", ["0.0", "1E0", "1.0e0", "-0.0", "1e2", "10E-1"])
def test_integer_place_written_with_fraction_or_exponent_is_a_mismatch(dirs, make_contract, spelling):
    expected, actual = dirs
    write(expected, "common/x/case.json", '{"seq": 1, "list": [1]}')
    write(actual, "common/x/case.json", '{"seq": 1, "list": [1]}'.replace('"seq": 1', f'"seq": {spelling}'))
    found = [str(m) for m in compare_dirs(expected, actual, make_contract(SPELLING_SCHEMA))]
    assert any(line.startswith("common/x/case.json#/seq:") for line in found)


def test_integer_spelling_is_judged_inside_arrays(dirs, make_contract):
    expected, actual = dirs
    write(expected, "common/x/case.json", '{"list": [1, 2]}')
    write(actual, "common/x/case.json", '{"list": [1, 2.0]}')
    found = [str(m) for m in compare_dirs(expected, actual, make_contract(SPELLING_SCHEMA))]
    assert found and found[0].startswith("common/x/case.json#/list/1:")


def test_number_place_keeps_every_spelling_of_its_value(dirs, make_contract):
    expected, actual = dirs
    write(expected, "common/x/case.json", '{"seq": 1, "credits": 2.5}')
    write(actual, "common/x/case.json", '{"seq": 1, "credits": 2.50e0}')
    assert compare_dirs(expected, actual, make_contract(SPELLING_SCHEMA)) == []


def test_integer_spelled_output_still_passes(dirs, make_contract):
    expected, actual = dirs
    write(expected, "common/x/case.json", '{"seq": 1, "credits": 2}')
    write(actual, "common/x/case.json", '{"credits": 2.0, "seq": 1}')
    assert compare_dirs(expected, actual, make_contract(SPELLING_SCHEMA)) == []


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("capture/attachment/audio_segment_row.json", '"seq": 0', '"seq": 0.0'),
        ("item/item/course.json", '"rev": 17', '"rev": 17E0'),
        ("item/item/course.json", '"rev": 17', '"rev": 1.7e1'),
    ],
    ids=["seq-0.0", "rev-17E0", "rev-1.7e1"],
)
def test_cli_fails_a_golden_copy_with_an_integer_written_as_a_fraction(tmp_path, path, old, new):
    golden = Path(__file__).parent.parent / "fixtures" / "valid"
    text = (golden / path).read_text(encoding="utf-8")
    assert old in text
    out = tmp_path / "out"
    for source in golden.rglob("*.json"):
        target = out / source.relative_to(golden)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    (out / path).write_text(text.replace(old, new, 1), encoding="utf-8")
    assert main(["compare", str(golden), str(out)]) == 1


def test_cli_passes_the_golden_fixtures_against_themselves():
    golden = Path(__file__).parent.parent / "fixtures" / "valid"
    assert main(["compare", str(golden), str(golden)]) == 0


CONTRACT_ROOT = Path(__file__).parent.parent
M, I = "3f2504e0-4f89-41d3-9a0c-0305e82c3301", "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _patch(to, frm="2025"):
    return (
        f'{{"mutation_id":"{M}","op":"patch","entity":"item","id":"{I}",'
        f'"set":{{"fields.year":{{"from":{frm},"to":{to}}}}}}}'
    )


TYPED_OUTSIDE_THE_SCHEMA = [
    (_patch("2026"), _patch("2026.0")),
    (_patch("2026"), _patch("2026", frm="2025E0")),
    (
        f'{{"mutation_id":"{M}","op":"patch","entity":"item","id":"{I}","set":{{"fields.schedule_slots":'
        '{"from":null,"to":[{"weekdays":[1,3],"start":"09:00","end":"10:15","tz":"Asia/Seoul",'
        '"from_date":"2026-03-02","until_date":"2026-06-19"}]}}}',
        f'{{"mutation_id":"{M}","op":"patch","entity":"item","id":"{I}","set":{{"fields.schedule_slots":'
        '{"from":null,"to":[{"weekdays":[1.0,3],"start":"09:00","end":"10:15","tz":"Asia/Seoul",'
        '"from_date":"2026-03-02","until_date":"2026-06-19"}]}}}',
    ),
]


@pytest.mark.parametrize(("golden", "output"), TYPED_OUTSIDE_THE_SCHEMA, ids=["to", "from", "weekdays"])
def test_integer_place_the_hub_types_by_rule_is_a_mismatch(dirs, golden, output):
    from campuslife_contracts import Contract

    expected, actual = dirs
    write(expected, "mutation/mutation/case.json", golden)
    write(actual, "mutation/mutation/case.json", output)
    found = [str(m) for m in compare_dirs(expected, actual, Contract.load(CONTRACT_ROOT))]
    assert found and "violates the contract" in found[0]


def test_mismatch_shows_the_spelling_the_output_used(dirs, make_contract):
    expected, actual = dirs
    write(expected, "common/x/case.json", '{"seq": 17}')
    write(actual, "common/x/case.json", '{"seq": 17e0}')
    (found,) = [str(m) for m in compare_dirs(expected, actual, make_contract(SPELLING_SCHEMA))]
    assert found.startswith("common/x/case.json#/seq:") and "written as 17e0" in found


def test_different_values_report_only_the_difference(dirs, make_contract):
    expected, actual = dirs
    write(expected, "common/x/case.json", '{"seq": 1}')
    write(actual, "common/x/case.json", '{"seq": 2.0}')
    (found,) = [str(m) for m in compare_dirs(expected, actual, make_contract(SPELLING_SCHEMA))]
    assert found.startswith("common/x/case.json#/seq: expected 1, got 2.0")


def test_a_file_whose_schema_is_not_in_the_contract_is_a_mismatch(dirs, make_contract):
    expected, actual = dirs
    write(expected, "common/y/case.json", "1")
    write(actual, "common/y/case.json", "1")
    assert [str(m) for m in compare_dirs(expected, actual, make_contract(SPELLING_SCHEMA))] == [
        "common/y/case.json: no schema common/y"
    ]


def _golden_copy_with_wrong_spelling(tmp_path):
    golden = CONTRACT_ROOT / "fixtures" / "valid"
    out = tmp_path / "out"
    for source in golden.rglob("*.json"):
        target = out / source.relative_to(golden)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    path = out / "capture/attachment/audio_segment_row.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"seq": 0', '"seq": 0.0', 1), encoding="utf-8")
    return golden, out


def test_cli_checks_spelling_when_expected_dir_is_a_subdirectory(tmp_path):
    golden, out = _golden_copy_with_wrong_spelling(tmp_path)
    assert main(["compare", str(golden / "capture"), str(out / "capture")]) == 1


def test_cli_checks_spelling_when_expected_dir_is_dot(tmp_path, monkeypatch):
    golden, out = _golden_copy_with_wrong_spelling(tmp_path)
    monkeypatch.chdir(golden)
    assert main(["compare", ".", str(out)]) == 1


def test_cli_checks_spelling_through_a_symlink(tmp_path):
    golden, out = _golden_copy_with_wrong_spelling(tmp_path)
    link = tmp_path / "somewhere"
    link.symlink_to(golden, target_is_directory=True)
    assert main(["compare", str(link), str(out)]) == 1


def test_cli_says_when_the_spelling_check_is_off(dirs, capsys):
    expected, actual = dirs
    write(expected, "common/x/case.json", '{"a": 1}')
    write(actual, "common/x/case.json", '{"a": 1}')
    assert main(["compare", str(expected), str(actual)]) == 0
    assert "integer spelling check off:" in capsys.readouterr().err


def test_cli_says_when_a_fixtures_valid_has_no_schemas_beside_it(tmp_path, capsys):
    valid = tmp_path / "fixtures" / "valid"
    write(valid, "common/x/case.json", '{"a": 1}')
    write(tmp_path / "out", "common/x/case.json", '{"a": 1.0e0}')
    assert main(["compare", str(valid), str(tmp_path / "out")]) == 0
    assert "integer spelling check off: no schemas/" in capsys.readouterr().err


def test_cli_exits_two_when_a_schema_file_is_broken(tmp_path, capsys):
    valid = tmp_path / "fixtures" / "valid"
    write(valid, "common/x/case.json", '{"a": 1}')
    write(tmp_path / "out", "common/x/case.json", '{"a": 1}')
    write(tmp_path, "schemas/common/x.json", "{not json")
    assert main(["compare", str(valid), str(tmp_path / "out")]) == 2
    assert "broken contract" in capsys.readouterr().err
