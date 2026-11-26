"""Rules owner and set_type for a patch's `set` and an accept's `edits` (E5, E6, A3).

The patch schema names, per entity, the entity schema whose columns `set`
writes and, for entities with kind-specific fields, the fields schemas a
`fields.<key>` may belong to (`x-set-targets`). A patch does not say which
kind it edits, so a `fields.<key>` is checked against every fields schema
that defines that key.

A patch writes only columns marked x-owner user (D1): hub columns are the
Hub's, client columns were fixed by the creating client, and an unmarked
column is not writable. Values must fit the column as input: an open enum
(x-open, left open for what the Hub sends) takes only its examples, also
inside $ref, allOf, anyOf, oneOf, properties, additionalProperties,
prefixItems and items.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import NamedTuple

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


def _fits_as_input(validator, schema: object, resolver, value: object) -> bool:
    """value fits schema, and every open enum it lands in knows it."""
    if next(validator.evolve(schema=schema, _resolver=resolver).iter_errors(value), None) is not None:
        return False
    if not isinstance(schema, dict):
        return True
    if schema.get("x-open") is True and value not in schema.get("examples", []):
        return False
    if "$ref" in schema:
        resolved = resolver.lookup(schema["$ref"])
        if not _fits_as_input(validator, resolved.contents, resolved.resolver, value):
            return False
    if not all(_fits_as_input(validator, branch, resolver, value) for branch in schema.get("allOf", [])):
        return False
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema and not any(_fits_as_input(validator, branch, resolver, value) for branch in schema[keyword]):
            return False
    return all(_fits_as_input(validator, member, resolver, part) for member, part in _parts(schema, value))


def _parts(schema: dict, value: object) -> Iterator[tuple[object, object]]:
    """(subschema, part of value) for each member or element a subschema applies to."""
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name, part in value.items():
            if name in properties:
                yield properties[name], part
            elif "additionalProperties" in schema:
                yield schema["additionalProperties"], part
    elif isinstance(value, list):
        prefix = schema.get("prefixItems", [])
        yield from zip(prefix, value)
        if "items" in schema:
            yield from ((schema["items"], part) for part in value[len(prefix):])


def _fits(validator, column: Column, value: object) -> bool:
    return any(_fits_as_input(validator, schema, resolver, value) for schema, resolver in column.schemas)


class Judged(NamedTuple):
    """One change a rule judges: its key, the change as {from?, from_hash?, to}, its column, and where it sits."""

    key: str
    change: dict
    column: Column | None
    at: list
    to_at: list


def _judged_changes(validator, schema: dict, instance: object) -> Iterator[Judged]:
    """Each change the schema itself accepts.

    A patch's set maps keys to changes of the entity its entity member names. With x-set-edits, edits
    maps keys straight to the value to write, the one entity of x-set-targets is the target, and
    fields.<key> is left to the Hub, which alone knows the Suggestion it edits.
    """
    edits = schema.get("x-set-edits") is True
    member = "edits" if edits else "set"
    if not isinstance(instance, dict) or not isinstance(instance.get(member), dict):
        return
    properties = schema.get("properties", {})
    if edits:
        (target,) = schema["x-set-targets"].values()
    else:
        entity = instance.get("entity")
        target = schema["x-set-targets"].get(entity) if isinstance(entity, str) else None
        if target is None or next(validator.descend(entity, properties.get("entity", True)), None) is not None:
            return
    member_schema = properties.get(member, {})
    for key, value in instance[member].items():
        if edits and key.startswith(FIELDS_PREFIX):
            continue
        fits_key = next(validator.descend(key, member_schema.get("propertyNames", True)), None) is None
        fits_value = next(validator.descend(value, member_schema.get("additionalProperties", True)), None) is None
        if fits_key and fits_value:
            change, to_at = ({"to": value}, [member, key]) if edits else (value, [member, key, "to"])
            yield Judged(key, change, _column(validator, target, key), [member, key], to_at)


def set_owner(validator, enabled, instance, schema):
    for judged in _judged_changes(validator, schema, instance):
        column = judged.column
        if column is not None and column.owner != "user":
            yield ValidationError(f"{judged.key} is not a column the user writes ({column.owner or 'no owner'})", path=judged.at)


def set_type(validator, enabled, instance, schema):
    for key, change, column, at, to_at in _judged_changes(validator, schema, instance):
        if column is None:
            yield ValidationError(f"{key} is not a column of this entity", path=at)
            continue
        if column.text and "from" in change:
            yield ValidationError(f"{key} is compared by from_hash", path=[*at, "from"])
        if not column.text and "from_hash" in change:
            yield ValidationError(f"{key} is compared by value", path=[*at, "from_hash"])
        for member in ("from", "to"):
            if member not in change:
                continue
            value = change[member]
            where = to_at if member == "to" else [*at, "from"]
            if value is None:
                if member == "to" and column.required:
                    yield ValidationError(f"{key} is required and cannot be removed", path=where)
            elif not _fits(validator, column, value):
                yield ValidationError(f"{member} does not fit {key}", path=where)
