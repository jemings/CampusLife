"""The sync listener's envelope (#4): what every request and response of the sync port passes through.

`SyncEnvelope` wraps the sync app from outside, so it also covers what the app's own middleware cannot reach: the
router's 404 and 405 and an exception nothing handled. It stamps every response with CampusLife-Hub-Id and
CampusLife-Epoch (E18) and judges each request in this order (#4 decision 3):

1. CampusLife-Contract missing, repeated or malformed: 400 invalid_request;
2. on every path but the public operations, known or not, a bearer token authenticate refuses: 401 with its code;
3. on every operation but GET /v1/hub, a contract version below the minimum: 400 contract_too_old;
4. on GET /v1/changes and POST /v1/mutations, CampusLife-Epoch missing, repeated or malformed: 400 invalid_request,
   and an epoch other than the Hub's: 409 epoch_changed with the Hub's as details.epoch (D24);
5. the body, which a route reads through read_json and judge: 400 not_canonical when NFC is all it breaks, else
   invalid_request;
6. an exception nothing handled: 500 internal_error, with nothing about it in the body.

Every refusal is a common/error body. Routes learn the Device only through device_of(request), never the token.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from campuslife_contracts import CONTRACT_VERSION, MIN_CONTRACT_VERSION, Rule, RuleViolation, validate
from campuslife_contracts.jsonio import NotStrictJson, loads_strict
from campuslife_contracts.violations import describe
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .devices import DeviceId, Refusal, RefusalCode
from .identity import HubIdentity

log = logging.getLogger(__name__)

type Authenticate = Callable[[str | None], Awaitable[DeviceId | Refusal]]
"""The bearer token of a request, None when it sends none, to the Device it names or why it names none (#19)."""

PUBLIC_OPERATIONS = frozenset({("POST", "/v1/pair")})
"""The operations sync.yaml marks `security: []`. Every other request is authenticated, whether or not a route
answers its path (fail-closed)."""
ANY_CONTRACT_OPERATIONS = frozenset({("GET", "/v1/hub")})
"""The operations that answer a client of any contract version, so a client too old can read min_contract_version."""
EPOCH_OPERATIONS = frozenset({("GET", "/v1/changes"), ("POST", "/v1/mutations")})
"""The operations sync.yaml gives CampusLife-Epoch: a cursor and a kept Mutation mean something only in the epoch they
were made in, so a request of another epoch is refused before its body is read (D24)."""
MAX_JSON_BODY = 8 * 1024 * 1024
DEVICE_SCOPE_KEY = "campuslife.device"
JSON_MEDIA_TYPE = "application/json"


class ErrorCode(enum.StrEnum):
    """The common/error codes the sync port sends besides a Refusal's."""

    INVALID_REQUEST = "invalid_request"
    NOT_CANONICAL = "not_canonical"
    CONTRACT_TOO_OLD = "contract_too_old"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"
    CHUNK_MISMATCH = "chunk_mismatch"
    INCOMPLETE = "incomplete"
    HASH_MISMATCH = "hash_mismatch"
    HUB_STORAGE_FULL = "hub_storage_full"
    EPOCH_CHANGED = "epoch_changed"


@dataclass(frozen=True)
class ContractVersions:
    """The contract versions the Hub speaks and still accepts, as GET /v1/hub reports them (E18)."""

    current: int = CONTRACT_VERSION
    minimum: int = MIN_CONTRACT_VERSION


class SyncError(Exception):
    """A request the sync port refuses; it is answered with a common/error body."""

    def __init__(
        self, status: int, code: ErrorCode | RefusalCode, message: str, *,
        details: Mapping[str, object] | None = None, headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers

    def response(self) -> JSONResponse:
        body: dict[str, object] = {"code": str(self.code), "message": self.message}
        if self.details is not None:
            body["details"] = dict(self.details)
        return JSONResponse(body, status_code=self.status, headers=dict(self.headers or {}))


def internal_error() -> SyncError:
    return SyncError(500, ErrorCode.INTERNAL_ERROR, "the Hub failed to answer; try again later")


def unauthorized(refusal: Refusal) -> SyncError:
    headers = {"WWW-Authenticate": "Bearer"}
    if refusal.retry_after is not None:
        headers["Retry-After"] = str(refusal.retry_after)
    return SyncError(401, refusal.code, f"the bearer token names no Device that may sync: {refusal.code}", headers=headers)


def bearer_token(headers: Headers) -> str | None:
    """The token of the one `Authorization: Bearer <token>` the request sends, else None."""
    values = headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, _, token = values[0].partition(" ")
    return (token.strip() or None) if scheme.lower() == "bearer" else None


def device_of(request: Request) -> DeviceId:
    """The Device the envelope authenticated this request as; a public operation has none."""
    return request.scope[DEVICE_SCOPE_KEY]


def query_of(request: Request) -> dict[str, str]:
    """The query parameters as they arrived, each sent once."""
    pairs = request.query_params.multi_items()
    query = dict(pairs)
    if len(query) != len(pairs):
        raise SyncError(400, ErrorCode.INVALID_REQUEST, "send each query parameter once")
    return query


async def read_capped(request: Request, cap: int) -> bytes | None:
    """The whole body, or None once it passes cap bytes, having read no further."""
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > cap:
                return None
    except ClientDisconnect:
        raise SyncError(400, ErrorCode.INVALID_REQUEST, "the connection closed before the body ended") from None
    return bytes(body)


async def read_json(request: Request, *, cap: int = MAX_JSON_BODY) -> object:
    """The body as strict JSON (UTF-8, no duplicate members), sent as application/json and at most cap bytes."""
    media = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media != JSON_MEDIA_TYPE:
        raise SyncError(400, ErrorCode.INVALID_REQUEST, f"send the body as Content-Type: {JSON_MEDIA_TYPE}")
    body = await read_capped(request, cap)
    if body is None:
        raise SyncError(400, ErrorCode.INVALID_REQUEST, f"this JSON body carries at most {cap} bytes")
    try:
        return loads_strict(body.decode("utf-8"))
    except (UnicodeDecodeError, NotStrictJson) as error:
        raise SyncError(400, ErrorCode.INVALID_REQUEST, f"the body is not strict UTF-8 JSON: {error}") from None


def judge(schema_id: str, instance: object) -> None:
    """Refuse an instance the contract schema does not accept: not_canonical when NFC is all it breaks (U2)."""
    violations = validate(schema_id, instance)
    if not violations:
        return
    if all(isinstance(violation, RuleViolation) and violation.rule is Rule.NFC for violation in violations):
        raise SyncError(400, ErrorCode.NOT_CANONICAL, "every string is sent in NFC: " + "; ".join(map(describe, violations)))
    raise SyncError(400, ErrorCode.INVALID_REQUEST, f"not a {schema_id}: " + "; ".join(map(describe, violations)))


async def _sync_error(request: Request, error: SyncError) -> JSONResponse:
    return error.response()


async def _not_found(request: Request, error: HTTPException) -> JSONResponse:
    return SyncError(404, ErrorCode.NOT_FOUND, "the sync port has no such path").response()


async def _wrong_method(request: Request, error: HTTPException) -> JSONResponse:
    return SyncError(405, ErrorCode.INVALID_REQUEST, f"this path does not take {request.method}", headers=error.headers).response()


async def _internal_error(request: Request, error: Exception) -> JSONResponse:
    return internal_error().response()


ERROR_HANDLERS = {SyncError: _sync_error, 404: _not_found, 405: _wrong_method, Exception: _internal_error}
"""The sync app's handlers: a route's SyncError, the router's 404 and 405 and an unhandled exception each become a
common/error body. Starlette sends the Exception one, then raises the exception on to SyncEnvelope, which logs it."""


class SyncEnvelope:
    def __init__(self, app: ASGIApp, *, identity: HubIdentity, authenticate: Authenticate, versions: ContractVersions) -> None:
        self.app = app
        self.stamp = {"CampusLife-Hub-Id": identity.hub_id, "CampusLife-Epoch": identity.epoch}
        self.epoch = identity.epoch
        self.authenticate = authenticate
        self.versions = versions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def stamped(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in self.stamp.items():
                    headers[name] = value
            await send(message)

        try:
            refused = await self._admit(scope)
            if refused is not None:
                await refused.response()(scope, receive, stamped)
                return
            await self.app(scope, receive, stamped)
        except Exception:
            log.exception("the sync listener failed to answer %s %s", scope["method"], scope["path"])
            if not started:
                await internal_error().response()(scope, receive, stamped)

    async def _admit(self, scope: Scope) -> SyncError | None:
        headers = Headers(scope=scope)
        operation = (scope["method"], scope["path"])
        contract = headers.getlist("campuslife-contract")
        if len(contract) != 1 or validate("sync/request_headers", {"CampusLife-Contract": contract[0]}):
            return SyncError(400, ErrorCode.INVALID_REQUEST, "send CampusLife-Contract once: the client's contract version, a whole number from 1")
        if operation not in PUBLIC_OPERATIONS:
            admitted = await self.authenticate(bearer_token(headers))
            if isinstance(admitted, Refusal):
                return unauthorized(admitted)
            scope[DEVICE_SCOPE_KEY] = admitted
        if int(contract[0]) < self.versions.minimum and operation not in ANY_CONTRACT_OPERATIONS:
            return SyncError(
                400, ErrorCode.CONTRACT_TOO_OLD,
                f"this Hub accepts contract versions {self.versions.minimum} to {self.versions.current}; update the app",
            )
        if operation in EPOCH_OPERATIONS:
            return self._judge_epoch(contract[0], headers)
        return None

    def _judge_epoch(self, contract: str, headers: Headers) -> SyncError | None:
        epoch = headers.getlist("campuslife-epoch")
        if len(epoch) != 1 or validate("sync/request_headers", {"CampusLife-Contract": contract, "CampusLife-Epoch": epoch[0]}):
            return SyncError(
                400, ErrorCode.INVALID_REQUEST, "send CampusLife-Epoch once: the Hub epoch this client's cache, cursor and kept Mutations belong to"
            )
        if epoch[0] != self.epoch:
            return SyncError(
                409, ErrorCode.EPOCH_CHANGED,
                f"this Hub's epoch is {self.epoch}: empty the cache, pull from since=0 and send the kept Mutations again",
                details={"epoch": self.epoch},
            )
        return None
