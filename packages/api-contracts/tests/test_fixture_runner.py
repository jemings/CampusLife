import json

import pytest

from campuslife_contracts import Contract
from campuslife_contracts.fixtures import FixtureKind, case_problems, discover, layout_problems

from conftest import write_schema

PAGE = {
    "type": "object",
    "required": ["page"],
    "properties": {"page": {"type": "integer", "minimum": 1}},
    "additionalProperties": False,
}


@pytest.fixture
def root(tmp_path):
    write_schema(tmp_path, "common/page", PAGE)
    return tmp_path


def put(root, relative, value):
    path = root / "fixtures" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def problems_of(root):
    contract = Contract.load(root)
    cases = discover(root)
    return {f"{case.kind}/{case.schema_id}/{case.name}": case_problems(contract, case) for case in cases}


def test_discovers_cases_by_kind_and_schema_id(root):
    put(root, "valid/common/page/one.json", {"page": 1})
    put(root, "invalid/common/page/zero.json", {"page": 0})
    put(root, "invalid/common/page/zero.expect.json", [{"path": "/page", "keyword": "minimum"}])
    put(root, "forward/common/page/fwd.json", {"page": 1})
    found = {(case.kind, case.schema_id, case.name) for case in discover(root)}
    assert found == {
        (FixtureKind.VALID, "common/page", "one"),
        (FixtureKind.INVALID, "common/page", "zero"),
        (FixtureKind.FORWARD, "common/page", "fwd"),
    }


def test_valid_case_passes_when_it_validates_and_round_trips(root):
    put(root, "valid/common/page/one.json", '{"page": 1}')
    assert problems_of(root) == {"valid/common/page/one": []}


def test_valid_case_with_a_violation_fails(root):
    put(root, "valid/common/page/zero.json", {"page": 0})
    (problems,) = problems_of(root).values()
    assert problems == ["violation /page minimum"]


def test_invalid_case_passes_when_violations_equal_the_expectation(root):
    put(root, "invalid/common/page/zero.json", {"page": 0, "x": "강"})
    put(
        root,
        "invalid/common/page/zero.expect.json",
        [{"path": "/x", "keyword": "additionalProperties"}, {"path": "/page", "keyword": "minimum"}],
    )
    assert problems_of(root) == {"invalid/common/page/zero": []}


def test_invalid_case_fails_on_a_missing_or_an_extra_violation(root):
    put(root, "invalid/common/page/zero.json", {"page": 0, "x": 1})
    put(root, "invalid/common/page/zero.expect.json", [{"path": "/page", "keyword": "minimum"}, {"path": "/page", "rule": "nfc"}])
    (problems,) = problems_of(root).values()
    assert problems == ["expected but not reported: /page rule nfc", "reported but not expected: /x additionalProperties"]


def test_invalid_case_that_validates_cleanly_fails(root):
    put(root, "invalid/common/page/one.json", {"page": 1})
    put(root, "invalid/common/page/one.expect.json", [])
    (problems,) = problems_of(root).values()
    assert problems == ["an invalid fixture must expect at least one violation"]


@pytest.mark.parametrize(
    "expectation",
    [
        [{"path": "/page"}],
        [{"path": "/page", "keyword": "minimum", "rule": "nfc"}],
        [{"path": "/page", "rule": "no_such_rule"}],
        [{"path": "page", "keyword": "minimum"}],
        [{"path": "/page", "keyword": "minimum"}, {"path": "/page", "keyword": "minimum"}],
        {"path": "/page", "keyword": "minimum"},
    ],
    ids=["neither", "both", "unknown-rule", "not-a-pointer", "duplicate", "not-a-list"],
)
def test_malformed_expectation_fails(root, expectation):
    put(root, "invalid/common/page/zero.json", {"page": 0})
    put(root, "invalid/common/page/zero.expect.json", expectation)
    (problems,) = problems_of(root).values()
    assert len(problems) == 1 and problems[0].startswith("malformed expect file")


def test_forward_case_must_validate_cleanly(root):
    put(root, "forward/common/page/extra.json", {"page": 1, "later": True})
    (problems,) = problems_of(root).values()
    assert problems == ["violation /later additionalProperties"]


def test_fixture_that_is_not_strict_json_fails(root):
    put(root, "valid/common/page/dup.json", '{"page": 1, "page": 1}')
    (problems,) = problems_of(root).values()
    assert problems[0].startswith("not strict JSON")


def test_layout_is_clean_for_a_well_formed_tree(root):
    put(root, "valid/common/page/one.json", {"page": 1})
    put(root, "invalid/common/page/zero.json", {"page": 0})
    put(root, "invalid/common/page/zero.expect.json", [{"path": "/page", "keyword": "minimum"}])
    assert layout_problems(root, Contract.load(root)) == []


def test_layout_problems_are_reported(root):
    put(root, "valid/common/nope/one.json", {"page": 1})
    put(root, "valid/common/page/one.expect.json", [])
    put(root, "invalid/common/page/zero.json", {"page": 0})
    put(root, "invalid/common/page/lonely.expect.json", [])
    put(root, "invalid/common/page/notes.txt", "x")
    put(root, "stale/common/page/one.json", {"page": 1})
    assert layout_problems(root, Contract.load(root)) == [
        "fixtures/invalid/common/page/lonely.expect.json: no case lonely.json next to it",
        "fixtures/invalid/common/page/notes.txt: not a .json file",
        "fixtures/invalid/common/page/zero.json: no zero.expect.json next to it",
        "fixtures/stale: not one of forward, invalid, policy, valid",
        "fixtures/valid/common/nope/one.json: no schema common/nope",
        "fixtures/valid/common/page/one.expect.json: only invalid cases carry expectations",
    ]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('{"ratio": 0.10000000000000000001}', "round trip changed /ratio: expected 0.10000000000000000001, got 0.1"),
        ('{"ratio": 1e400}', "not strict JSON: 1e400 is beyond the range of a double"),
    ],
    ids=["beyond-double-precision", "beyond-double-range"],
)
def test_valid_case_whose_python_round_trip_changes_it_fails(tmp_path, text, problem):
    write_schema(tmp_path, "common/ratio", {"type": "object", "properties": {"ratio": {"type": "number"}}})
    put(tmp_path, "valid/common/ratio/case.json", text)
    (problems,) = problems_of(tmp_path).values()
    assert len(problems) == 1 and problems[0].startswith(problem)
