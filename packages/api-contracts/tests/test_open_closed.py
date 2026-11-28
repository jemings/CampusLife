"""Shapes the Hub sends stay open; input the Hub receives is closed.

A client ignores members it does not know in what the Hub sends (E21), so
those shapes accept extra members. capture.commit is input and refuses them.
"""

import pytest

from campuslife_contracts import Rule, RuleViolation, SchemaViolation, bundled_contract, contract_root
from campuslife_contracts.contract import schema_uri
from campuslife_contracts.dialect import ContractValidator
from campuslife_contracts.errors import violations_from
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
    "device/pair_response", "device/pair_failure_headers", "device/pairing_offer",
    "blob/upload_session", "blob/stored", "blob/download_request_headers", "blob/download_response_headers",
    "blob/range_not_satisfiable_headers",
    "locator/locator", "artifact/artifact", "artifact/body", "artifact/extraction", "artifact/identify", "suggestion/suggestion",
    "conflict_record/conflict_record",
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


EVERY_OPEN_VALID = [
    (c.schema_id, c.name, read_strict(c.path))
    for c in CASES if c.kind is FixtureKind.VALID and c.schema_id in OPEN and isinstance(read_strict(c.path), dict)
]


@pytest.mark.parametrize(
    ("schema_id", "instance"), [(schema_id, instance) for schema_id, _, instance in EVERY_OPEN_VALID],
    ids=[f"{schema_id}/{name}" for schema_id, name, _ in EVERY_OPEN_VALID],
)
def test_open_shape_ignores_an_unknown_member(schema_id, instance):
    assert CONTRACT.validate(schema_id, {**instance, "zz_unknown": 1}) == []


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


MVP_KINDS = {"term", "course", "lecture_session", "note", "task", "assignment", "exam"}
LATER_KINDS = {"event", "announcement", "opportunity", "organization"}
"""#9 decision 8; person and place wait for a consumer."""


def test_the_item_knows_the_mvp_and_the_later_kinds():
    assert set(KINDS) == MVP_KINDS | LATER_KINDS


def last_segment(ref):
    return ref.rsplit("/", 1)[-1].removesuffix(".json")


def dispatch(branches):
    """kind -> the name its fields go to, from an allOf of `if kind_is/<kind> then fields $ref`."""
    return {last_segment(b["if"]["$ref"]): last_segment(b["then"]["properties"]["fields"]["$ref"]) for b in branches}


def listed(refs):
    return {last_segment(ref): last_segment(ref) for ref in refs}


KIND_PLACES = {
    "item/fields/*.json": listed(path.name for path in (contract_root() / "schemas" / "item" / "fields").glob("*.json")),
    "item kind_is": {kind: test["properties"]["kind"]["const"] for kind, test in ITEM["$defs"]["kind_is"].items()},
    "item fields_of_kind": dispatch(ITEM["$defs"]["fields_of_kind"]["allOf"]),
    "mutation closed_fields_of_kind": dispatch(MUTATION["closed_fields_of_kind"]["allOf"]),
    "mutation closed_fields": {kind: last_segment(view["$ref"]) for kind, view in MUTATION["closed_fields"].items()},
    "mutation patch x-set-targets": listed(MUTATION["patch"]["x-set-targets"]["item"]["fields"]),
    "suggestion item_patch x-set-targets": listed(
        schema("suggestion/suggestion")["$defs"]["item_patch"]["x-set-targets"]["item"]["fields"]
    ),
}
"""Every place that names the known Item kinds, as kind -> the fields schema it means there."""


@pytest.mark.parametrize("place", sorted(KIND_PLACES))
def test_every_place_that_names_the_kinds_names_each_with_its_own_fields_schema(place):
    """A kind missing from one place would leave its fields unchecked there: one list, checked in one test."""
    assert KIND_PLACES[place] == {kind: kind for kind in KINDS}


def fields_by_name():
    """Each member name of the item/fields schemas -> {kind: its member node}."""
    found = {}
    for path in (contract_root() / "schemas" / "item" / "fields").glob("*.json"):
        for name, member in read_strict(path).get("properties", {}).items():
            found.setdefault(name, {})[path.stem] = member
    return found


SHARED_FIELDS = {name: defining for name, defining in fields_by_name().items() if len(defining) >= 2}
"""Fields several kinds define, found rather than listed so a name becomes shared the moment a second kind uses it. A
patch's fields.<key> passes when it fits any kind that defines the key (rule set_type), so one kind's looser copy would
loosen the patch of every kind."""


def test_the_shared_fields_include_the_ones_the_later_kinds_were_built_on():
    assert {"category", "organization_id", "url", "course_id", "due_text"} <= set(SHARED_FIELDS)


@pytest.mark.parametrize("name", sorted(SHARED_FIELDS))
def test_a_field_several_kinds_share_is_one_definition_they_refer_to(name):
    defining = SHARED_FIELDS[name]
    assert len({member.get("$ref") for member in defining.values()}) == 1, defining
    for kind, member in defining.items():
        assert "$ref" in member and set(member) <= {"$ref", "description", "examples"}, kind


def test_create_writes_the_relation_and_tag_columns_minus_the_id():
    """A Relation's locators are the Hub's: it copies them from a Suggestion's evidence on accept."""
    assert names(MUTATION["relation_data"]) == names(schema("relation/relation")) - {"id", "locators"}
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


SUGGESTION = schema("suggestion/suggestion")["$defs"]


def test_a_proposal_names_the_members_of_the_input_it_mirrors():
    """Suggestion's proposals are open copies of the closed Mutation input, status refusal included."""
    assert names(SUGGESTION["item_proposal"]) == names(MUTATION["item_data"]) | {"status"}
    assert names(SUGGESTION["proposed_change"]) == names(MUTATION["change"])


def test_a_resolve_sets_what_a_patch_of_the_same_entity_sets():
    """D24 decision 3: conflict.resolve's set is a patch of the record's entity, judged by the same rules."""
    resolve, patch = MUTATION["conflict_resolve"], MUTATION["patch"]
    assert resolve["properties"]["set"] == patch["properties"]["set"]
    assert resolve["properties"]["entity"] == patch["properties"]["entity"]
    assert {key: value for key, value in resolve.items() if key.startswith("x-set-")} == {
        key: value for key, value in patch.items() if key.startswith("x-set-")
    }


def test_a_conflict_record_keeps_a_change_as_the_patch_sent_it():
    """D24 decision 1: the record inlines the submitted set entries, an open copy of the closed input."""
    record = schema("conflict_record/conflict_record")
    assert record["properties"]["set"]["propertyNames"] == {"$ref": "../mutation/mutation.json#/$defs/set_key"}
    assert names(record["$defs"]["submitted_change"]) == names(MUTATION["change"])
    assert "additionalProperties" not in record["$defs"]["submitted_change"]


CLOSED_VALUE_TYPES = ("common/local_date", "common/local_time_rule")
FREE_FORM_IN_FEED = {
    ("suggestion/suggestion", ("proposed", "set")),
    ("conflict_record/conflict_record", ("set",)),
    ("sync/changes_page", ("changes", "data", "set")),
}
HASHED = {("artifact/extraction", ("extractor", "params"))}
"""Open objects a rule hashes: a member added there is another value, which the block ids follow (rule block_id)."""
KEYED_BY_CELL = {(schema_id, ("payload", "formulas")) for schema_id in ("artifact/body", "artifact/extraction")}
"""A table_row's formulas map names cells, not members: a new name is another cell, not a newer Hub's member.
The exemption matches the path suffix payload/formulas only, so a formulas member of another block_type's payload is
exempt too."""
OPEN_SAMPLES = [
    (f"{c.schema_id}/{c.name}", c.schema_id, read_strict(c.path))
    for c in CASES if c.kind is FixtureKind.VALID and c.schema_id in OPEN
]


def node_at(sample, at):
    for part in at:
        sample = sample[part]
    return sample


def closed_inside(schema_id, sample, at):
    """A closed value type (D20), a map whose names are columns or cells, or an object a rule hashes, inside an open
    shape."""
    names_only = tuple(part for part in at if isinstance(part, str))
    if any(
        names_only[-len(path):] == path and schema_id == owner for owner, path in FREE_FORM_IN_FEED | HASHED | KEYED_BY_CELL
    ):
        return True
    return any(CONTRACT.validate(value_type, node_at(sample, at)) == [] for value_type in CLOSED_VALUE_TYPES)


NESTED = [
    (name, at)
    for name, schema_id, sample in OPEN_SAMPLES
    for at in object_paths(sample)
    if at and isinstance(node_at(sample, at), dict) and not closed_inside(schema_id, sample, at)
]


@pytest.mark.parametrize(("name", "at"), NESTED, ids=lambda v: v if isinstance(v, str) else "/".join(map(str, v)))
def test_every_object_inside_an_open_shape_is_open(name, at):
    _, schema_id, sample = next(entry for entry in OPEN_SAMPLES if entry[0] == name)
    assert CONTRACT.validate(schema_id, with_member_at(sample, at, "zz_unknown")) == []


def test_the_open_samples_reach_the_nested_feed_shapes():
    shapes = {(name.rsplit("/", 1)[0], tuple(part for part in at if isinstance(part, str))) for name, at in NESTED}
    assert {
        ("suggestion/suggestion", ("proposed",)),
        ("suggestion/suggestion", ("proposed", "fields")),
        ("suggestion/suggestion", ("proposed", "set", "due_at")),
        ("suggestion/suggestion", ("evidence",)),
        ("relation/relation", ("locators",)),
        ("sync/changes_page", ("changes", "data", "proposed")),
        ("sync/changes_page", ("changes", "data", "evidence")),
        ("capture/capture", ("origin",)),
        ("capture/timeline", ("anchors",)),
        ("item/item", ("fields",)),
        ("conflict_record/conflict_record", ("set", "body")),
        ("conflict_record/conflict_record", ("resolved",)),
        ("sync/changes_page", ("changes", "data", "set", "body")),
        ("sync/changes_page", ("changes", "data", "resolved")),
        ("artifact/body", ("blocks", "payload")),
        ("artifact/extraction", ("blocks", "payload")),
    } <= shapes


def view(schema_id, pointer):
    return ContractValidator({"$ref": f"{schema_uri(schema_id)}#{pointer}"}, registry=CONTRACT.registry)


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ({"v": 1, "kind": "document", "blob": "ab" * 32}, set()),
        ({"v": 1, "kind": "annotation", "blob": "ab" * 32}, {("/kind", "enum")}),
        ({"v": 2, "kind": "document", "blob": "ab" * 32}, {("/v", "maximum")}),
    ],
    ids=["known", "unknown-kind", "newer-version"],
)
def test_the_locator_input_view_takes_only_known_kinds_of_version_one(instance, expected):
    found = {(v.path, v.keyword) for v in violations_from(view("locator/locator", "/$defs/input").iter_errors(instance))}
    assert found == expected


@pytest.mark.parametrize(("schema_id", "name"), [("locator/locator", "kind"), ("suggestion/suggestion", "action")])
def test_an_open_union_names_its_known_branches_as_examples(schema_id, name):
    document = schema(schema_id)
    known = document["$defs"]["known"]
    assert document["properties"][name]["examples"] == known["properties"][name]["enum"] == list(known["discriminator"]["mapping"])


KNOWN_BLOCK_TYPES = ["heading", "paragraph", "page", "table", "table_row", "table_header", "notes"]
"""#6 fixes the document extractors' vocabulary before #7 and #13 add theirs (D20: the examples are the known values)."""
KNOWN_WARNING_CODES = ["no_text_layer", "limit_reached", "replaced_characters", "objects_skipped"]


def test_the_known_block_types_and_warning_codes_are_the_ones_listed_here():
    block = schema("artifact/body")["$defs"]["block"]["properties"]["block_type"]
    warning = schema("artifact/extraction")["$defs"]["warning"]["properties"]["code"]
    assert (block["examples"], warning["examples"]) == (KNOWN_BLOCK_TYPES, KNOWN_WARNING_CODES)


def test_the_block_types_with_a_payload_shape_are_known_ones():
    body = schema("artifact/body")["$defs"]
    shaped = {branch["if"]["$ref"].rsplit("/", 1)[1] for branch in body["payload_of_type"]["allOf"]}
    assert shaped == set(body["block_type_is"]) == {"heading", "table_row"}
    assert shaped <= set(KNOWN_BLOCK_TYPES)


KNOWN_RELATION_TYPES = ["about", "supersedes", "rendition_of", "evidence", "tagged", "conflicts_with"]
LOCATOR = {"v": 1, "kind": "document", "blob": "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"}


def test_the_known_relation_types_are_the_ones_listed_here():
    assert sorted(schema("relation/relation")["properties"]["type"]["examples"]) == sorted(KNOWN_RELATION_TYPES)


@pytest.mark.parametrize("relation_type", [name for name in KNOWN_RELATION_TYPES if name != "evidence"])
def test_a_known_relation_type_other_than_evidence_refuses_locators(relation_type):
    relation = next(
        read_strict(c.path) for c in CASES
        if c.kind is FixtureKind.VALID and c.schema_id == "relation/relation" and read_strict(c.path)["type"] == relation_type
    )
    assert CONTRACT.validate("relation/relation", {**relation, "locators": [LOCATOR]}) == [
        RuleViolation("/locators", Rule.LOCATOR_ANCHOR)
    ]
