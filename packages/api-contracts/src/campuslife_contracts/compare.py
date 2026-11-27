"""Round-trip comparator (E9): two JSON values are equal when their
`canonical_json` bytes are equal; the walk below only locates the differences.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .contract import Contract
from .jsonio import FractionalSpelling, NotStrictJson, canonical_json, pointer, read_strict
from .violations import describe


class BrokenGolden(Exception):
    """An expected (golden) file is not strict JSON; nothing can be judged against it."""


@dataclass(frozen=True)
class Mismatch:
    file: str
    reason: str
    at: str | None = None

    def __str__(self) -> str:
        where = self.file if self.at is None else f"{self.file}#{self.at}"
        return f"{where}: {self.reason}"


def _kind(value: object) -> str:
    match value:
        case None:
            return "null"
        case bool():
            return "boolean"
        case Decimal():
            return "number"
        case str():
            return "string"
        case list():
            return "array"
        case dict():
            return "object"
    raise TypeError(f"not a JSON value: {value!r}")


def _show(value: object) -> str:
    match value:
        case Decimal():
            return str(value)
        case list():
            return f"array of {len(value)}"
        case dict():
            return f"object of {len(value)}"
    return json.dumps(value)


def _differences(expected: object, actual: object, path: tuple[object, ...]) -> Iterator[tuple[str, str]]:
    where = pointer(path)
    if _kind(expected) != _kind(actual):
        yield where, f"expected {_kind(expected)} {_show(expected)}, got {_kind(actual)} {_show(actual)}"
    elif isinstance(expected, dict):
        for name in sorted(expected.keys() | actual.keys()):
            if name not in actual:
                yield pointer((*path, name)), "member missing from output"
            elif name not in expected:
                yield pointer((*path, name)), f"unexpected member {_show(actual[name])}"
            else:
                yield from _differences(expected[name], actual[name], (*path, name))
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            yield where, f"expected {_show(expected)}, got {_show(actual)}"
        else:
            for index, (left, right) in enumerate(zip(expected, actual)):
                yield from _differences(left, right, (*path, index))
    elif canonical_json(expected) != canonical_json(actual):
        yield where, f"expected {_show(expected)}, got {_show(actual)}"


def differences(expected: object, actual: object) -> list[tuple[str, str]]:
    """(JSON Pointer, reason) for each place two exact-number JSON values differ."""
    if canonical_json(expected) == canonical_json(actual):
        return []
    return list(_differences(expected, actual, ()))


def _as_json_loads_types(value: object) -> object:
    """What `json.loads` would have produced: a fraction or exponent spelling is a float, any other number an int."""
    match value:
        case FractionalSpelling():
            return float(value.text)
        case Decimal():
            return int(value)
        case list():
            return [_as_json_loads_types(item) for item in value]
        case dict():
            return {name: _as_json_loads_types(item) for name, item in value.items()}
    return value


def _value_at(value: object, at: str) -> object:
    for token in at.split("/")[1:]:
        name = token.replace("~1", "/").replace("~0", "~")
        value = value[int(name)] if isinstance(value, list) else value[name]
    return value


def _contract_mismatches(contract: Contract, schema_id: str, actual: object) -> list[tuple[str, str]]:
    """What the Hub's judge says about the output once numbers have the types `json.loads` gives them."""
    try:
        violations = contract.validate(schema_id, _as_json_loads_types(actual))
    except TypeError as error:
        return [("", str(error))]
    found = []
    for violation in violations:
        reason = f"violates the contract: {describe(violation)}"
        if isinstance(spelled := _value_at(actual, violation.path), FractionalSpelling):
            reason += f" (written as {spelled.text})"
        found.append((violation.path, reason))
    return found


def compare_file(
    expected_path: Path,
    actual_path: Path,
    name: str,
    contract: Contract | None = None,
    schema_id: str | None = None,
) -> list[Mismatch]:
    if not actual_path.is_file():
        return [Mismatch(name, "missing output")]
    try:
        expected = read_strict(expected_path, exact_numbers=True)
    except NotStrictJson as error:
        raise BrokenGolden(f"{name}: {error}") from error
    try:
        actual = read_strict(actual_path, exact_numbers=True)
    except NotStrictJson as error:
        return [Mismatch(name, f"not strict JSON: {error}")]
    found = differences(expected, actual)
    if not found and contract is not None:
        if schema_id not in contract.schema_ids:
            return [Mismatch(name, f"no schema {schema_id}")]
        found = _contract_mismatches(contract, schema_id, actual)
    return [Mismatch(name, reason, at) for at, reason in found]


def compare_dirs(
    expected_dir: Path, actual_dir: Path, contract: Contract | None = None, schema_base: str = ""
) -> list[Mismatch]:
    """Compare every `*.json` under expected_dir with the same path under actual_dir.

    With a contract, a file `<schema_id>/<case>.json` whose values all match is also judged by
    the Hub's validator, so an integer place written as `0.0` or `1E0` is a mismatch.
    `schema_base` is expected_dir's own path below `fixtures/valid`, if it is a subdirectory.
    """
    mismatches: list[Mismatch] = []
    for expected_path in sorted(Path(expected_dir).rglob("*.json")):
        relative = expected_path.relative_to(expected_dir)
        schema_id = (Path(schema_base) / relative.parent).as_posix()
        mismatches += compare_file(
            expected_path, Path(actual_dir) / relative, relative.as_posix(), contract, schema_id
        )
    return mismatches
