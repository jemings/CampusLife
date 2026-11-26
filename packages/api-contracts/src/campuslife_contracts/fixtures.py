"""Golden fixture layout (E7, E8) and the check each case must pass.

fixtures/<kind>/<schema_id>/<case>.json, and for invalid cases a
<case>.expect.json next to it listing the exact violations.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path

from .compare import differences
from .contract import Contract
from .jsonio import NotStrictJson, loads_strict, read_strict
from .violations import (
    MalformedExpectation,
    Violation,
    describe,
    expectation_from_json,
    violation_order,
)

EXPECT_SUFFIX = ".expect.json"


class FixtureKind(enum.StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    FORWARD = "forward"


@dataclass(frozen=True)
class FixtureCase:
    kind: FixtureKind
    schema_id: str
    name: str
    path: Path

    @property
    def expect_path(self) -> Path:
        return self.path.with_name(self.name + EXPECT_SUFFIX)


def _is_expectation(path: Path) -> bool:
    return path.name.endswith(EXPECT_SUFFIX)


def discover(root: Path) -> list[FixtureCase]:
    cases = []
    for kind in FixtureKind:
        base = Path(root) / "fixtures" / kind
        for path in sorted(base.rglob("*.json")):
            if not _is_expectation(path):
                schema_id = path.parent.relative_to(base).as_posix()
                cases.append(FixtureCase(kind, schema_id, path.stem, path))
    return cases


def layout_problems(root: Path, contract: Contract) -> list[str]:
    fixtures_dir = Path(root) / "fixtures"
    problems: dict[Path, str] = {}
    for entry in fixtures_dir.iterdir():
        if entry.name not in set(FixtureKind):
            problems[entry] = f"not one of {', '.join(sorted(FixtureKind))}"
    for kind in FixtureKind:
        base = fixtures_dir / kind
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix != ".json":
                problems[path] = "not a .json file"
            elif _is_expectation(path):
                case_name = path.name.removesuffix(EXPECT_SUFFIX) + ".json"
                if kind is not FixtureKind.INVALID:
                    problems[path] = "only invalid cases carry expectations"
                elif not path.with_name(case_name).is_file():
                    problems[path] = f"no case {case_name} next to it"
            elif (schema_id := path.parent.relative_to(base).as_posix()) not in contract.schema_ids:
                problems[path] = f"no schema {schema_id}"
            elif kind is FixtureKind.INVALID and not path.with_name(path.stem + EXPECT_SUFFIX).is_file():
                problems[path] = f"no {path.stem}{EXPECT_SUFFIX} next to it"
    return [
        f"{path.relative_to(root).as_posix()}: {problem}"
        for path, problem in sorted(problems.items(), key=lambda item: item[0].relative_to(root).as_posix())
    ]


def _violation_lines(prefix: str, violations: set[Violation] | list[Violation]) -> list[str]:
    return [f"{prefix}{describe(violation)}" for violation in sorted(violations, key=violation_order)]


def _round_trip_problems(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    expected = loads_strict(text, exact_numbers=True)
    actual = loads_strict(json.dumps(json.loads(text), ensure_ascii=False), exact_numbers=True)
    return [f"round trip changed {at}: {reason}" for at, reason in differences(expected, actual)]


def _expectation_problems(case: FixtureCase, reported: list[Violation]) -> list[str]:
    try:
        expected = expectation_from_json(read_strict(case.expect_path))
    except (NotStrictJson, MalformedExpectation) as error:
        return [f"malformed expect file: {error}"]
    if not expected:
        return ["an invalid fixture must expect at least one violation"]
    return _violation_lines("expected but not reported: ", expected - set(reported)) + _violation_lines(
        "reported but not expected: ", set(reported) - expected
    )


def case_problems(contract: Contract, case: FixtureCase) -> list[str]:
    """Empty when the case behaves as its kind requires."""
    try:
        instance = read_strict(case.path)
    except NotStrictJson as error:
        return [f"not strict JSON: {error}"]
    reported = contract.validate(case.schema_id, instance)
    match case.kind:
        case FixtureKind.VALID:
            return _violation_lines("violation ", reported) + _round_trip_problems(case.path)
        case FixtureKind.FORWARD:
            return _violation_lines("violation ", reported)
        case FixtureKind.INVALID:
            return _expectation_problems(case, reported)
