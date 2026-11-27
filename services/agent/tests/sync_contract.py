"""The sync listener's responses checked against openapi/sync.yaml (#4): one place for the status, the headers and the body.

`check` takes any response of the sync listener. For an operation of the document, the status must be one it lists, or
500 (any operation may fail, sync.yaml info), and the body and every header the document names for that status must
match their contract schemas. A request no operation answers (unknown path or method) gets a contract error. Every
response, whatever its status, carries CampusLife-Hub-Id and CampusLife-Epoch (sync/response_headers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
import yaml
from campuslife_contracts import bundled_contract, contract_headers, contract_root, validate
from campuslife_contracts.contract import schema_uri
from campuslife_contracts.dialect import ContractValidator
from campuslife_contracts.jsonio import loads_strict

DOCUMENT = contract_root() / "openapi" / "sync.yaml"
SPEC = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))
METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
JSON = "application/json"
ERROR = "common/error"
NO_OPERATION_STATUSES = {"400", "401", "404", "405", "500"}
"""What a request no operation answers can get: the envelope's refusals, then the router's not_found or a wrong method."""


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    pattern: re.Pattern
    spec: dict


def _deref(node: dict) -> dict:
    ref = node.get("$ref")
    if ref is None:
        return node
    assert ref.startswith("#/"), ref
    found = SPEC
    for part in ref.removeprefix("#/").split("/"):
        found = found[part]
    return found


def _schema_id(ref: str) -> tuple[str, str]:
    """The contract schema id and JSON Pointer a document $ref names."""
    target, _, pointer = ref.partition("#")
    return target.removeprefix("../schemas/").removesuffix(".json"), pointer


OPERATIONS = [
    Operation(method.upper(), path, re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$"), operation)
    for path, item in SPEC["paths"].items()
    for method, operation in item.items()
    if method in METHODS
]
PUBLIC = frozenset((op.method, op.path) for op in OPERATIONS if op.spec.get("security") == [])
ANY_CONTRACT = frozenset(
    (op.method, op.path) for op in OPERATIONS if op.spec["responses"]["400"] == {"$ref": "#/components/responses/BadRequestAnyContract"}
)


def operation_of(method: str, path: str) -> Operation | None:
    return next((op for op in OPERATIONS if op.method == method and op.pattern.match(path)), None)


def _violations(schema_id: str, pointer: str, instance: object) -> list:
    if not pointer:
        return validate(schema_id, instance)
    validator = ContractValidator({"$ref": schema_uri(schema_id) + "#" + pointer}, registry=bundled_contract().registry)
    return [error.message for error in validator.iter_errors(instance)]


def _json_body(response: httpx.Response) -> object:
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    assert media == JSON, f"{response.status_code} body is {media!r}, not {JSON}"
    return loads_strict(response.content.decode("utf-8"))


def _check_error(response: httpx.Response) -> object:
    body = _json_body(response)
    assert validate(ERROR, body) == [], (response.status_code, body)
    return body


def check(response: httpx.Response) -> None:
    """Fail unless the response is one sync.yaml allows; see the module docstring."""
    request = response.request
    headers = contract_headers(response.headers.multi_items())
    stamp = {name: headers.get(name) for name in ("CampusLife-Hub-Id", "CampusLife-Epoch") if name in headers}
    assert validate("sync/response_headers", stamp) == [], (request.method, request.url.path, response.status_code, stamp)
    status = str(response.status_code)
    operation = operation_of(request.method, request.url.path)
    if operation is None:
        assert status in NO_OPERATION_STATUSES, f"{request.method} {request.url.path} is no operation, yet answered {status}"
        _check_error(response)
        return
    if status == "500":
        _check_error(response)
        return
    responses = operation.spec["responses"]
    assert status in responses, f"{operation.method} {operation.path} does not answer {status}"
    declared = _deref(responses[status])
    for name, header in declared.get("headers", {}).items():
        header = _deref(header)
        if name not in headers:
            assert not header.get("required", False), f"{operation.method} {operation.path} {status} lacks {name}"
            continue
        schema_id, pointer = _schema_id(header["schema"]["$ref"])
        assert _violations(schema_id, pointer, headers[name]) == [], (name, headers[name])
    content = declared.get("content")
    if content is None:
        assert response.content == b"", f"{operation.method} {operation.path} {status} carries a body"
        return
    ((media, entry),) = content.items()
    if media != JSON:
        assert response.headers.get("content-type", "").split(";")[0].strip().lower() == media
        return
    schema_id, pointer = _schema_id(entry["schema"]["$ref"])
    body = _json_body(response)
    assert _violations(schema_id, pointer, body) == [], (operation.method, operation.path, status, body)


async def checked(response: httpx.Response) -> None:
    """An httpx response hook: read the body and check the response, so no test sees one the contract does not allow."""
    await response.aread()
    check(response)
