"""Devices on the sync port (#19): the envelope's authenticate, and the routes a Device pairs and keeps itself by.

- POST /v1/pair, the one public operation: a pairing secret for this Device's id and token (device/pair_response).
  Its body is read up to PAIR_BODY_CAP and judged against device/pair_request before the secret is. A refused secret
  is 401 unauthorized, with Retry-After once failed attempts are rate limited. The Tailscale-User-Login it came with
  goes to the audit log and decides nothing.
- POST /v1/devices/me/token: another token for the Device the request authenticated as (D23). It takes no body. A
  Device revoked after its request was admitted gets 401 device_revoked rather than a token.
- POST /v1/devices/me/status: the Device's diagnostic heartbeat (device/status), kept to show and never acted on.

Offering secrets, revoking and approving are the admin listener's alone; the sync port has no route for them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.types import ASGIApp

from . import devices, sync
from .devices import ClientPlatform, DeviceId, PairingLimiter, PairRequest, Refusal, RefusalCode, RevokedDevice
from .host import Hosted
from .sync_envelope import device_of, judge, read_json, unauthorized

PAIR_BODY_CAP = 4 * 1024
"""A pair request is four short members; the cap keeps what an unauthenticated client can make the Hub read small."""


class DeviceSync:
    """What the sync app needs of Devices: `authenticate` for its envelope and `router` for its routes, over one Hub,
    one clock and the one PairingLimiter of the process."""

    def __init__(self, hosted: Hosted, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC), limiter: PairingLimiter | None = None):
        self._hosted = hosted
        self._clock = clock
        self._limiter = limiter or PairingLimiter()
        self.router = APIRouter()
        self.router.add_api_route("/v1/pair", self._pair, methods=["POST"])
        self.router.add_api_route("/v1/devices/me/token", self._reissue, methods=["POST"])
        self.router.add_api_route("/v1/devices/me/status", self._status, methods=["POST"])

    async def authenticate(self, bearer: str | None) -> DeviceId | Refusal:
        hosted = self._hosted
        return await devices.authenticate(hosted.core, bearer, now=self._clock(), redactor=hosted.redactor, dormant_after=hosted.devices.dormant_after)

    async def _pair(self, request: Request) -> JSONResponse:
        body = await read_json(request, cap=PAIR_BODY_CAP)
        judge("device/pair_request", body)
        pairing = PairRequest(body["secret"], ClientPlatform(body["platform"]), body["device_name"], body["app_version"])
        answer = await devices.pair(
            self._hosted.core, pairing, now=self._clock(), limiter=self._limiter, redactor=self._hosted.redactor,
            tailscale_user=request.headers.get("tailscale-user-login"),
        )
        if isinstance(answer, Refusal):
            raise unauthorized(answer)
        return JSONResponse(answer.wire())

    async def _reissue(self, request: Request) -> JSONResponse:
        try:
            paired = await devices.reissue(self._hosted.core, device_of(request), now=self._clock(), redactor=self._hosted.redactor)
        except RevokedDevice:
            raise unauthorized(Refusal(RefusalCode.DEVICE_REVOKED)) from None
        return JSONResponse(paired.wire())

    async def _status(self, request: Request) -> Response:
        status = await read_json(request)
        judge("device/status", status)
        await devices.heartbeat(self._hosted.core, device_of(request), status, now=self._clock())
        return Response(status_code=204)


def sync_app(hosted: Hosted) -> ASGIApp:
    """The sync app serve runs: #4's routes and the device routes, each request admitted by its Device's token."""
    device_sync = DeviceSync(hosted)
    return sync.create_sync_app(hosted, device_sync.authenticate, [device_sync.router])
