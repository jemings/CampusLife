"""Rules owner and set_type for a patch's `set` (E5, E6, A3).

The patch schema names, per entity, the entity schema whose columns `set`
writes and, for entities with kind-specific fields, the fields schemas a
`fields.<key>` may belong to (`x-set-targets`). A patch does not say which
kind it edits, so a `fields.<key>` is checked against every fields schema
that defines that key.

A patch writes only columns marked x-owner user (D1): hub columns are the
Hub's, client columns were fixed by the creating client, and an unmarked
column is not writable. Values must fit the column as input: an open enum
(pattern plus examples, left open for what the Hub sends) takes only its
known values.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from jsonschema import ValidationError


FIELDS_PREFIX = "fields."


@dataclass(frozen=True)
class Column:
    """Where a set key lands and what it may hold."""

    owner: str | None
    text: bool
    required: bool
    schemas: list[tuple[dict, object]]


def _lookup(validator, ref: str) -> tuple[dict, object]:
    # jsonschema has no public way to resolve a reference from inside a keyword.
    resolved = validator._resolver.lookup(ref)
    return resolved.contents, resolved.resolver


def _column(validator, target: dict, key: str) -> Column | None:
    entity, entity_resolver = _lookup(validator, target["schema"])
    properties = entity.get("properties", {})
    if not key.startswith(FIELDS_PREFIX):
        member = properties.get(key)
        if member is None:
            return None
        return Column(member.get("x-owner"), member.get("x-text") is True, key in entity.get("required", []),
                      [(member, entity_resolver)])
    name = key.removeprefix(FIELDS_PREFIX)
    defining = [fields for fields in map(lambda ref: _lookup(validator, ref), target.get("fields", []))
                if name in fields[0].get("properties", {})]
    if not defining:
        return None
    return Column(
        properties.get("fields", {}).get("x-owner"),
        False,
        all(name in fields.get("required", []) for fields, _ in defining),
        [(fields["properties"][name], resolver) for fields, resolver in defining],
    )


def _known_values(schema: dict, resolver) -> list | None:
    """The examples of an open enum at the column or the schema it refers to."""
    if "examples" in schema:
        return schema["examples"]
    if "$ref" in schema:
        resolved = resolver.lookup(schema["$ref"])
        return _known_values(resolved.contents, resolved.resolver)
    return None


def _fits(validator, column: Column, value: object) -> bool:
    for schema, resolver in column.schemas:
        known = _known_values(schema, resolver)
        if known is not None and value not in known:
            continue
        if next(validator.evolve(schema=schema, _resolver=resolver).iter_errors(value), None) is None:
            return True
    return False


def _judged_changes(validator, schema: dict, instance: object) -> Iterator[tuple[str, dict, Column | None]]:
    """(key, change, column) for each change the schema itself accepts."""
    if not isinstance(instance, dict) or not isinstance(instance.get("set"), dict):
        return
    properties = schema.get("properties", {})
    entity = instance.get("entity")
    target = schema["x-set-targets"].get(entity) if isinstance(entity, str) else None
    if target is None or next(validator.descend(entity, properties.get("entity", True)), None) is not None:
        return
    set_schema = properties.get("set", {})
    for key, change in instance["set"].items():
        fits_key = next(validator.descend(key, set_schema.get("propertyNames", True)), None) is None
        fits_change = next(validator.descend(change, set_schema.get("additionalProperties", True)), None) is None
        if fits_key and fits_change:
            yield key, change, _column(validator, target, key)


def set_owner(validator, enabled, instance, schema):
    for key, _, column in _judged_changes(validator, schema, instance):
        if column is not None and column.owner != "user":
            yield ValidationError(f"{key} is not a column the user writes ({column.owner or 'no owner'})", path=["set", key])


def set_type(validator, enabled, instance, schema):
    for key, change, column in _judged_changes(validator, schema, instance):
        if column is None:
            yield ValidationError(f"{key} is not a column of this entity", path=["set", key])
            continue
        if column.text and "from" in change:
            yield ValidationError(f"{key} is compared by from_hash", path=["set", key, "from"])
        if not column.text and "from_hash" in change:
            yield ValidationError(f"{key} is compared by value", path=["set", key, "from_hash"])
        for member in ("from", "to"):
            if member not in change:
                continue
            value = change[member]
            if value is None:
                if member == "to" and column.required:
                    yield ValidationError(f"{key} is required and cannot be removed", path=["set", key, "to"])
            elif not _fits(validator, column, value):
                yield ValidationError(f"{member} does not fit {key}", path=["set", key, member])
