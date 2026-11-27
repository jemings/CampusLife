"""Round-trip comparator (E9): two JSON values are equal when their
`canonical_json` bytes are equal; the walk below only locates the differences.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError, validators

from .contract import Contract, schema_uri
from .ecma import ecma_search
from .jsonio import FractionalSpelling, NotStrictJson, canonical_json, pointer, read_strict


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


def _ecma_pattern(validator, pattern, instance, schema):
    if validator.is_type(instance, "string") and not ecma_search(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


def _is_decimal(checker, instance) -> bool:
    return isinstance(instance, Decimal)


def _is_unspelled_integer(checker, instance) -> bool:
    return isinstance(instance, Decimal) and not isinstance(instance, FractionalSpelling)


# `integer` holds only for a number written without fraction or exponent: the
# schemas say where integers are, and the wire spelling is part of the contract.
_SpellingValidator = validators.extend(
    Draft202012Validator,
    validators={"pattern": _ecma_pattern},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many(
        {"integer": _is_unspelled_integer, "number": _is_decimal}
    ),
)


def _spelling_mismatches(contract: Contract, schema_id: str, actual: object) -> list[tuple[str, str]]:
    validator = _SpellingValidator({"$ref": schema_uri(schema_id)}, registry=contract.registry)
    found = []
    for error in validator.iter_errors(actual):
        at = pointer(error.absolute_path)
        if error.validator == "type" and isinstance(error.instance, FractionalSpelling):
            found.append((at, f"integer place written as {error.instance}; write it without fraction or exponent"))
        else:
            found.append((at, f"does not fit the schema: {error.message}"))
    return found


def compare_file(
    expected_path: Path, actual_path: Path, name: str, contract: Contract | None = None
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
    schema_id = Path(name).parent.as_posix()
    if not found and contract is not None and schema_id in contract.schema_ids:
        found = _spelling_mismatches(contract, schema_id, actual)
    return [Mismatch(name, reason, at) for at, reason in found]


def compare_dirs(expected_dir: Path, actual_dir: Path, contract: Contract | None = None) -> list[Mismatch]:
    """Compare every `*.json` under expected_dir with the same path under actual_dir.

    With a contract, a file at `<schema_id>/<case>.json` whose values all match is also checked
    against that schema, so an integer place written as `0.0` or `1E0` is a mismatch.
    """
    mismatches: list[Mismatch] = []
    for expected_path in sorted(Path(expected_dir).rglob("*.json")):
        name = expected_path.relative_to(expected_dir).as_posix()
        mismatches += compare_file(expected_path, Path(actual_dir) / name, name, contract)
    return mismatches
