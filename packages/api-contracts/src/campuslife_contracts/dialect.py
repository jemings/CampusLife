"""The JSON Schema 2020-12 validator the contract is checked with.

`pattern` and `integer` differ from `jsonschema`'s defaults so that Python
accepts exactly what the Kotlin and Swift decoders accept, and the contract
rule keywords are added.
"""

from __future__ import annotations

from jsonschema import Draft202012Validator, ValidationError, validators

from .ecma import ecma_search
from .rules import RULE_KEYWORDS


def _is_strict_integer(checker, instance) -> bool:
    return isinstance(instance, int) and not isinstance(instance, bool)


def _ecma_pattern(validator, pattern, instance, schema):
    if validator.is_type(instance, "string") and not ecma_search(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


ContractValidator = validators.extend(
    Draft202012Validator,
    validators={
        "pattern": _ecma_pattern,
        **{keyword: entry.check for keyword, entry in RULE_KEYWORDS.items()},
    },
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_strict_integer),
)
