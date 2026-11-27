"""The contract version this package speaks, which the Hub reports from GET /v1/hub (D17, E18)."""

import campuslife_contracts
from campuslife_contracts import CONTRACT_VERSION, MIN_CONTRACT_VERSION, validate

HUB_ID = "c56a4180-65aa-42ec-a945-5fd21dec0538"
EPOCH = "e4eaaaf2-d142-41ff-9e1f-2b3c4d5e6f70"


def test_the_first_contract_is_version_1_and_accepts_only_itself():
    assert (CONTRACT_VERSION, MIN_CONTRACT_VERSION) == (1, 1)
    assert {"CONTRACT_VERSION", "MIN_CONTRACT_VERSION"} <= set(campuslife_contracts.__all__)


def test_the_versions_make_a_hub_body_the_contract_accepts():
    body = {"hub_id": HUB_ID, "epoch": EPOCH, "contract_version": CONTRACT_VERSION, "min_contract_version": MIN_CONTRACT_VERSION}
    assert validate("sync/hub", body) == []


def test_the_contract_version_is_a_header_value_a_client_can_send():
    assert validate("sync/request_headers", {"CampusLife-Contract": str(CONTRACT_VERSION)}) == []
