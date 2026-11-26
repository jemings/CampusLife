import json
from decimal import Decimal

import pytest

from campuslife_contracts import SchemaViolation, contract_root, validate


def test_lowercase_v4_uuid_is_valid():
    assert validate("common/uuid", "6ba7b810-9dad-41d1-80b4-00c04fd430c8") == []


def test_uppercase_uuid_fails_pattern():
    assert validate("common/uuid", "6BA7B810-9DAD-41D1-80B4-00C04FD430C8") == [
        SchemaViolation(path="", keyword="pattern")
    ]


def test_pattern_dollar_does_not_match_before_a_trailing_newline():
    assert validate("common/uuid", "6ba7b810-9dad-41d1-80b4-00c04fd430c8\n") == [
        SchemaViolation(path="", keyword="pattern")
    ]


def test_pattern_digit_class_is_ascii_only(make_contract):
    contract = make_contract({"t/digit": {"type": "string", "pattern": "^\\d$"}})
    assert contract.validate("t/digit", "7") == []
    assert contract.validate("t/digit", "٣") == [SchemaViolation(path="", keyword="pattern")]


def test_integer_written_with_a_fraction_is_not_an_integer(make_contract):
    contract = make_contract(
        {"t/page": {"type": "object", "properties": {"page": {"type": "integer", "minimum": 1}}}}
    )
    assert contract.validate("t/page", {"page": 2}) == []
    assert contract.validate("t/page", {"page": 2.0}) == [SchemaViolation(path="/page", keyword="type")]


def test_boolean_is_not_an_integer(make_contract):
    contract = make_contract({"t/n": {"type": "integer"}})
    assert contract.validate("t/n", True) == [SchemaViolation(path="", keyword="type")]


CLOSED_PAIR = {
    "type": "object",
    "required": ["a", "b"],
    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    "additionalProperties": False,
}


def test_each_missing_member_is_reported_at_its_own_path(make_contract):
    contract = make_contract({"t/pair": CLOSED_PAIR})
    assert contract.validate("t/pair", {}) == [
        SchemaViolation(path="/a", keyword="required"),
        SchemaViolation(path="/b", keyword="required"),
    ]


def test_each_unexpected_member_is_reported_at_its_own_path(make_contract):
    contract = make_contract({"t/pair": CLOSED_PAIR})
    instance = {"a": "1", "b": "2", "x": 1, "y/z": 2}
    assert contract.validate("t/pair", instance) == [
        SchemaViolation(path="/x", keyword="additionalProperties"),
        SchemaViolation(path="/y~1z", keyword="additionalProperties"),
    ]


SQUARE = {
    "type": "object",
    "required": ["kind", "side"],
    "properties": {"kind": {"const": "square"}, "side": {"type": "integer", "minimum": 1}},
    "additionalProperties": False,
}
CIRCLE = {
    "type": "object",
    "required": ["kind", "radius"],
    "properties": {"kind": {"const": "circle"}, "radius": {"type": "integer", "minimum": 1}},
    "additionalProperties": False,
}
SHAPE = {
    "type": "object",
    "required": ["kind"],
    "properties": {"kind": {"enum": ["square", "circle"]}},
    "oneOf": [{"$ref": "square.json"}, {"$ref": "circle.json"}],
    "discriminator": {"propertyName": "kind", "mapping": {"square": "square.json", "circle": "circle.json"}},
}


def _shape_union(make_contract, square=SQUARE, circle=CIRCLE):
    return make_contract({"t/square": square, "t/circle": circle, "t/shape": SHAPE})


def test_union_error_is_reported_from_the_branch_the_discriminator_selects(make_contract):
    contract = _shape_union(make_contract)
    assert contract.validate("t/shape", {"kind": "square", "side": 0}) == [
        SchemaViolation(path="/side", keyword="minimum")
    ]
    assert contract.validate("t/shape", {"kind": "circle", "side": 2}) == [
        SchemaViolation(path="/radius", keyword="required"),
        SchemaViolation(path="/side", keyword="additionalProperties"),
    ]


def test_union_without_a_usable_discriminator_reports_only_the_discriminator(make_contract):
    contract = _shape_union(make_contract)
    assert contract.validate("t/shape", {"side": 2}) == [SchemaViolation(path="/kind", keyword="required")]
    assert contract.validate("t/shape", {"kind": "hexagon"}) == [SchemaViolation(path="/kind", keyword="enum")]


def test_valid_union_member_has_no_violations(make_contract):
    assert _shape_union(make_contract).validate("t/shape", {"kind": "circle", "radius": 3}) == []


def test_union_keeps_the_oneof_error_when_the_selected_branch_alone_cannot_explain_it(make_contract):
    contract = _shape_union(make_contract, square={"type": "object"}, circle={"type": "object"})
    assert contract.validate("t/shape", {"kind": "square"}) == [SchemaViolation(path="", keyword="oneOf")]


def test_union_without_discriminator_keeps_the_oneof_error(make_contract):
    contract = make_contract({"t/either": {"oneOf": [{"type": "string"}, {"type": "integer"}]}})
    assert contract.validate("t/either", 1.5) == [SchemaViolation(path="", keyword="oneOf")]


@pytest.mark.parametrize(
    "instance",
    [Decimal("1.5"), {"page": Decimal("1")}, [1, (2, 3)], {1: "a"}, b"x",
     float("nan"), [0, float("inf")], {"x": float("-inf")}],
    ids=["decimal", "nested-decimal", "tuple", "non-string-key", "bytes", "nan", "infinity", "minus-infinity"],
)
def test_instance_must_use_json_loads_types(make_contract, instance):
    contract = make_contract({"t/any": {}})
    with pytest.raises(TypeError):
        contract.validate("t/any", instance)


def test_error_code_catalogue_is_exactly_the_contract_list():
    catalogue = [
        "invalid_request", "not_canonical", "id_reuse", "hash_mismatch", "chunk_mismatch", "incomplete",
        "unauthorized", "device_revoked", "device_dormant", "not_found", "contract_too_old",
    ]
    schema = json.loads((contract_root() / "schemas/common/error.json").read_text(encoding="utf-8"))
    assert schema["properties"]["code"]["enum"] == catalogue
    for code in catalogue:
        details = {"details": {"missing_chunks": [0]}} if code == "incomplete" else {}
        assert validate("common/error", {"code": code, "message": "m", **details}) == []


UUID = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.mark.parametrize("entity", ["item", "attachment", "capture", "artifact", "suggestion", "tag"])
def test_entity_ref_names_each_uuid_entity(entity):
    assert validate("common/entity_ref", f"{entity}:{UUID}") == []
    assert validate("common/entity_ref", f"{entity}:{SHA}") == [SchemaViolation(path="", keyword="pattern")]


@pytest.mark.parametrize(
    "entity", ["relation", "device", "annotation_layer", "conflict_record", "note", "Item", "blob"]
)
def test_entity_ref_rejects_entities_outside_the_list_by_uuid(entity):
    assert validate("common/entity_ref", f"{entity}:{UUID}") == [SchemaViolation(path="", keyword="pattern")]


def test_box_parsed_with_nan_is_refused():
    with pytest.raises(TypeError):
        validate("common/box", json.loads("[NaN, 0, 0, 0]"))


def test_each_member_a_present_member_depends_on_is_reported_where_it_is_missing(make_contract):
    contract = make_contract({"t/file": {"type": "object", "dependentRequired": {"raw": ["text", "kind"], "other": ["x"]}}})
    assert contract.validate("t/file", {"raw": "a", "kind": "k"}) == [
        SchemaViolation(path="/text", keyword="dependentRequired")
    ]
    assert contract.validate("t/file", {"raw": "a"}) == [
        SchemaViolation(path="/kind", keyword="dependentRequired"),
        SchemaViolation(path="/text", keyword="dependentRequired"),
    ]
