"""Devices on the admin listener (#19, D6, D23), which tailscale serve never exposes: the local UI's device pages and
the admin API the CLI uses.

- GET /devices: every paired Device, its name escaped beside the start of its id and when it paired, so names that look
  alike are told apart; the changes its state allows; and adding a device, or how to give serve --public-url first.
- POST /devices/pairing-offer: a new pairing offer as a QR code (device/pairing_offer JSON, an inline SVG the CSP allows)
  and as the manual code. The secret is only in this page's body, never in a URL, and no admin page is cached.
- GET|POST /devices/{id}/revoke and /devices/{id}/approve: a page that asks, then the change. Approving warns that a lost
  device approved syncs again.
- GET /api/devices: every paired Device, revoked ones included, oldest first.
- POST /api/devices/{id}/revoke: revoke a Device for good and end its uploads; its next request is device_revoked.
- POST /api/devices/{id}/approve: make a dormant Device active again with the same id and token.
Each API change answers {id, state, changed}; a Device the Hub does not know is 404, and the Hub's own Device or
approving a revoked one is 409.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from html import escape
from typing import TYPE_CHECKING

import segno
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import devices
from .admin_page import page
from .clock import instant
from .devices import HUB_PLATFORM, DeviceRecord, DeviceState, HubDeviceRefused, NoPublicUrl, PairingOffer, RevokedDevice, UnknownDevice

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


NO_PUBLIC_URL = (
    "No device can be added until serve knows the address devices reach the Hub at: run <code>campuslife-hub tailscale-check</code>, "
    "then start serve, or register the autostart task again, with <code>--public-url</code> and the hub_url it prints."
)
APPROVE_WARNING = "Approve only a device you have in hand: a lost device approved here syncs again with the token it holds. Revoke a lost device instead."


def qr_svg(text: str) -> str:
    """text as a QR code in an inline SVG of attributes only, which a page under the admin CSP may show."""
    return segno.make(text, error="m", micro=False).svg_inline(scale=5, dark="#000", light="#fff")


def manual_code(secret: str) -> str:
    """The secret as the manual code: eight groups of four lowercase hex digits joined by - (D23)."""
    return "-".join(secret[start : start + 4] for start in range(0, len(secret), 4))


def _named(device: DeviceRecord) -> str:
    return f"<strong>{escape(device.device_name)}</strong> <small><code>{escape(device.id[:8])}</code> · paired {escape(device.created_at)}</small>"


def _reported(device: DeviceRecord) -> str:
    status = device.last_status
    if status is None:
        return "-"
    counts = (
        f"{escape(str(status.get('pending_upload_count')))} uploads and {escape(str(status.get('pending_mutation_count')))} changes waiting, "
        f"{escape(str(status.get('open_conflict_count')))} conflicts"
    )
    error = f"<br>{escape(str(status['last_error']))}" if status.get("last_error") else ""
    return f"{escape(device.last_status_at or '')}: {counts}{error}"


def _actions(device: DeviceRecord) -> str:
    links = []
    if device.state == DeviceState.DORMANT:
        links.append(f'<a href="/devices/{escape(device.id)}/approve">Approve</a>')
    if device.state != DeviceState.REVOKED:
        links.append(f'<a href="/devices/{escape(device.id)}/revoke">Revoke</a>')
    return " ".join(links)


def _row(device: DeviceRecord) -> str:
    return (
        f"<tr><td>{_named(device)}</td><td>{escape(device.platform)}</td><td>{escape(device.app_version)}</td><td>{escape(device.state)}</td>"
        f"<td>{escape(device.last_seen_at or '-')}</td><td>{escape(device.last_rejected_at or '-')}</td><td>{_reported(device)}</td>"
        f"<td>{_actions(device)}</td></tr>"
    )


@router.get("/devices")
async def devices_page(request: Request) -> Response:
    admin = _admin(request)
    listed = await admin.core.db.read(devices.client_devices)
    dormant = sum(device.state == DeviceState.DORMANT for device in listed)
    waiting = (
        f'<p class="warning">{dormant} dormant devices wait for approval: approve only a device you have in hand, and revoke one that was lost.</p>'
        if dormant
        else ""
    )
    adding = (
        '<form method="post" action="/devices/pairing-offer"><p><button>Add a device</button></p></form>'
        if admin.device_settings.public_url is not None
        else f'<p class="warning">{NO_PUBLIC_URL}</p>'
    )
    head = "<tr><th>Device</th><th>Platform</th><th>App</th><th>State</th><th>Last seen</th><th>Last refused</th><th>Reported</th><th></th></tr>"
    table = "<table>" + head + "".join(_row(device) for device in listed) + "</table>" if listed else "<p>No devices yet.</p>"
    return page("Devices", waiting + adding + table)


def _offer_page(offer: PairingOffer) -> Response:
    wire = json.dumps(offer.wire(), separators=(",", ":"))
    return page(
        "Add a device",
        "<p>Scan this code with the CampusLife app on the phone or tablet. It pairs one device, once, "
        f"until {escape(instant(offer.expires_at))}.</p>"
        f'<figure>{qr_svg(wire)}<figcaption>Pairing code for {escape(offer.hub_url)}</figcaption></figure>'
        f"<p>Without the camera, type the Hub address <code>{escape(offer.hub_url)}</code> and the code "
        f"<code>{escape(manual_code(offer.secret))}</code> in the app.</p>"
        '<p>Making another code ends this one. <a href="/devices">Back to devices</a></p>',
    )


@router.post("/devices/pairing-offer")
async def pairing_offer_form(request: Request) -> Response:
    admin = _admin(request)
    try:
        offer = await devices.offer(admin.core, now=admin.clock(), public_url=admin.device_settings.public_url, redactor=admin.redactor)
    except NoPublicUrl:
        return page("No device can be added", f"<p>{NO_PUBLIC_URL}</p>", status=409)
    return _offer_page(offer)


async def _client_device(admin: Admin, device_id: str) -> DeviceRecord | None:
    device = await admin.core.db.read(lambda snap: devices.device(snap, device_id))
    return None if device is None or device.platform == HUB_PLATFORM else device


def _not_found() -> Response:
    return page("Not found", "<p>No such device.</p>", status=404)


@router.get("/devices/{device_id}/revoke")
async def revoke_page(request: Request, device_id: str) -> Response:
    device = await _client_device(_admin(request), device_id)
    if device is None:
        return _not_found()
    if device.state == DeviceState.REVOKED:
        return page("Revoked already", f"<p>{_named(device)} is revoked already.</p>", status=409)
    return page(
        "Revoke a device",
        f"<p>Revoke {_named(device)}?</p><p>It cannot be undone: the device's next request is refused and its unfinished uploads end. "
        "To use it again, pair it as a new device. Other devices and the data are not touched.</p>"
        f'<form method="post" action="/devices/{escape(device.id)}/revoke"><p><button>Revoke</button></p></form>',
    )


@router.get("/devices/{device_id}/approve")
async def approve_page(request: Request, device_id: str) -> Response:
    device = await _client_device(_admin(request), device_id)
    if device is None:
        return _not_found()
    if device.state != DeviceState.DORMANT:
        return page("Not dormant", f"<p>{_named(device)} is {escape(device.state)}; only a dormant device is approved.</p>", status=409)
    return page(
        "Approve a device",
        f"<p>Approve {_named(device)}? It was last seen {escape(device.last_seen_at or 'never')} "
        f"and last refused {escape(device.last_rejected_at or 'never')}.</p><p class=\"warning\">{escape(APPROVE_WARNING)}</p>"
        f'<form method="post" action="/devices/{escape(device.id)}/approve"><p><button>Approve</button></p></form>',
    )


def _form_change(change: Change, done: str) -> Callable[[Request, str], Awaitable[Response]]:
    async def endpoint(request: Request, device_id: str) -> Response:
        admin = _admin(request)
        device = await _client_device(admin, device_id)
        if device is None:
            return _not_found()
        try:
            changed = await _changed(admin, change, device_id)
        except RevokedDevice as error:
            return page("Not changed", f"<p>{escape(str(error))}</p>", status=409)
        if not changed:
            return page("Not changed", f"<p>{_named(device)} was {escape(device.state)} already; nothing changed.</p>", status=409)
        return page(done.capitalize(), f'<p>{_named(device)} is {done}.</p><p><a href="/devices">Back to devices</a></p>')

    return endpoint


for action, change, state in (("revoke", devices.revoke, DeviceState.REVOKED), ("approve", devices.approve, DeviceState.ACTIVE)):
    router.add_api_route(f"/api/devices/{{device_id}}/{action}", _api_change(change, state), methods=["POST"])
    router.add_api_route(f"/devices/{{device_id}}/{action}", _form_change(change, f"{action}d"), methods=["POST"])
