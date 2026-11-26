"""Reference cloud_policy resolution (D8, A6) and its golden vectors.

fixtures/policy/resolve/<case>.json:
    {"explicit": p|null, "course_default": p|null, "global_default": p, "expected": p}
fixtures/policy/min/<case>.json:
    {"policies": [p, ...], "expected": p}
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .jsonio import NotStrictJson, read_strict

CLOUD_POLICIES = ("blocked", "local_only", "cloud_allowed")
"""Most restrictive first; the order is the policy's meaning."""

POLICY_DIR = "policy"


def _known(policy: object) -> str:
    if policy not in CLOUD_POLICIES:
        raise ValueError(f"not a cloud_policy: {policy!r}")
    return policy


def resolve_cloud_policy(explicit: str | None, course_default: str | None, global_default: str) -> str:
    """The explicit policy, else the course's default_cloud_policy, else the global default.

    Every policy given must be known, even one that does not decide the result.
    """
    chain = [_known(policy) for policy in (explicit, course_default) if policy is not None]
    return [*chain, _known(global_default)][0]


def effective_cloud_policy(policies: Iterable[str]) -> str:
    """The most restrictive policy over every input of a provider call (D8)."""
    ranks = [CLOUD_POLICIES.index(_known(policy)) for policy in policies]
    if not ranks:
        raise ValueError("a provider call has at least one input")
    return CLOUD_POLICIES[min(ranks)]


def _resolve_vector(vector: dict) -> tuple[set[str], object]:
    return {"explicit", "course_default", "global_default", "expected"}, lambda: resolve_cloud_policy(
        vector["explicit"], vector["course_default"], vector["global_default"]
    )


def _min_vector(vector: dict) -> tuple[set[str], object]:
    def run():
        policies = vector["policies"]
        if not isinstance(policies, list) or not policies:
            raise ValueError("policies must list at least one policy")
        return effective_cloud_policy(policies)

    return {"policies", "expected"}, run


OPERATIONS = {"resolve": _resolve_vector, "min": _min_vector}


@dataclass(frozen=True)
class PolicyVector:
    operation: str
    name: str
    path: Path


def discover_policy_vectors(root: Path) -> list[PolicyVector]:
    base = Path(root) / "fixtures" / POLICY_DIR
    return [
        PolicyVector(operation, path.stem, path)
        for operation in sorted(OPERATIONS)
        for path in sorted((base / operation).glob("*.json"))
    ]


def policy_vector_problems(vector: PolicyVector) -> list[str]:
    """Empty when the reference functions give the vector's expected policy."""
    try:
        data = read_strict(vector.path)
    except NotStrictJson as error:
        return [f"not strict JSON: {error}"]
    if not isinstance(data, dict):
        return ["a vector is a JSON object"]
    members, run = OPERATIONS[vector.operation](data)
    if set(data) != members:
        return [f"members must be exactly {', '.join(sorted(members))}"]
    try:
        expected = _known(data["expected"])
        actual = run()
    except ValueError as error:
        return [str(error)]
    if actual != expected:
        return [f"{vector.operation} gives {actual}, the vector expects {expected}"]
    return []


def policy_layout_problems(root: Path) -> dict[Path, str]:
    base = Path(root) / "fixtures" / POLICY_DIR
    problems: dict[Path, str] = {}
    for entry in base.iterdir() if base.is_dir() else ():
        if entry.name not in OPERATIONS or not entry.is_dir():
            problems[entry] = f"not one of {', '.join(sorted(OPERATIONS))}"
            continue
        for path in entry.iterdir():
            if path.is_dir():
                problems[path] = f"vectors sit directly under {entry.name}"
            elif path.suffix != ".json":
                problems[path] = "not a .json file"
    return problems
