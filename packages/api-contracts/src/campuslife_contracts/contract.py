from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from referencing import Registry
from referencing.jsonschema import DRAFT202012

from .dialect import ContractValidator
from .errors import violations_from
from .nfc import nfc_violations
from .violations import Violation, violation_order
from .vocabulary import vocabulary_error

SCHEMA_BASE = "https://campuslife.invalid/contracts/schemas/"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


class ContractError(Exception):
    """A schema file under `schemas/` is not a well-formed contract schema."""


def schema_uri(schema_id: str) -> str:
    return f"{SCHEMA_BASE}{schema_id}.json"


def _read_schema(path: Path, schema_id: str) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{schema_id}: not UTF-8 JSON ({error})") from error
    if not isinstance(document, dict):
        raise ContractError(f"{schema_id}: a schema file must hold a JSON object")
    if document.get("$schema") != DRAFT_2020_12:
        raise ContractError(f"{schema_id}: $schema must be {DRAFT_2020_12} (JSON Schema 2020-12)")
    if document.get("$id") != schema_uri(schema_id):
        raise ContractError(f"{schema_id}: $id must be {schema_uri(schema_id)}")
    problem = vocabulary_error(document)
    if problem is not None:
        raise ContractError(f"{schema_id}: {problem}")
    return document


def _as_contract_resource(document: dict):
    # `jsonschema` picks the validator class from `$schema` whenever it follows
    # a `$ref`, which would drop the contract dialect for referenced schemas.
    body = {key: value for key, value in document.items() if key != "$schema"}
    return DRAFT202012.create_resource(body)


def _require_json_loads_types(value: object) -> None:
    match value:
        case float() if not math.isfinite(value):
            raise TypeError(f"{value!r} is not a JSON number")
        case None | bool() | int() | float() | str():
            return
        case list():
            for item in value:
                _require_json_loads_types(item)
        case dict():
            for name, item in value.items():
                if not isinstance(name, str):
                    raise TypeError(f"member name {name!r} is not a str")
                _require_json_loads_types(item)
        case _:
            raise TypeError(f"{type(value).__name__} is not a json.loads type")


@dataclass(frozen=True)
class Contract:
    """The schemas under one contract root, ready to validate instances."""

    registry: Registry
    schema_ids: frozenset[str]

    @classmethod
    def load(cls, root: Path) -> Contract:
        schemas_dir = Path(root) / "schemas"
        resources = {}
        for path in sorted(schemas_dir.rglob("*.json")):
            schema_id = path.relative_to(schemas_dir).with_suffix("").as_posix()
            resources[schema_id] = _as_contract_resource(_read_schema(path, schema_id))
        registry = Registry().with_resources(
            (schema_uri(schema_id), resource) for schema_id, resource in resources.items()
        )
        return cls(registry=registry.crawl(), schema_ids=frozenset(resources))

    @cached_property
    def _validators(self) -> dict[str, ContractValidator]:
        return {
            schema_id: ContractValidator({"$ref": schema_uri(schema_id)}, registry=self.registry)
            for schema_id in self.schema_ids
        }

    def validate(self, schema_id: str, instance: object) -> list[Violation]:
        """Violations of one instance built from `json.loads` default types."""
        validator = self._validators[schema_id]
        _require_json_loads_types(instance)
        violations = violations_from(validator.iter_errors(instance))
        violations.update(nfc_violations(instance))
        return sorted(violations, key=violation_order)
