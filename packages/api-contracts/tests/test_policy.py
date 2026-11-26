import json

import pytest

from campuslife_contracts import Contract, effective_cloud_policy, resolve_cloud_policy
from campuslife_contracts.fixtures import layout_problems
from campuslife_contracts.policy import discover_policy_vectors, policy_vector_problems

from conftest import write_schema


@pytest.mark.parametrize(
    ("explicit", "course_default", "global_default", "expected"),
    [
        ("blocked", "cloud_allowed", "local_only", "blocked"),
        ("cloud_allowed", "blocked", "local_only", "cloud_allowed"),
        (None, "cloud_allowed", "local_only", "cloud_allowed"),
        (None, None, "local_only", "local_only"),
        (None, None, "blocked", "blocked"),
    ],
)
def test_resolve_takes_the_first_policy_that_is_set(explicit, course_default, global_default, expected):
    assert resolve_cloud_policy(explicit, course_default, global_default) == expected


@pytest.mark.parametrize(
    ("policies", "expected"),
    [
        (["cloud_allowed"], "cloud_allowed"),
        (["cloud_allowed", "local_only"], "local_only"),
        (["local_only", "blocked", "cloud_allowed"], "blocked"),
    ],
)
def test_effective_policy_is_the_most_restrictive_input(policies, expected):
    assert effective_cloud_policy(policies) == expected


def test_effective_policy_needs_at_least_one_input():
    with pytest.raises(ValueError):
        effective_cloud_policy([])


@pytest.mark.parametrize(
    "call",
    [
        lambda: resolve_cloud_policy("cloud", None, "local_only"),
        lambda: resolve_cloud_policy(None, None, None),
        lambda: effective_cloud_policy(["local_only", "Blocked"]),
        lambda: resolve_cloud_policy("blocked", "cloud", "local_only"),
        lambda: resolve_cloud_policy("blocked", None, "nope"),
        lambda: resolve_cloud_policy(None, "blocked", "nope"),
    ],
    ids=["typo", "no-global-default", "case", "unused-course-default", "unused-global-default",
         "global-default-behind-course"],
)
def test_unknown_policy_values_are_refused(call):
    with pytest.raises(ValueError):
        call()


def put(root, relative, value):
    path = root / "fixtures" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return path


GOOD_RESOLVE = {"explicit": None, "course_default": None, "global_default": "local_only", "expected": "local_only"}


def test_vectors_are_discovered_under_resolve_and_min(tmp_path):
    put(tmp_path, "policy/resolve/default.json", GOOD_RESOLVE)
    put(tmp_path, "policy/min/one.json", {"policies": ["blocked"], "expected": "blocked"})
    found = {(vector.operation, vector.name) for vector in discover_policy_vectors(tmp_path)}
    assert found == {("resolve", "default"), ("min", "one")}


@pytest.mark.parametrize(
    ("relative", "value", "problem"),
    [
        ("policy/resolve/a.json", GOOD_RESOLVE, None),
        ("policy/resolve/a.json", {**GOOD_RESOLVE, "expected": "cloud_allowed"},
         "resolve gives local_only, the vector expects cloud_allowed"),
        ("policy/resolve/a.json", {k: v for k, v in GOOD_RESOLVE.items() if k != "explicit"},
         "members must be exactly course_default, expected, explicit, global_default"),
        ("policy/resolve/a.json", {**GOOD_RESOLVE, "explicit": "cloud"}, "not a cloud_policy: 'cloud'"),
        ("policy/min/a.json", {"policies": [], "expected": "blocked"}, "policies must list at least one policy"),
        ("policy/min/a.json", {"policies": "blocked", "expected": "blocked"}, "policies must list at least one policy"),
        ("policy/min/a.json", '{"policies": ["blocked"], "policies": ["blocked"], "expected": "blocked"}',
         "not strict JSON"),
    ],
    ids=["passes", "wrong-expectation", "missing-member", "unknown-value", "empty-min", "min-not-a-list", "duplicate"],
)
def test_vector_problems(tmp_path, relative, value, problem):
    put(tmp_path, relative, value)
    (vector,) = discover_policy_vectors(tmp_path)
    problems = policy_vector_problems(vector)
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problems[0].startswith(problem)


def test_layout_accepts_the_policy_tree_and_nothing_else_inside_it(tmp_path):
    write_schema(tmp_path, "common/x", {})
    put(tmp_path, "policy/resolve/default.json", GOOD_RESOLVE)
    put(tmp_path, "policy/min/one.json", {"policies": ["blocked"], "expected": "blocked"})
    assert layout_problems(tmp_path, Contract.load(tmp_path)) == []
    put(tmp_path, "policy/max/one.json", {})
    put(tmp_path, "policy/min/deeper/one.json", {})
    put(tmp_path, "policy/min/notes.txt", "x")
    assert layout_problems(tmp_path, Contract.load(tmp_path)) == [
        "fixtures/policy/max: not one of min, resolve",
        "fixtures/policy/min/deeper: vectors sit directly under min",
        "fixtures/policy/min/notes.txt: not a .json file",
    ]
