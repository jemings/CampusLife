"""Conventions every bundled schema follows."""

import pytest

from campuslife_contracts import contract_root
from campuslife_contracts.jsonio import read_strict

SCHEMAS = contract_root() / "schemas"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_integer_typed(node):
    """A subschema that only integers can satisfy, however it is spelled."""
    types = node.get("type")
    types = types if isinstance(types, list) else [types]
    return (
        "integer" in types
        or ("number" in types and "multipleOf" in node)
        or ("const" in node and _is_int(node["const"]))
        or (isinstance(node.get("enum"), list) and node["enum"] and all(_is_int(value) for value in node["enum"]))
    )


def integer_types(node, at=""):
    if isinstance(node, dict):
        if is_integer_typed(node):
            yield at or "/"
        for name, value in node.items():
            yield from integer_types(value, f"{at}/{name}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from integer_types(value, f"{at}/{index}")


@pytest.mark.parametrize(
    ("node", "integer"),
    [
        ({"type": "integer"}, True),
        ({"type": ["integer", "null"]}, True),
        ({"type": "number", "multipleOf": 1}, True),
        ({"const": 3}, True),
        ({"enum": [1, 2, 3]}, True),
        ({"type": "number"}, False),
        ({"const": True}, False),
        ({"enum": ["a", 1]}, False),
        ({"enum": [True, False]}, False),
    ],
)
def test_integer_typed_subschemas_are_recognised(node, integer):
    assert is_integer_typed(node) is integer


def test_every_integer_goes_through_int64():
    found = {
        f"{path.relative_to(SCHEMAS).as_posix()}#{at}"
        for path in SCHEMAS.rglob("*.json")
        for at in integer_types(read_strict(path))
    }
    assert found == {"common/int64.json#/"}


HUB = "hub"
OWNERS = {
    "artifact/artifact": dict.fromkeys(
        ["id", "artifact_type", "subject_ref", "job_key", "provider", "model", "prompt_version", "pipeline_version",
         "input_hashes", "content_blob", "created_at"], HUB),
    "capture/attachment": {
        "id": "client", "capture_id": "hub", "blob_sha256": "hub", "role": "client", "seq": "client",
        "original_filename": "client", "original_filename_raw": "client", "declared_mime": "client",
        "detected_mime": "hub", "captured_at": "client", "captured_at_precision": "client",
        "captured_mono_ns": "client", "cloud_policy": "user", "backed_up_at": "hub",
    },
    "capture/capture": {
        "id": "client", "device_id": "hub", "created_at": "client", "origin": "client", "category_hint": "client",
        "user_note": "client", "url": "client", "timeline": "client",
    },
    "item/item": {
        "id": "client", "kind": "client", "schema_version": "hub", "title": "user", "body": "user",
        "starts_at": "user", "due_at": "user", "all_day": "user", "fields": "user", "rev": "hub", "deleted_at": "hub",
    },
    "relation/relation": {"id": "client", "from": "client", "type": "client", "to": "client", "locators": "hub"},
    "tag/tag": {"id": "client", "name": "user", "color": "user"},
}


def owners_of(schema_id):
    properties = read_strict(SCHEMAS / f"{schema_id}.json").get("properties", {})
    return {name: member["x-owner"] for name, member in properties.items() if "x-owner" in member}


REFUSED = {"not": {}}


def test_every_entity_column_names_its_owner():
    """A column without x-owner would otherwise be patched or refused silently by rule owner."""
    for schema_id in OWNERS:
        properties = read_strict(SCHEMAS / f"{schema_id}.json")["properties"]
        unmarked = {
            name for name, member in properties.items()
            if "x-owner" not in member and {k: v for k, v in member.items() if k != "description"} != REFUSED
        }
        assert unmarked == set(), schema_id


OPEN_NAME = "^[a-z][a-z_]*$"


def test_open_enums_name_their_known_values():
    """x-open marks a string the Hub may send new values of; set_type lets a patch write only its examples.

    Every open enum takes the same names, lowercase letters and underscores starting with a letter, so a
    harness can decode all of them with one rule.
    """
    def walk(node, at):
        if isinstance(node, dict):
            if node.get("x-open") is True:
                yield at, node
            for key, value in node.items():
                yield from walk(value, f"{at}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, f"{at}/{index}")

    for path in SCHEMAS.rglob("*.json"):
        for at, node in walk(read_strict(path), ""):
            assert node.get("type") == "string" and node.get("pattern") == OPEN_NAME, f"{path.name}#{at}"
            assert node.get("examples") and all(isinstance(value, str) for value in node["examples"]), f"{path.name}#{at}"


def test_ownership_is_the_contract_table():
    marked = {
        path.relative_to(SCHEMAS).with_suffix("").as_posix()
        for path in SCHEMAS.rglob("*.json")
        if owners_of(path.relative_to(SCHEMAS).with_suffix("").as_posix())
    }
    assert marked == set(OWNERS)
    assert {schema_id: owners_of(schema_id) for schema_id in OWNERS} == OWNERS


def test_pairing_takes_the_client_platforms():
    """windows_hub is the Hub's own Device row and never pairs over the sync port."""
    platforms = read_strict(SCHEMAS / "device/platform.json")["enum"]
    paired = read_strict(SCHEMAS / "device/pair_request.json")["properties"]["platform"]["enum"]
    assert paired == [platform for platform in platforms if platform != "windows_hub"]
