"""Runs every golden fixture under fixtures/ (E7, E8)."""

import pytest

from campuslife_contracts import bundled_contract, contract_root
from campuslife_contracts.fixtures import FixtureKind, case_problems, discover, layout_problems
from campuslife_contracts.policy import OPERATIONS, discover_policy_vectors, policy_vector_problems

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


@pytest.mark.parametrize("vector", VECTORS, ids=[f"policy/{v.operation}/{v.name}" for v in VECTORS])
def test_policy_vector(vector):
    assert policy_vector_problems(vector) == []


def test_every_policy_operation_has_vectors():
    assert {vector.operation for vector in VECTORS} == set(OPERATIONS)


def enum_lists(node):
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list):
            yield node["enum"]
        for value in node.values():
            yield from enum_lists(value)
    elif isinstance(node, list):
        for value in node:
            yield from enum_lists(value)


def test_every_enum_value_appears_where_a_valid_fixture_meets_that_enum(monkeypatch):
    """Harnesses round-trip only valid fixtures, so each enum value must occur in one (E8)."""
    from campuslife_contracts import Contract
    from campuslife_contracts.contract import schema_uri
    from campuslife_contracts.dialect import ContractValidator
    from campuslife_contracts.jsonio import read_strict

    seen: dict[int, list] = {}
    original = ContractValidator.VALIDATORS["enum"]

    def recording_enum(validator, enums, instance, schema):
        if instance in enums:
            seen.setdefault(id(enums), []).append(instance)
        yield from original(validator, enums, instance, schema)

    monkeypatch.setitem(ContractValidator.VALIDATORS, "enum", recording_enum)
    contract = Contract.load(ROOT)
    for case in CASES:
        if case.kind is FixtureKind.VALID:
            contract.validate(case.schema_id, read_strict(case.path))
    missing = {}
    for schema_id in sorted(contract.schema_ids):
        for enum in enum_lists(contract.registry.contents(schema_uri(schema_id))):
            unseen = [value for value in enum if value not in seen.get(id(enum), [])]
            if unseen:
                missing.setdefault(schema_id, []).extend(unseen)
    assert missing == {}
