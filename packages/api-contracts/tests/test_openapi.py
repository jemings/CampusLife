"""openapi/sync.yaml: a valid OpenAPI 3.1 document that only points at the contract's JSON Schemas (D17, E18, E19)."""

from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename

from campuslife_contracts import bundled_contract, contract_root
from campuslife_contracts.jsonio import read_strict

ROOT = contract_root()
DOCUMENT = ROOT / "openapi" / "sync.yaml"
SPEC = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))
METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}

ENDPOINTS = {
    ("post", "/v1/pair"),
    ("post", "/v1/blobs/uploads"),
    ("put", "/v1/blobs/uploads/{sha}/chunks/{i}"),
    ("post", "/v1/blobs/uploads/{sha}/finalize"),
    ("get", "/v1/blobs/{sha}"),
    ("post", "/v1/mutations"),
    ("get", "/v1/changes"),
    ("get", "/v1/hub"),
    ("post", "/v1/devices/me/status"),
    ("post", "/v1/devices/me/token"),
}


def walk(node, at=()):
    yield at, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, (*at, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, (*at, index))


def follow_pointer(document, pointer):
    node = document
    for part in pointer.lstrip("/").split("/") if pointer else ():
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def resolve(ref):
    """The node a $ref of this document names, without the validator's help."""
    target, _, pointer = ref.partition("#")
    if not target:
        return follow_pointer(SPEC, pointer)
    return follow_pointer(read_strict((DOCUMENT.parent / target).resolve()), pointer)


def operations():
    for path, item in SPEC["paths"].items():
        for method, operation in item.items():
            if method in METHODS:
                yield method, path, operation


def deref(node):
    return resolve(node["$ref"]) if isinstance(node, dict) and "$ref" in node else node


def test_the_document_is_valid_openapi_3_1_with_its_references_resolved():
    spec, base_uri = read_from_filename(str(DOCUMENT))
    validate(spec, base_uri=base_uri)
    assert spec["openapi"].startswith("3.1.")


REFS = sorted({node["$ref"] for _, node in walk(SPEC) if isinstance(node, dict) and "$ref" in node})


@pytest.mark.parametrize("ref", REFS)
def test_every_reference_resolves(ref):
    assert isinstance(resolve(ref), dict)


def test_external_references_name_contract_schemas():
    schema_ids = bundled_contract().schema_ids
    for ref in REFS:
        target = ref.partition("#")[0]
        if target:
            assert target.startswith("../schemas/") and target.endswith(".json"), ref
            assert target.removeprefix("../schemas/").removesuffix(".json") in schema_ids, ref


def test_every_schema_in_the_document_is_a_reference_to_a_contract_schema():
    """No wire shape is defined here; the JSON Schemas are the only source (D17)."""
    found = [(at, node) for at, node in walk(SPEC) if at and at[-1] == "schema"]
    assert found
    for at, node in found:
        assert set(node) == {"$ref"} and node["$ref"].startswith("../schemas/"), at
    assert "schemas" not in SPEC.get("components", {})


def test_the_endpoints_are_the_contract_list():
    assert {(method, path) for method, path, _ in operations()} == ENDPOINTS


TOP_LEVEL_KEYS = {"openapi", "jsonSchemaDialect", "info", "servers", "security", "paths", "components"}

# path -> the keys of its path item; no method or $ref can slip an operation in beside them
PATH_ITEM_KEYS = {
    "/v1/pair": {"post"},
    "/v1/blobs/uploads": {"post"},
    "/v1/blobs/uploads/{sha}/chunks/{i}": {"parameters", "put"},
    "/v1/blobs/uploads/{sha}/finalize": {"parameters", "post"},
    "/v1/blobs/{sha}": {"parameters", "get"},
    "/v1/mutations": {"post"},
    "/v1/changes": {"get"},
    "/v1/hub": {"get"},
    "/v1/devices/me/status": {"post"},
    "/v1/devices/me/token": {"post"},
}


def test_the_document_has_exactly_its_top_level_keys():
    """No webhooks or callbacks: the Hub never calls a client (D19), and admin operations stay off this port (D5)."""
    assert set(SPEC) == TOP_LEVEL_KEYS
    assert not [at for at, _ in walk(SPEC) if at and at[-1] in {"webhooks", "callbacks"}]


def test_each_path_item_has_exactly_its_keys():
    assert {path: set(item) for path, item in SPEC["paths"].items()} == PATH_ITEM_KEYS


def test_every_operation_but_pairing_needs_the_device_token():
    assert SPEC["security"] == [{"device": []}]
    assert SPEC["components"]["securitySchemes"]["device"] == {
        **SPEC["components"]["securitySchemes"]["device"], "type": "http", "scheme": "bearer"
    }
    unauthenticated = {(method, path) for method, path, operation in operations() if operation.get("security") == []}
    assert unauthenticated == {("post", "/v1/pair")}


@pytest.mark.parametrize(("method", "path"), sorted(ENDPOINTS))
def test_every_request_sends_the_contract_version(method, path):
    operation = SPEC["paths"][path][method]
    names = {deref(parameter)["name"] for parameter in operation.get("parameters", [])}
    assert "CampusLife-Contract" in names
    assert deref({"$ref": "#/components/parameters/Contract"})["required"] is True


RESPONSES = [
    (method, path, status)
    for method, path, operation in operations()
    for status in operation["responses"]
]


@pytest.mark.parametrize(("method", "path", "status"), RESPONSES)
def test_every_response_carries_the_hub_id_and_epoch(method, path, status):
    response = deref(SPEC["paths"][path][method]["responses"][status])
    headers = {name: deref(header) for name, header in response.get("headers", {}).items()}
    assert headers["CampusLife-Hub-Id"]["required"] is True
    assert headers["CampusLife-Epoch"]["required"] is True


@pytest.mark.parametrize(("method", "path", "status"), [entry for entry in RESPONSES if entry[2].startswith("4")])
def test_every_error_response_is_a_common_error(method, path, status):
    response = deref(SPEC["paths"][path][method]["responses"][status])
    assert response["content"]["application/json"]["schema"] == {"$ref": "../schemas/common/error.json"}


JSON = "application/json"
OCTETS = "application/octet-stream"
ERROR = (JSON, "common/error")
STAMP = {"CampusLife-Hub-Id": "HubId", "CampusLife-Epoch": "Epoch"}
BLOB = {**STAMP, "ETag": "ETag", "Accept-Ranges": "AcceptRanges"}
UPLOAD = "/v1/blobs/uploads"
CHUNK = "/v1/blobs/uploads/{sha}/chunks/{i}"
FINALIZE = "/v1/blobs/uploads/{sha}/finalize"

# (method, path, status) -> (body as (media type, schema id) or None, {header: component})
RESPONSE_TABLE = {
    ("post", "/v1/pair", "200"): ((JSON, "device/pair_response"), STAMP),
    ("post", "/v1/pair", "400"): (ERROR, STAMP),
    ("post", "/v1/pair", "401"): (ERROR, {**STAMP, "Retry-After": "RetryAfter"}),
    ("post", UPLOAD, "200"): ((JSON, "blob/stored"), STAMP),
    ("post", UPLOAD, "201"): ((JSON, "blob/upload_session"), STAMP),
    ("post", UPLOAD, "400"): (ERROR, STAMP),
    ("post", UPLOAD, "401"): (ERROR, STAMP),
    ("put", CHUNK, "204"): (None, STAMP),
    ("put", CHUNK, "400"): (ERROR, STAMP),
    ("put", CHUNK, "401"): (ERROR, STAMP),
    ("put", CHUNK, "404"): (ERROR, STAMP),
    ("put", CHUNK, "409"): (ERROR, STAMP),
    ("post", FINALIZE, "200"): ((JSON, "blob/stored"), STAMP),
    ("post", FINALIZE, "400"): (ERROR, STAMP),
    ("post", FINALIZE, "401"): (ERROR, STAMP),
    ("post", FINALIZE, "404"): (ERROR, STAMP),
    ("post", FINALIZE, "409"): (ERROR, STAMP),
    ("get", "/v1/blobs/{sha}", "200"): ((OCTETS, None), BLOB),
    ("get", "/v1/blobs/{sha}", "206"): ((OCTETS, None), {**BLOB, "Content-Range": "ContentRange"}),
    ("get", "/v1/blobs/{sha}", "304"): (None, {**STAMP, "ETag": "ETag"}),
    ("get", "/v1/blobs/{sha}", "400"): (ERROR, STAMP),
    ("get", "/v1/blobs/{sha}", "401"): (ERROR, STAMP),
    ("get", "/v1/blobs/{sha}", "404"): (ERROR, STAMP),
    ("get", "/v1/blobs/{sha}", "416"): (ERROR, {**STAMP, "Content-Range": "UnsatisfiedRange"}),
    ("post", "/v1/mutations", "200"): ((JSON, "mutation/batch_response"), STAMP),
    ("post", "/v1/mutations", "400"): (ERROR, STAMP),
    ("post", "/v1/mutations", "401"): (ERROR, STAMP),
    ("post", "/v1/mutations", "409"): (ERROR, STAMP),
    ("get", "/v1/changes", "200"): ((JSON, "sync/changes_page"), STAMP),
    ("get", "/v1/changes", "400"): (ERROR, STAMP),
    ("get", "/v1/changes", "401"): (ERROR, STAMP),
    ("get", "/v1/changes", "409"): (ERROR, STAMP),
    ("get", "/v1/hub", "200"): ((JSON, "sync/hub"), STAMP),
    ("get", "/v1/hub", "400"): (ERROR, STAMP),
    ("get", "/v1/hub", "401"): (ERROR, STAMP),
    ("post", "/v1/devices/me/status", "204"): (None, STAMP),
    ("post", "/v1/devices/me/status", "400"): (ERROR, STAMP),
    ("post", "/v1/devices/me/status", "401"): (ERROR, STAMP),
    ("post", "/v1/devices/me/token", "200"): ((JSON, "device/pair_response"), STAMP),
    ("post", "/v1/devices/me/token", "400"): (ERROR, STAMP),
    ("post", "/v1/devices/me/token", "401"): (ERROR, STAMP),
}

# Header component -> (schema it points at, required)
HEADER_COMPONENTS = {
    "HubId": ("sync/response_headers.json#/properties/CampusLife-Hub-Id", True),
    "Epoch": ("sync/response_headers.json#/properties/CampusLife-Epoch", True),
    "ETag": ("blob/download_response_headers.json#/properties/ETag", True),
    "AcceptRanges": ("blob/download_response_headers.json#/properties/Accept-Ranges", True),
    "ContentRange": ("blob/download_response_headers.json#/properties/Content-Range", True),
    "UnsatisfiedRange": ("blob/range_not_satisfiable_headers.json#/properties/Content-Range", True),
    "RetryAfter": ("device/pair_failure_headers.json#/properties/Retry-After", False),
}

CONTRACT_PARAMETER = {"CampusLife-Contract": ("header", True, "sync/request_headers.json#/properties/CampusLife-Contract")}
EPOCH_PARAMETER = {"CampusLife-Epoch": ("header", True, "sync/request_headers.json#/properties/CampusLife-Epoch")}
SHA_PARAMETER = {"sha": ("path", True, "common/sha256.json")}

# (method, path) -> {parameter: (in, required, schema)}, path-level parameters included
PARAMETER_TABLE = {
    ("post", "/v1/pair"): CONTRACT_PARAMETER,
    ("post", UPLOAD): CONTRACT_PARAMETER,
    ("put", CHUNK): {**CONTRACT_PARAMETER, **SHA_PARAMETER, "i": ("path", True, "common/int64.json#/$defs/non_negative")},
    ("post", FINALIZE): {**CONTRACT_PARAMETER, **SHA_PARAMETER},
    ("get", "/v1/blobs/{sha}"): {
        **CONTRACT_PARAMETER, **SHA_PARAMETER,
        "Range": ("header", False, "blob/download_request_headers.json#/properties/Range"),
        "If-None-Match": ("header", False, "blob/download_request_headers.json#/properties/If-None-Match"),
    },
    ("post", "/v1/mutations"): {**CONTRACT_PARAMETER, **EPOCH_PARAMETER},
    ("get", "/v1/changes"): {
        **CONTRACT_PARAMETER, **EPOCH_PARAMETER,
        "since": ("query", True, "sync/changes_query.json#/properties/since"),
        "limit": ("query", False, "sync/changes_query.json#/properties/limit"),
    },
    ("get", "/v1/hub"): CONTRACT_PARAMETER,
    ("post", "/v1/devices/me/status"): CONTRACT_PARAMETER,
    ("post", "/v1/devices/me/token"): CONTRACT_PARAMETER,
}

# (method, path) -> request body as (media type, schema id), or None
REQUEST_TABLE = {
    ("post", "/v1/pair"): (JSON, "device/pair_request"),
    ("post", UPLOAD): (JSON, "blob/upload_request"),
    ("put", CHUNK): (OCTETS, None),
    ("post", FINALIZE): None,
    ("get", "/v1/blobs/{sha}"): None,
    ("post", "/v1/mutations"): (JSON, "mutation/batch_request"),
    ("get", "/v1/changes"): None,
    ("get", "/v1/hub"): None,
    ("post", "/v1/devices/me/status"): (JSON, "device/status"),
    ("post", "/v1/devices/me/token"): None,
}


def schema_ref(schema):
    return schema["$ref"].removeprefix("../schemas/")


def body(content):
    if content is None:
        return None
    ((media, entry),) = content.items()
    return media, schema_ref(entry["schema"]).removesuffix(".json") if "schema" in entry else None


def component_name(ref):
    return ref.removeprefix("#/components/headers/")


def test_each_response_has_exactly_its_body_and_headers():
    found = {}
    for method, path, operation in operations():
        for status, response in operation["responses"].items():
            response = deref(response)
            headers = {name: component_name(header["$ref"]) for name, header in response.get("headers", {}).items()}
            found[(method, path, status)] = (body(response.get("content")), headers)
    assert found == RESPONSE_TABLE


def test_each_header_component_points_at_its_header_schema():
    found = {
        name: (schema_ref(header["schema"]), header.get("required", False))
        for name, header in SPEC["components"]["headers"].items()
    }
    assert found == HEADER_COMPONENTS


def test_each_operation_takes_exactly_its_parameters():
    found = {}
    for method, path, operation in operations():
        parameters = [*SPEC["paths"][path].get("parameters", []), *operation.get("parameters", [])]
        found[(method, path)] = {
            (parameter := deref(entry))["name"]: (parameter["in"], parameter.get("required", False), schema_ref(parameter["schema"]))
            for entry in parameters
        }
    assert found == PARAMETER_TABLE


def test_each_operation_takes_exactly_its_request_body():
    found = {}
    for method, path, operation in operations():
        request = operation.get("requestBody")
        assert request is None or request["required"] is True
        found[(method, path)] = body(request["content"]) if request else None
    assert found == REQUEST_TABLE


@pytest.mark.parametrize(("method", "path"), sorted(ENDPOINTS))
def test_every_operation_answers_400(method, path):
    expected = "BadRequestAnyContract" if (method, path) == ("get", "/v1/hub") else "BadRequest"
    assert SPEC["paths"][path][method]["responses"]["400"] == {"$ref": f"#/components/responses/{expected}"}


def test_only_get_hub_never_answers_contract_too_old():
    """A client too old for the Hub still reads min_contract_version from GET /v1/hub and stops by itself."""
    responses = SPEC["components"]["responses"]
    assert "contract_too_old" in responses["BadRequest"]["description"]
    assert "Never contract_too_old" in responses["BadRequestAnyContract"]["description"]
    assert "GET /v1/hub is the one exception to `contract_too_old`" in " ".join(SPEC["info"]["description"].split())


def test_a_reissued_token_replaces_the_old_one_at_its_first_use():
    """D6: both tokens work until the new one authenticates.

    #19 decision 2: asking again issues another new token and ends the unused one, because a Hub that keeps
    only SHA-256(token) cannot hand the same token out twice; a token ended this way is unknown, not revoked.
    """
    operation = SPEC["paths"]["/v1/devices/me/token"]["post"]
    assert "security" not in operation and "requestBody" not in operation
    description = " ".join(operation["description"].split())
    assert "until the new one first authenticates successfully" in description
    assert "both tokens work" in description
    assert "Asking again returns another new token, and the unused one it replaces stops working" in description
    assert "the client keeps the old token until a new one has worked" in description
    assert "and ends at that moment" in description and "revoked at that moment" not in description
    assert description.endswith("A token ended either way is unknown to the Hub from then on: 401 unauthorized, not device_revoked.")


def prose(text):
    return " ".join(text.split())


def test_open_upload_states_the_session_key_and_its_end():
    """E19, D6, D7: the wording a Hub implementation is held to."""
    description = prose(SPEC["paths"]["/v1/blobs/uploads"]["post"]["description"])
    assert "Keyed by (this Device, sha256)" in description
    assert "another size while the session is open gets 400 invalid_request" in description
    assert "Revoking the Device discards its open sessions" in description


def test_finalize_of_a_stored_blob_answers_200_with_or_without_a_session():
    description = prose(SPEC["paths"]["/v1/blobs/uploads/{sha}/finalize"]["post"]["description"])
    assert "Finalizing a Blob the Hub already stores answers 200 whether or not a session is open" in description


def test_the_bearer_scheme_says_when_a_token_ends():
    description = prose(SPEC["components"]["securitySchemes"]["device"]["description"])
    assert "It does not expire" in description
    assert "revoke ends it" in description
    assert "the new token's first use ends the old one" in description
    assert "another reissue ends a new token not yet used" in description
    assert "same unused new token" not in prose(DOCUMENT.read_text(encoding="utf-8"))


def test_header_names_are_the_header_schemas_property_names():
    """So contract_headers knows every header name the document uses."""
    for method, path, operation in operations():
        for status, response in operation["responses"].items():
            for name, header in deref(response).get("headers", {}).items():
                assert HEADER_COMPONENTS[component_name(header["$ref"])][0].endswith("#/properties/" + name)
        for entry in [*SPEC["paths"][path].get("parameters", []), *operation.get("parameters", [])]:
            parameter = deref(entry)
            if parameter["in"] == "header":
                assert schema_ref(parameter["schema"]).endswith("_headers.json#/properties/" + parameter["name"])


def test_any_operation_can_answer_500_and_a_response_without_the_hub_id_is_no_answer_from_the_hub():
    """#4 decision 4: the status table lists no 500, yet the Hub can fail on any operation, and tailscale serve
    answers 502 for a Hub that is down; a client backs off on both instead of treating them as a refusal."""
    description = prose(SPEC["info"]["description"])
    assert "Any operation can answer 500 internal_error, with the Hub headers." in description
    assert "A response without CampusLife-Hub-Id, such as the 502 tailscale serve sends while the Hub is down" in description
    assert "the Hub was not reached: back off and try again" in description


def test_open_upload_says_a_hub_short_of_space_answers_hub_storage_full():
    """#4 decision 5: a full disk is not a malformed request, so it gets a code of its own a client retries later."""
    description = prose(SPEC["paths"]["/v1/blobs/uploads"]["post"]["description"])
    assert "400 hub_storage_full" in description



def test_post_mutations_says_which_batches_get_400():
    """#4 review: an entry without a mutation_id a result could carry fails the whole batch, as the Hub's applier does."""
    description = prose(SPEC["paths"]["/v1/mutations"]["post"]["description"])
    assert "A Mutation that breaks its schema is rejected{schema} inside a 200." in description
    assert "400 invalid_request is for a body that is not a batch, and for a batch with an entry no result can describe" in description
    assert "one without a UUIDv4 mutation_id, or one whose op this Hub does not apply yet" in description
    assert "Nothing in such a batch is applied: the client fixes or drops that entry and sends the rest again." in description


EPOCH_BOUND = [("get", "/v1/changes"), ("post", "/v1/mutations")]


def test_only_the_feed_and_the_writes_send_the_base_epoch_and_hear_409_epoch_changed():
    """D24 decision 4: what a cursor and an Outbox mean depends on the epoch, so only those two requests name it."""
    for method, path, operation in operations():
        names = {deref(parameter)["name"] for parameter in operation.get("parameters", [])}
        bound = (method, path) in EPOCH_BOUND
        assert ("CampusLife-Epoch" in names) is bound, (method, path)
        assert (operation["responses"].get("409") == {"$ref": "#/components/responses/EpochChanged"}) is bound, (method, path)


def test_epoch_changed_is_answered_before_the_body_and_changes_nothing():
    description = prose(SPEC["components"]["responses"]["EpochChanged"]["description"])
    assert "epoch_changed" in description and "details.epoch" in description
    assert "before it reads the body" in description and "applies nothing" in description
    info = prose(SPEC["info"]["description"])
    assert "GET /v1/changes and POST /v1/mutations also send CampusLife-Epoch" in info
    assert "The CampusLife-Epoch of a response is for diagnosis; a client does not compare it." in info
