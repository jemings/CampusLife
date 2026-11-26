"""Runs every golden fixture under fixtures/ (E7, E8)."""

import pytest

from campuslife_contracts import bundled_contract, contract_root
from campuslife_contracts.fixtures import FixtureKind, case_problems, discover, layout_problems
from campuslife_contracts.policy import OPERATIONS, discover_policy_vectors, policy_vector_problems
from campuslife_contracts.text_hash import discover_text_hash_vectors, text_hash_vector_problems

ROOT = contract_root()
CONTRACT = bundled_contract()
CASES = discover(ROOT)


def test_fixture_tree_is_well_formed():
    assert layout_problems(ROOT, CONTRACT) == []


@pytest.mark.parametrize("case", CASES, ids=[f"{c.kind}/{c.schema_id}/{c.name}" for c in CASES])
def test_fixture_case(case):
    assert case_problems(CONTRACT, case) == []


@pytest.mark.parametrize("schema_id", sorted(CONTRACT.schema_ids))
def test_every_schema_has_valid_and_invalid_fixtures(schema_id):
    kinds = {case.kind for case in CASES if case.schema_id == schema_id}
    assert {FixtureKind.VALID, FixtureKind.INVALID} <= kinds


VECTORS = discover_policy_vectors(ROOT)
TEXT_HASH_VECTORS = discover_text_hash_vectors(ROOT)


@pytest.mark.parametrize("vector", TEXT_HASH_VECTORS, ids=lambda vector: vector.name)
def test_text_hash_vector(vector):
    assert text_hash_vector_problems(vector) == []


def test_text_hash_vectors_exist():
    assert {vector.name for vector in TEXT_HASH_VECTORS} >= {"empty", "lf", "crlf", "cr", "nfd", "nfc", "non_bmp"}



@pytest.mark.parametrize("vector", VECTORS, ids=[f"policy/{v.operation}/{v.name}" for v in VECTORS])
def test_policy_vector(vector):
    assert policy_vector_problems(vector) == []


def test_every_policy_operation_has_vectors():
    assert {vector.operation for vector in VECTORS} == set(OPERATIONS)


VALUE_LISTS = ("enum", "examples")


def value_lists(node):
    """Every enum and examples list in a schema document, with the keyword it belongs to."""
    if isinstance(node, dict):
        for keyword in VALUE_LISTS:
            if isinstance(node.get(keyword), list):
                yield keyword, node[keyword]
        for value in node.values():
            yield from value_lists(value)
    elif isinstance(node, list):
        for value in node:
            yield from value_lists(value)


def test_every_listed_value_appears_in_a_valid_fixture_of_its_own_schema(monkeypatch):
    """Harnesses round-trip only valid fixtures (E8).

    Each enum or examples list must be covered by the valid fixtures of the
    schema that declares it, not by those of another schema that refers to it.
    """
    from campuslife_contracts import Contract
    from campuslife_contracts.contract import schema_uri
    from campuslife_contracts.dialect import ContractValidator
    from campuslife_contracts.jsonio import read_strict

    seen: set[tuple[str, int, object]] = set()
    current = {}

    def recording(keyword):
        original = ContractValidator.VALIDATORS.get(keyword)

        def check(validator, values, instance, schema):
            if isinstance(instance, (str, int, float, bool)) or instance is None:
                seen.add((current["schema_id"], id(values), instance))
            if original is not None:
                yield from original(validator, values, instance, schema)

        return check

    for keyword in VALUE_LISTS:
        monkeypatch.setitem(ContractValidator.VALIDATORS, keyword, recording(keyword))
    contract = Contract.load(ROOT)
    for case in CASES:
        if case.kind is FixtureKind.VALID:
            current["schema_id"] = case.schema_id
            contract.validate(case.schema_id, read_strict(case.path))
    missing = {}
    for schema_id in sorted(contract.schema_ids):
        for keyword, values in value_lists(contract.registry.contents(schema_uri(schema_id))):
            unseen = [value for value in values if (schema_id, id(values), value) not in seen]
            if unseen:
                missing.setdefault(f"{schema_id} {keyword}", []).extend(unseen)
    assert missing == {}
