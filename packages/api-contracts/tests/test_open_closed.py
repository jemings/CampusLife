"""Shapes the Hub sends stay open; input the Hub receives is closed.

A client ignores members it does not know in what the Hub sends (E21), so
those shapes accept extra members. capture.commit is input and refuses them.
"""

import pytest

from campuslife_contracts import SchemaViolation, bundled_contract, contract_root
from campuslife_contracts.fixtures import FixtureKind, discover
from campuslife_contracts.jsonio import read_strict

CONTRACT = bundled_contract()
CASES = discover(contract_root())

CLOSED = {"capture/commit", "capture/commit_attachment", "common/local_date", "common/local_time_rule"}
OPEN = {
    "capture/capture", "capture/attachment", "capture/origin", "capture/timeline",
    "item/item", "relation/relation", "tag/tag", "common/error",
    *(schema_id for schema_id in CONTRACT.schema_ids if schema_id.startswith("item/fields/")),
}


def test_every_object_schema_is_classified():
    objects = {case.schema_id for case in CASES if case.kind is FixtureKind.VALID and isinstance(read_strict(case.path), dict)}
    assert objects == CLOSED | OPEN


def first_valid(schema_id):
    return next(read_strict(c.path) for c in CASES if c.kind is FixtureKind.VALID and c.schema_id == schema_id)


@pytest.mark.parametrize("schema_id", sorted(CLOSED))
def test_closed_input_refuses_an_unknown_member(schema_id):
    instance = {**first_valid(schema_id), "zz_unknown": 1}
    assert CONTRACT.validate(schema_id, instance) == [SchemaViolation("/zz_unknown", "additionalProperties")]


@pytest.mark.parametrize("schema_id", sorted(OPEN))
def test_open_shape_ignores_an_unknown_member(schema_id):
    assert CONTRACT.validate(schema_id, {**first_valid(schema_id), "zz_unknown": 1}) == []


def schema(schema_id):
    return read_strict(contract_root() / "schemas" / f"{schema_id}.json")


def names(subschema):
    return set(subschema["properties"])


@pytest.mark.parametrize(
    ("closed", "open_"),
    [
        (("capture/commit", "closed_origin"), ("capture/origin", None)),
        (("capture/commit", "closed_timeline"), ("capture/timeline", None)),
        (("capture/commit", "closed_anchor"), ("capture/timeline", "anchor")),
        (("capture/commit", "closed_event"), ("capture/timeline", "event")),
    ],
    ids=["origin", "timeline", "anchor", "event"],
)
def test_commit_closes_exactly_the_members_of_the_open_shape(closed, open_):
    closed_view = schema(closed[0])["$defs"][closed[1]]
    open_shape = schema(open_[0]) if open_[1] is None else schema(open_[0])["$defs"][open_[1]]
    assert names(closed_view) == names(open_shape)


def test_commit_input_is_the_feed_shape_minus_what_the_hub_fills():
    assert names(schema("capture/commit")) == names(schema("capture/capture")) - {"device_id"} | {"links", "attachments"}
    assert names(schema("capture/commit_attachment")) == names(schema("capture/attachment")) - {
        "capture_id", "detected_mime", "backed_up_at"
    }


def object_paths(node, at=()):
    if isinstance(node, dict):
        yield at
        for name, value in node.items():
            yield from object_paths(value, (*at, name))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from object_paths(value, (*at, index))


def with_member_at(node, at, name):
    if not at:
        return {**node, name: 1}
    head, *rest = at
    copy = list(node) if isinstance(node, list) else dict(node)
    copy[head] = with_member_at(node[head], rest, name)
    return copy


RECORDING = next(
    read_strict(c.path) for c in CASES
    if c.kind is FixtureKind.VALID and c.schema_id == "capture/commit" and c.name == "recording_with_links"
)


@pytest.mark.parametrize("at", list(object_paths(RECORDING)), ids=lambda at: "/".join(map(str, at)) or "root")
def test_every_object_inside_a_commit_is_closed(at):
    pointer = "".join(f"/{part}" for part in at)
    assert CONTRACT.validate("capture/commit", with_member_at(RECORDING, at, "zz_unknown")) == [
        SchemaViolation(f"{pointer}/zz_unknown", "additionalProperties")
    ]


def test_the_closure_sample_reaches_every_nested_shape():
    shapes = {tuple(part for part in at if isinstance(part, str)) for at in object_paths(RECORDING)}
    assert shapes == {(), ("origin",), ("links",), ("attachments",), ("timeline",), ("timeline", "anchors"),
                      ("timeline", "events")}
