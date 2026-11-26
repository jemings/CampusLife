import json

import pytest

from campuslife_contracts import Contract, ContractError

from conftest import SCHEMA_BASE, write_schema


def test_schema_id_is_the_path_under_schemas_without_extension(make_contract):
    contract = make_contract({"common/a": {"type": "string"}, "item/b": {"type": "object"}})
    assert contract.schema_ids == frozenset({"common/a", "item/b"})


def test_unknown_schema_id_is_a_programming_error(make_contract):
    contract = make_contract({"common/a": {"type": "string"}})
    with pytest.raises(KeyError):
        contract.validate("common/missing", "x")


def test_id_that_does_not_mirror_the_path_is_rejected(tmp_path):
    write_schema(tmp_path, "common/a", {"$id": f"{SCHEMA_BASE}common/b.json"})
    with pytest.raises(ContractError, match="common/a"):
        Contract.load(tmp_path)


def test_schema_must_declare_draft_2020_12(tmp_path):
    write_schema(tmp_path, "common/a", {"$schema": "http://json-schema.org/draft-07/schema#"})
    with pytest.raises(ContractError, match="2020-12"):
        Contract.load(tmp_path)


def test_relative_ref_between_schema_files_resolves(make_contract):
    contract = make_contract(
        {
            "common/id": {"type": "string", "pattern": "^[a-z]+$"},
            "item/thing": {"type": "object", "properties": {"id": {"$ref": "../common/id.json"}}},
        }
    )
    assert contract.validate("item/thing", {"id": "abc"}) == []
    assert len(contract.validate("item/thing", {"id": "ABC"})) == 1


def test_schema_that_breaks_the_metaschema_is_rejected(tmp_path):
    write_schema(tmp_path, "common/a", {"type": "strin"})
    with pytest.raises(ContractError, match="common/a"):
        Contract.load(tmp_path)


def test_schema_file_must_be_utf8_json(tmp_path):
    path = tmp_path / "schemas" / "common" / "a.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"type": ')
    with pytest.raises(ContractError, match="common/a"):
        Contract.load(tmp_path)


def _object_with(member_schema: dict) -> dict:
    return {"type": "object", "properties": {"inner": {"type": "object", "properties": {"m": member_schema}}}}


@pytest.mark.parametrize("owner", ["hub", "user", "client"])
def test_property_ownership_uses_the_three_owners(tmp_path, owner):
    write_schema(tmp_path, "item/a", _object_with({"type": "string", "x-owner": owner}))
    Contract.load(tmp_path)


def test_unknown_owner_is_rejected_even_deep_inside_a_schema(tmp_path):
    write_schema(tmp_path, "item/a", _object_with({"type": "string", "x-owner": "server"}))
    with pytest.raises(ContractError, match="item/a"):
        Contract.load(tmp_path)


def test_unknown_contract_extension_keyword_is_rejected(tmp_path):
    write_schema(tmp_path, "item/a", _object_with({"type": "string", "x-ownr": "hub"}))
    with pytest.raises(ContractError, match="x-ownr"):
        Contract.load(tmp_path)


def test_property_named_like_an_extension_is_still_a_property(tmp_path):
    write_schema(tmp_path, "item/a", {"type": "object", "properties": {"x-anything": {"type": "string"}}})
    Contract.load(tmp_path)


BRANCH_A = {"type": "object", "properties": {"kind": {"const": "a"}}}
BRANCH_B = {"type": "object", "properties": {"kind": {"const": "b"}}}
DISCRIMINATED = {
    "type": "object",
    "required": ["kind"],
    "properties": {"kind": {"enum": ["a", "b"]}},
    "oneOf": [{"$ref": "a.json"}, {"$ref": "b.json"}],
    "discriminator": {"propertyName": "kind", "mapping": {"a": "a.json", "b": "b.json"}},
}


def _load_union(tmp_path, union):
    write_schema(tmp_path, "t/a", BRANCH_A)
    write_schema(tmp_path, "t/b", BRANCH_B)
    write_schema(tmp_path, "t/u", union)
    return Contract.load(tmp_path)


def test_discriminated_union_maps_every_enum_value_to_one_branch(tmp_path):
    _load_union(tmp_path, DISCRIMINATED)


@pytest.mark.parametrize(
    "broken",
    [
        {**DISCRIMINATED, "required": []},
        {**DISCRIMINATED, "properties": {"kind": {"type": "string"}}},
        {key: value for key, value in DISCRIMINATED.items() if key != "oneOf"},
        {**DISCRIMINATED, "discriminator": {"propertyName": "kind"}},
        {**DISCRIMINATED, "discriminator": {"propertyName": "kind", "mapping": {"a": "a.json"}}},
        {**DISCRIMINATED, "discriminator": {"propertyName": "kind", "mapping": {"a": "a.json", "b": "a.json"}}},
        {**DISCRIMINATED, "discriminator": {"propertyName": "kind", "mapping": {"a": "a.json", "b": "b.json", "c": "b.json"}}},
        {**DISCRIMINATED, "oneOf": [{"$ref": "a.json"}, {"$ref": "b.json", "minProperties": 1}]},
        {key: value for key, value in DISCRIMINATED.items() if key != "type"},
        {**DISCRIMINATED, "oneOf": [{"$ref": "a.json"}, {"$ref": "a.json"}],
         "discriminator": {"propertyName": "kind", "mapping": {"a": "a.json", "b": "a.json"}}},
        {**DISCRIMINATED, "discriminator": "kind"},
        {**DISCRIMINATED, "discriminator": {"propertyName": "kind", "mapping": [["a", "a.json"]]}},
        {**DISCRIMINATED, "discriminator": {"propertyName": ["kind"], "mapping": {"a": "a.json", "b": "b.json"}}},
        {**DISCRIMINATED, "oneOf": {"$ref": "a.json"}},
        {**DISCRIMINATED, "properties": {"kind": {"enum": "ab"}}},
        {**DISCRIMINATED, "discriminator": {"propertyName": "kind", "mapping": {"a": 1, "b": "b.json"}}},
        {**DISCRIMINATED, "discriminator": {"propertyName": "kind", "mapping": {"a": ["a.json"], "b": "b.json"}}},
        {**DISCRIMINATED, "discriminator": {"propertyName": "kind", "mapping": {"a": None, "b": "b.json"}}},
        {**DISCRIMINATED, "oneOf": [{"$ref": 1}, {"$ref": "b.json"}]},
        {**DISCRIMINATED, "oneOf": [{"$ref": ["a.json"]}, {"$ref": "b.json"}]},
        {**DISCRIMINATED, "properties": {"kind": {"enum": [["a"], "b"]}}},
        {**DISCRIMINATED, "properties": {"kind": {"enum": [{"x": 1}, "b"]}}},
    ],
    ids=["not-required", "not-enumerated", "no-oneOf", "no-mapping", "enum-value-unmapped",
         "two-values-one-branch", "mapping-beyond-enum", "branch-not-a-bare-ref", "not-object-typed",
         "duplicate-branch-and-mapping", "discriminator-is-a-string", "mapping-is-an-array",
         "property-name-is-an-array", "oneOf-is-an-object", "enum-is-a-string",
         "mapping-value-number", "mapping-value-array", "mapping-value-null", "ref-number", "ref-array",
         "enum-value-array", "enum-value-object"],
)
def test_discriminator_that_could_hide_a_union_error_is_rejected(tmp_path, broken):
    with pytest.raises(ContractError, match="t/u"):
        _load_union(tmp_path, broken)


def test_pattern_properties_are_not_part_of_the_vocabulary(tmp_path):
    write_schema(tmp_path, "t/p", {"type": "object", "patternProperties": {"^[a-z]+$": {}}})
    with pytest.raises(ContractError, match="patternProperties"):
        Contract.load(tmp_path)


@pytest.mark.parametrize("value", [False, "yes", 1])
def test_text_marker_is_only_true(tmp_path, value):
    write_schema(tmp_path, "item/a", _object_with({"type": "string", "x-text": value}))
    with pytest.raises(ContractError, match="item/a"):
        Contract.load(tmp_path)


def test_vocabulary_names_every_rule_keyword():
    from campuslife_contracts.rules import RULE_KEYWORDS
    from campuslife_contracts.vocabulary import VOCABULARY

    extension_keywords = set(VOCABULARY["propertyNames"]["then"]["enum"])
    assert extension_keywords == {"x-owner", "x-text", *RULE_KEYWORDS}
