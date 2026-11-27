"""The sync listener's app (#4): the surface of openapi/sync.yaml that clients reach through tailscale serve (D5).

`create_sync_app(hosted, authenticate, routers)` makes what the host serves on the sync port: these routes, and those
routers add (#19's pairing and device routes), all behind one SyncEnvelope. The app is FastAPI with its OpenAPI
document, /docs and /redoc turned off and no redirect for a trailing slash (sync.yaml lists no 3xx); bodies are read as they come and judged against the contract schemas, with no
request model beside them (D17).

- GET /v1/hub: the Hub's identity and the contract versions it speaks and accepts (E18).
- GET /v1/changes: one page of the changes feed above a cursor (D3).
- POST /v1/mutations: a batch of 1 to 200 Mutations, applied in order by the Mutation applier (E12). The HTTP layer
  checks only the batch around them; the applier judges each Mutation and answers rejected{schema} inside the 200.
  A Device's batches apply one at a time, so a resent batch waits for the first and gets duplicate (#4 decision 7).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from campuslife_contracts import contract_root
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp

from . import changes, mutations
from .admin import NO_TELEMETRY
from .devices import DeviceId, Refusal, RefusalCode
from .host import Hosted
from .mutations import NoMutationId, UnsupportedOp
from .sync_envelope import (
    ERROR_HANDLERS,
    Authenticate,
    ContractVersions,
    ErrorCode,
    SyncEnvelope,
    SyncError,
    device_of,
    judge,
    query_of,
    read_json,
)

DEFAULT_PAGE_ROWS = 500
"""The rows of a changes page whose request leaves out limit (sync/changes_query)."""

_BATCH = json.loads((contract_root() / "schemas" / "mutation" / "batch_request.json").read_text(encoding="utf-8"))["properties"]["mutations"]
BATCH_SIZES = range(_BATCH["minItems"], _BATCH["maxItems"] + 1)

router = APIRouter()


@dataclass(frozen=True)
class SyncState:
    hosted: Hosted
    versions: ContractVersions
    device_locks: defaultdict[DeviceId, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock))


def _state(request: Request) -> SyncState:
    return request.app.state.sync


@router.get("/v1/hub")
async def get_hub(request: Request) -> JSONResponse:
    state = _state(request)
    identity = state.hosted.core.identity
    return JSONResponse({
        "hub_id": identity.hub_id, "epoch": identity.epoch,
        "contract_version": state.versions.current, "min_contract_version": state.versions.minimum,
    })


@router.get("/v1/changes")
async def get_changes(request: Request) -> JSONResponse:
    query = query_of(request)
    judge("sync/changes_query", query)
    since, limit = int(query["since"]), int(query.get("limit", DEFAULT_PAGE_ROWS))
    return JSONResponse(await _state(request).hosted.core.db.read(lambda snap: changes.page(snap, since, limit)))


def _batch(body: object) -> list[object]:
    match body:
        case {"mutations": list() as batch} if len(body) == 1 and len(batch) in BATCH_SIZES:
            return batch
    raise SyncError(400, ErrorCode.INVALID_REQUEST, f'the body is {{"mutations": [{BATCH_SIZES.start} to {BATCH_SIZES[-1]} Mutations]}}')


@router.post("/v1/mutations")
async def post_mutations(request: Request) -> JSONResponse:
    state = _state(request)
    batch, device = _batch(await read_json(request)), device_of(request)
    async with state.device_locks[device]:
        try:
            results = await mutations.apply(state.hosted.core.db, batch, device)
        except (NoMutationId, UnsupportedOp) as error:
            raise SyncError(400, ErrorCode.INVALID_REQUEST, str(error)) from None
    if any(mutation.get("op") == "capture.commit" and result["result"] == "applied" for mutation, result in zip(batch, results, strict=True)):
        state.hosted.runner.wake()
    return JSONResponse({"results": results})


async def refuse_every_bearer(bearer: str | None) -> Refusal:
    """The authenticator until Devices can pair (#19): no token names a Device, so every request but pairing is 401."""
    return Refusal(RefusalCode.UNAUTHORIZED)


def create_sync_app(
    hosted: Hosted, authenticate: Authenticate, routers: Sequence[APIRouter] = (), *, versions: ContractVersions = ContractVersions()
) -> ASGIApp:
    app = FastAPI(
        openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False, telemetry=NO_TELEMETRY, exception_handlers=ERROR_HANDLERS
    )
    for added in (router, *routers):
        app.include_router(added)
    app.state.sync = SyncState(hosted, versions)
    return SyncEnvelope(app, identity=hosted.core.identity, authenticate=authenticate, versions=versions)
