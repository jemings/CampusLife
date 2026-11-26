"""Checks that a schema file uses only the contract's JSON Schema vocabulary."""

from __future__ import annotations

import json
from importlib.resources import files

from jsonschema import ValidationError, validators
from jsonschema.exceptions import best_match

from .dialect import ContractValidator

VOCABULARY = json.loads(files(__package__).joinpath("vocabulary.json").read_text(encoding="utf-8"))


def _discriminated_union(validator, enabled, schema, metaschema):
    """A discriminator must make every union error explainable.

    The object must be typed, the discriminator required and enumerated, every
    oneOf branch a bare $ref, and mapping must pair each enum value with
    exactly one branch. Otherwise dropping the `oneOf` error in favour of the
    selected branch, or of the sibling keyword, could leave no error at all.
    """
    if not isinstance(schema, dict) or "discriminator" not in schema:
        return
    discriminator = schema["discriminator"]
    name = discriminator.get("propertyName")
    mapping = discriminator.get("mapping", {})
    branches = schema.get("oneOf", [])
    branch_refs = [branch.get("$ref") for branch in branches if isinstance(branch, dict)]
    enum = schema.get("properties", {}).get(name, {}).get("enum")
    if schema.get("type") != "object":
        yield ValidationError("a discriminated union must be type object")
    if not branches:
        yield ValidationError("a discriminator needs a oneOf next to it")
    if any(not isinstance(branch, dict) or set(branch) != {"$ref"} for branch in branches):
        yield ValidationError("every oneOf branch of a discriminated union must be a bare $ref")
    if name not in schema.get("required", []):
        yield ValidationError(f"discriminator {name!r} must be listed in required")
    if enum is None:
        yield ValidationError(f"discriminator {name!r} must be an enum in properties")
    elif set(mapping) != set(enum):
        yield ValidationError(f"discriminator mapping keys {sorted(mapping)} must be the enum {sorted(enum)}")
    if sorted(mapping.values()) != sorted(branch_refs) or len(set(branch_refs)) != len(branch_refs):
        yield ValidationError("discriminator mapping must name each oneOf branch exactly once")


VocabularyValidator = validators.extend(
    ContractValidator, validators={"x-discriminated-union": _discriminated_union}
)


def vocabulary_error(document: dict) -> str | None:
    error = best_match(VocabularyValidator(VOCABULARY).iter_errors(document))
    if error is None:
        return None
    location = "/".join(str(part) for part in error.absolute_path)
    return f"at /{location}: {error.message}"
