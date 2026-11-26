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

CLOSED = {
    "capture/commit", "capture/commit_attachment", "common/local_date", "common/local_time_rule",
    "mutation/mutation", "mutation/batch_request",
    "device/pair_request", "device/status",
    "blob/upload_request",
    "sync/changes_query",
}
OPEN = {
    "capture/capture", "capture/attachment", "capture/origin", "capture/timeline",
    "item/item", "relation/relation", "tag/tag", "common/error",
    "mutation/result", "mutation/batch_response",
    "sync/changes_page", "sync/hub", "sync/request_headers", "sync/response_headers",
    "device/pair_response", "device/pair_failure_headers",
    "blob/upload_session", "blob/stored", "blob/download_request_headers", "blob/download_response_headers",
    "blob/range_not_satisfiable_headers",
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


MUTATION = schema("mutation/mutation")["$defs"]
ITEM = schema("item/item")
KINDS = sorted(ITEM["$defs"]["kind_is"])
HUB_OWNED = {name for name, member in ITEM["properties"].items() if member.get("x-owner") == "hub"}


def test_create_writes_the_item_columns_minus_the_hubs_and_the_id():
    assert names(MUTATION["item_data"]) == names(ITEM) - HUB_OWNED - {"id", "status"}


@pytest.mark.parametrize("kind", KINDS)
def test_create_closes_exactly_the_members_of_each_kind(kind):
    assert names(MUTATION["closed_fields"][kind]) == names(schema(f"item/fields/{kind}"))


def test_create_and_patch_know_the_same_kinds_as_the_item():
    dispatched = {branch["if"]["$ref"].rsplit("/", 1)[1] for branch in MUTATION["closed_fields_of_kind"]["allOf"]}
    patched = {ref.rsplit("/", 1)[1].removesuffix(".json") for ref in MUTATION["patch"]["x-set-targets"]["item"]["fields"]}
    assert dispatched == patched == set(KINDS) == set(MUTATION["closed_fields"])


def test_create_writes_the_relation_and_tag_columns_minus_the_id():
    assert names(MUTATION["relation_data"]) == names(schema("relation/relation")) - {"id"}
    assert names(MUTATION["tag_data"]) == names(schema("tag/tag")) - {"id"}


def free_form(at):
    """Places a client fills with column names or values by design: a patch's set values and accept edits."""
    strings = [part for part in at if isinstance(part, str)]
    if "edits" in strings:
        return True
    if "set" in strings:
        return len(at) - at.index("set") > 2 or len(at) - at.index("set") == 1
    return False


def unknown_kind_fields(sample, at):
    """An unknown kind's fields are the Hub's to judge (mutation.json closed_fields_of_kind)."""
    data = sample.get("data", {})
    return at[:2] == ("data", "fields") and isinstance(data, dict) and data.get("kind") not in KINDS


MUTATION_SAMPLES = [
    (c.name, read_strict(c.path)) for c in CASES if c.kind is FixtureKind.VALID and c.schema_id == "mutation/mutation"
]


@pytest.mark.parametrize(
    ("name", "at"),
    [
        (name, at)
        for name, sample in MUTATION_SAMPLES
        for at in object_paths(sample)
        if not free_form(at) and not unknown_kind_fields(sample, at)
    ],
    ids=lambda value: value if isinstance(value, str) else "/".join(map(str, value)) or "root",
)
def test_every_object_inside_a_mutation_is_closed(name, at):
    sample = dict(MUTATION_SAMPLES)[name]
    pointer = "".join(f"/{part}" for part in at)
    assert CONTRACT.validate("mutation/mutation", with_member_at(sample, at, "zz_unknown")) == [
        SchemaViolation(f"{pointer}/zz_unknown", "additionalProperties")
    ]
