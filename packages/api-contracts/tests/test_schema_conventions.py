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
    "suggestion/suggestion": dict.fromkeys(
        ["id", "source_artifact_id", "action", "proposed", "evidence", "fingerprint", "state", "conflicts_with"], HUB),
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
        "evidence_purged_at": "hub",
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


def open_enums():
    """(schema id#pointer, node) for every node marked x-open: true."""
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
            yield f"{path.relative_to(SCHEMAS).with_suffix('').as_posix()}#{at}", node


def test_open_enums_name_their_known_values():
    """x-open marks a string the Hub may send new values of; set_type lets a patch write only its examples.

    Every open enum takes the same names, lowercase letters and underscores starting with a letter, so a
    harness can decode all of them with one rule.
    """
    for location, node in open_enums():
        assert node.get("type") == "string" and node.get("pattern") == OPEN_NAME, location
        assert node.get("examples") and all(isinstance(value, str) for value in node["examples"]), location


OPEN_ENUMS = {
    "artifact/artifact#/properties/artifact_type",
    "artifact/body#/$defs/block/properties/block_type",
    "artifact/extraction#/$defs/warning/properties/code",
    "capture/attachment#/properties/role",
    "capture/origin#/properties/type",
    "common/error#/properties/code",
    "locator/locator#/properties/kind",
    "relation/relation#/properties/type",
    "suggestion/suggestion#/properties/action",
    "sync/changes_page#/$defs/change/properties/entity",
}


def test_the_open_enums_are_the_contract_list():
    """The values D20 lets grow after the first release; every other enum is closed and cannot gain members."""
    assert {location for location, _ in open_enums()} == OPEN_ENUMS


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


def test_the_pairing_offer_carries_what_pairing_and_get_hub_use():
    """The QR code's secret is the one POST /v1/pair takes, and its hub_id the one GET /v1/hub reports (D3, D6)."""
    offer = read_strict(SCHEMAS / "device/pairing_offer.json")["properties"]
    assert offer["secret"]["$ref"] == "pair_request.json#/properties/secret"
    assert offer["hub_id"]["$ref"] == "../sync/hub.json#/properties/hub_id"


def test_the_strings_a_device_reports_are_one_line_text():
    """#19 decision 1: device_name, app_version and last_error share one rule, so none can break the Hub's lines."""
    request = read_strict(SCHEMAS / "device/pair_request.json")["properties"]
    status = read_strict(SCHEMAS / "device/status.json")["properties"]
    assert request["device_name"]["$ref"] == request["app_version"]["$ref"] == "#/$defs/one_line"
    assert status["app_version"]["$ref"] == "pair_request.json#/properties/app_version"
    assert status["last_error"]["$ref"] == "pair_request.json#/$defs/one_line"


ARTIFACT_BODY_SCHEMAS = {"extraction": "artifact/extraction"}
"""The artifact types whose content Blob is JSON in the artifact/body envelope. A rendition's content Blob is the
rendered PDF itself (D12: annotations and pins hold that Blob), so it has no body schema."""


def test_body_schemas_are_the_text_artifact_types():
    artifact_types = read_strict(SCHEMAS / "artifact/artifact.json")["properties"]["artifact_type"]["examples"]
    body_schemas = {
        path.relative_to(SCHEMAS).with_suffix("").as_posix()
        for path in (SCHEMAS / "artifact").glob("*.json")
        if path.stem not in {"artifact", "body"}
    }
    assert body_schemas == set(ARTIFACT_BODY_SCHEMAS.values())
    assert set(ARTIFACT_BODY_SCHEMAS) <= set(artifact_types)
    assert "rendition" in artifact_types and "rendition" not in ARTIFACT_BODY_SCHEMAS
    for artifact_type, schema_id in ARTIFACT_BODY_SCHEMAS.items():
        assert read_strict(SCHEMAS / f"{schema_id}.json")["$ref"] == "body.json", artifact_type

