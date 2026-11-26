"""Turn `jsonschema` errors into contract violations with stable paths."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from jsonschema import ValidationError

from .rules import RULE_KEYWORDS
from .jsonio import pointer
from .violations import RuleViolation, SchemaViolation, Violation


def _missing_members(error: ValidationError) -> list[str]:
    return [name for name in error.validator_value if name not in error.instance]


def _missing_dependencies(error: ValidationError) -> list[str]:
    present = [name for name in error.validator_value if name in error.instance]
    return [need for name in present for need in error.validator_value[name] if need not in error.instance]


def _unexpected_members(error: ValidationError) -> list[str]:
    declared = error.schema.get("properties", {})
    return [name for name in error.instance if name not in declared]


MEMBER_KEYWORDS: dict[str, Callable[[ValidationError], list[str]]] = {
    "required": _missing_members,
    "additionalProperties": _unexpected_members,
    "dependentRequired": _missing_dependencies,
}


def _discriminated_branch_errors(error: ValidationError) -> list[ValidationError] | None:
    """What a discriminated `oneOf` error stands for.

    - None: keep the `oneOf` error itself (no discriminator, or the selected
      branch has no errors of its own, so another branch matched as well).
    - []: no branch is selected because the discriminator is missing, not a
      mapped value or the instance is not an object; the sibling `type`,
      `required` or `enum` keyword reports that, which the vocabulary
      guarantees is present.
    - otherwise the errors of the branch `discriminator.mapping` selects.
    """
    discriminator = error.schema.get("discriminator")
    if error.validator != "oneOf" or discriminator is None:
        return None
    mapping = discriminator["mapping"]
    value = error.instance.get(discriminator["propertyName"]) if isinstance(error.instance, dict) else None
    if not isinstance(value, str) or value not in mapping:
        return []
    selected = error.validator_value.index({"$ref": mapping[value]})
    branch_errors = [child for child in error.context if child.relative_schema_path[0] == selected]
    return branch_errors or None


def _violations_of(error: ValidationError) -> set[Violation]:
    path = list(error.absolute_path)
    branch_errors = _discriminated_branch_errors(error)
    if branch_errors is not None:
        return violations_from(branch_errors)
    if error.validator in RULE_KEYWORDS:
        return {RuleViolation(pointer(path), RULE_KEYWORDS[error.validator].rule)}
    members_of = MEMBER_KEYWORDS.get(error.validator)
    if members_of is not None:
        return {SchemaViolation(pointer([*path, member]), error.validator) for member in members_of(error)}
    return {SchemaViolation(pointer(path), error.validator)}


def violations_from(errors: Iterable[ValidationError]) -> set[Violation]:
    return set().union(*map(_violations_of, errors))
