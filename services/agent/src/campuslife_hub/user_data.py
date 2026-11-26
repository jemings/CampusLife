"""create, patch, delete and restore of user data: Items, Relations, Tags and an Attachment's cloud_policy (D1, D18, A4).

The applier has run `validate()` before any of these. What it cannot judge, because
it needs the Hub's rows, is judged here.
"""

from __future__ import annotations

import json

from campuslife_contracts import contract_root

from . import store
from .created import earlier_create, remember_create
from .db import Tx
from .devices import DeviceId
from .outcomes import Applied, Outcome, Reason, Rejected
from .store import Entity, EntityRef


def _schema(name: str) -> dict:
    return json.loads((contract_root() / "schemas" / name).read_text(encoding="utf-8"))


ITEM_KINDS = frozenset(_schema("mutation/mutation.json")["$defs"]["closed_fields"])
"""Kinds whose fields the contract defines; this Hub cannot judge another kind's fields, so it does not create one."""

ITEM_TEXT_COLUMNS = frozenset(name for name, column in _schema("item/item.json")["properties"].items() if column.get("x-text") is True)

SCHEMA_VERSION = 1


def _is_canonical_text(text: object) -> bool:
    """An x-text value arrives with LF line ends (E7), so its from_hash is the sha256 of what is stored."""
    return not isinstance(text, str) or "\r" not in text


def _create_item(tx: Tx, ref: EntityRef, data: dict) -> Outcome:
    if data["kind"] not in ITEM_KINDS:
        return Rejected(Reason.SCHEMA)
    if not all(_is_canonical_text(data.get(column)) for column in ITEM_TEXT_COLUMNS):
        return Rejected(Reason.NOT_CANONICAL)
    earlier = earlier_create(tx, ref, data)
    if earlier is not None:
        return earlier
    store.write(tx, ref, {"id": ref.id, "schema_version": SCHEMA_VERSION, **data})
    remember_create(tx, ref, data)
    return Applied()


CREATES = {Entity.ITEM: _create_item}


def create(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    ref = EntityRef(Entity(mutation["entity"]), mutation["id"])
    return CREATES[ref.entity](tx, ref, mutation["data"])
