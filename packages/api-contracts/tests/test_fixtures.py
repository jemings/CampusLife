"""Runs every golden fixture under fixtures/ (E7, E8)."""

import pytest

from campuslife_contracts import bundled_contract, contract_root
from campuslife_contracts.fixtures import FixtureKind, case_problems, discover, layout_problems

ROOT = contract_root()
CONTRACT = bundled_contract()
CASES = discover(ROOT)


def test_fixture_tree_is_well_formed():
    assert layout_problems(ROOT, CONTRACT) == []


@pytest.mark.parametrize("case", CASES, ids=[f"{c.kind}/{c.schema_id}/{c.name}" for c in CASES])
def test_fixture_case(case):
    assert case_problems(CONTRACT, case) == []


@pytest.mark.parametrize("schema_id", sorted(i for i in CONTRACT.schema_ids if i.startswith("common/")))
def test_every_primitive_has_valid_and_invalid_fixtures(schema_id):
    kinds = {case.kind for case in CASES if case.schema_id == schema_id}
    assert {FixtureKind.VALID, FixtureKind.INVALID} <= kinds
