"""contract_headers: HTTP header names are case-insensitive (RFC 9110), contract schemas name them one way."""

import pytest

from campuslife_contracts import contract_headers, validate

HUB = "c56a4180-65aa-42ec-a945-5fd21dec0538"
EPOCH = "e4eaaaf2-d142-41ff-9e1f-2b3c4d5e6f70"


def test_known_names_take_the_contract_spelling_whatever_case_arrives():
    assert contract_headers({"campuslife-hub-id": HUB, "CAMPUSLIFE-EPOCH": EPOCH}) == {
        "CampusLife-Hub-Id": HUB,
        "CampusLife-Epoch": EPOCH,
    }


def test_http2_lowercase_headers_validate_after_respelling():
    lowered = {"campuslife-hub-id": HUB, "campuslife-epoch": EPOCH}
    assert validate("sync/response_headers", lowered) != []
    assert validate("sync/response_headers", contract_headers(lowered)) == []


def test_names_the_contract_does_not_define_pass_unchanged():
    assert contract_headers([("content-type", "application/json"), ("CampusLife-Contract", "3")]) == {
        "content-type": "application/json",
        "CampusLife-Contract": "3",
    }


def test_every_header_schema_name_is_known():
    names = {"CampusLife-Contract", "CampusLife-Hub-Id", "CampusLife-Epoch", "Range", "If-None-Match", "ETag",
             "Accept-Ranges", "Content-Range", "Retry-After"}
    assert contract_headers({name.lower(): "x" for name in names}) == {name: "x" for name in names}


def test_a_known_header_sent_twice_is_refused():
    with pytest.raises(ValueError, match="CampusLife-Epoch"):
        contract_headers([("CampusLife-Epoch", EPOCH), ("campuslife-epoch", EPOCH)])


def test_a_repeated_unknown_header_combines_its_values_in_order():
    assert contract_headers([("Vary", "Accept"), ("vary", "Range")]) == {"Vary": "Accept, Range"}
