"""Devices on the admin listener (#19, D6, D23), which tailscale serve never exposes: the admin API the CLI uses.

- GET /api/devices: every paired Device, revoked ones included, oldest first.
- POST /api/devices/{id}/revoke: revoke a Device for good and end its uploads; its next request is device_revoked.
- POST /api/devices/{id}/approve: make a dormant Device active again with the same id and token.
Each change answers {id, state, changed}; a Device the Hub does not know is 404, and the Hub's own Device or approving a
revoked one is 409.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import devices
from .devices import DeviceState, HubDeviceRefused, RevokedDevice, UnknownDevice

if TYPE_CHECKING:
    from .admin import Admin

router = APIRouter()

type Change = Callable[..., Awaitable[bool]]


def _admin(request: Request) -> Admin:
    return request.app.state.admin


@router.get("/api/devices")
async def api_devices(request: Request) -> JSONResponse:
    listed = await _admin(request).core.db.read(devices.client_devices)
    return JSONResponse({"devices": [asdict(record) for record in listed]})


async def _changed(admin: Admin, change: Change, device_id: str) -> bool:
    """Whether change changed the Device; UnknownDevice, HubDeviceRefused and RevokedDevice say why it could not."""
    return await change(admin.core, device_id, now=admin.clock())


def _api_change(change: Change, state: DeviceState) -> Callable[[Request, str], Awaitable[JSONResponse]]:
    async def endpoint(request: Request, device_id: str) -> JSONResponse:
        try:
            changed = await _changed(_admin(request), change, device_id)
        except UnknownDevice as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except (HubDeviceRefused, RevokedDevice) as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        return JSONResponse({"id": device_id, "state": state, "changed": changed})

    return endpoint


for action, change, state in (("revoke", devices.revoke, DeviceState.REVOKED), ("approve", devices.approve, DeviceState.ACTIVE)):
    router.add_api_route(f"/api/devices/{{device_id}}/{action}", _api_change(change, state), methods=["POST"])
