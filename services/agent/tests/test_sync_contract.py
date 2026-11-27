"""The response check every sync test leans on: it must refuse what openapi/sync.yaml does not allow."""

import httpx
import pytest
from sync_contract import ANY_CONTRACT, PUBLIC, check

from campuslife_hub.sync_envelope import ANY_CONTRACT_OPERATIONS, PUBLIC_OPERATIONS

HUB = {"CampusLife-Hub-Id": "c56a4180-65aa-42ec-a945-5fd21dec0538", "CampusLife-Epoch": "e4eaaaf2-d142-41ff-9e1f-2b3c4d5e6f70"}
HUB_BODY = {"hub_id": HUB["CampusLife-Hub-Id"], "epoch": HUB["CampusLife-Epoch"], "contract_version": 1, "min_contract_version": 1}
ERROR = {"code": "invalid_request", "message": "x"}


def answer(method, path, status, *, headers=HUB, json=None, content=None):
    return httpx.Response(status, headers=headers, json=json, content=content, request=httpx.Request(method, f"http://hub.test{path}"))


def test_a_response_the_document_allows_passes():
    check(answer("GET", "/v1/hub", 200, json=HUB_BODY))
    check(answer("GET", "/v1/hub", 500, json={"code": "internal_error", "message": "x"}))
    check(answer("GET", "/v1/nowhere", 404, json={"code": "not_found", "message": "x"}))
    check(answer("PUT", "/v1/blobs/uploads/" + "ab" * 32 + "/chunks/3", 204))


@pytest.mark.parametrize(
    "response",
    [
        answer("GET", "/v1/hub", 200, headers={}, json=HUB_BODY),
        answer("GET", "/v1/hub", 200, headers={"CampusLife-Hub-Id": HUB["CampusLife-Hub-Id"]}, json=HUB_BODY),
        answer("GET", "/v1/hub", 200, json={**HUB_BODY, "contract_version": 0}),
        answer("GET", "/v1/hub", 409, json=ERROR),
        answer("GET", "/v1/hub", 400, json={"detail": "Not Found"}),
        answer("GET", "/v1/hub", 500, content=b"Internal Server Error", headers={**HUB, "Content-Type": "text/plain"}),
        answer("GET", "/v1/nowhere", 200, json=HUB_BODY),
        answer("GET", "/v1/nowhere", 422, json=ERROR),
        answer("PUT", "/v1/blobs/uploads/" + "ab" * 32 + "/chunks/3", 204, content=b"x"),
    ],
    ids=["no hub headers", "no epoch", "body off schema", "status not listed", "error off schema", "plain 500", "no operation 200",
         "no operation 422", "body on 204"],
)
def test_a_response_the_document_does_not_allow_fails(response):
    with pytest.raises(AssertionError):
        check(response)


def test_the_envelope_s_public_and_any_version_operations_are_the_document_s():
    assert PUBLIC_OPERATIONS == PUBLIC
    assert ANY_CONTRACT_OPERATIONS == ANY_CONTRACT
