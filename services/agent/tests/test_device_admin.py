"""Devices on the admin listener (#19, D6, D23): the admin API the CLI uses and the local UI's device pages, over the
admin app as test_admin serves it."""

import json
import re
from datetime import timedelta

import pytest
import segno
from test_admin import ORIGIN, SIGNED, Clock, hub, log_in

from campuslife_hub import audit, devices
from campuslife_hub.audit import AuditEvent
from campuslife_hub.devices import ClientPlatform, DeviceSettings, DeviceState, PairingLimiter, PairRequest

PUBLIC_URL = "https://campus-laptop.tail0a1b2c.ts.net"
WITH_URL = {"device_settings": DeviceSettings(public_url=PUBLIC_URL)}
MANUAL_CODE = re.compile(r"<code>([0-9a-f]{4}(?:-[0-9a-f]{4}){7})</code>")


async def paired(admin, name="Fold8", platform=ClientPlatform.ANDROID):
    offer = await devices.offer(admin.core, now=admin.clock(), public_url=PUBLIC_URL, redactor=admin.redactor)
    request = PairRequest(offer.secret, platform, name, "0.1.0")
    return await devices.pair(admin.core, request, now=admin.clock(), limiter=PairingLimiter(), redactor=admin.redactor)


async def state_of(admin, device_id):
    return (await admin.core.db.read(lambda snap: devices.device(snap, device_id))).state


async def made_dormant(admin, device_id):
    later = admin.clock() + timedelta(days=91)
    assert await devices.mark_dormant(admin.core, now=later) == [device_id]


async def test_the_api_lists_client_devices_oldest_first_without_the_hub_itself(tmp_path):
    clock = Clock()
    async with hub(tmp_path, clock=clock) as (client, admin):
        phone = await paired(admin, "Fold8")
        clock.now += timedelta(minutes=1)
        tablet = await paired(admin, "iPad", ClientPlatform.IPADOS)
        await devices.heartbeat(admin.core, tablet.device_id, {"app_version": "0.2.0", "pending_upload_count": 3, "pending_mutation_count": 0,
                                                               "open_conflict_count": 0}, now=clock())

        listed = (await client.get("/api/devices", auth=SIGNED)).json()["devices"]

    assert [(device["id"], device["platform"], device["device_name"], device["state"]) for device in listed] == [
        (phone.device_id, "android", "Fold8", "active"), (tablet.device_id, "ipados", "iPad", "active"),
    ]
    assert listed[0]["created_at"] == "2026-03-02T00:30:00.000Z"
    assert (listed[1]["app_version"], listed[1]["last_status"]["pending_upload_count"]) == ("0.2.0", 3)
    assert set(listed[0]) == {
        "id", "platform", "device_name", "app_version", "state", "created_at", "last_seen_at", "last_rejected_at", "last_status", "last_status_at",
    }


async def test_the_api_revokes_a_device_once_and_says_whether_it_changed(tmp_path):
    async with hub(tmp_path) as (client, admin):
        phone, kept = await paired(admin), await paired(admin, "iPad", ClientPlatform.IPADOS)

        first = await client.post(f"/api/devices/{phone.device_id}/revoke", auth=SIGNED)
        again = await client.post(f"/api/devices/{phone.device_id}/revoke", auth=SIGNED)

        assert (first.status_code, first.json()) == (200, {"id": phone.device_id, "state": "revoked", "changed": True})
        assert again.json() == {"id": phone.device_id, "state": "revoked", "changed": False}
        assert (await state_of(admin, phone.device_id), await state_of(admin, kept.device_id)) == (DeviceState.REVOKED, DeviceState.ACTIVE)
        revoked = [entry.details for entry in await admin.core.db.read(audit.entries) if entry.event == AuditEvent.REVOKE]
        assert revoked == [{"reason": "user"}]


async def test_the_api_approves_a_dormant_device_and_starts_its_dormant_period_over(tmp_path):
    clock = Clock()
    async with hub(tmp_path, clock=clock) as (client, admin):
        phone = await paired(admin)
        await made_dormant(admin, phone.device_id)
        clock.now += timedelta(days=100)

        first = await client.post(f"/api/devices/{phone.device_id}/approve", auth=SIGNED)
        again = await client.post(f"/api/devices/{phone.device_id}/approve", auth=SIGNED)

        assert first.json() == {"id": phone.device_id, "state": "active", "changed": True}
        assert again.json() == {"id": phone.device_id, "state": "active", "changed": False}
        record = await admin.core.db.read(lambda snap: devices.device(snap, phone.device_id))
        assert record.last_seen_at == "2026-06-10T00:30:00.000Z"
        assert await devices.authenticate(admin.core, phone.token, now=clock(), redactor=admin.redactor) == phone.device_id


@pytest.mark.parametrize("action", ["revoke", "approve"])
async def test_the_api_refuses_a_device_it_does_not_know_or_the_hub_itself(tmp_path, action):
    async with hub(tmp_path) as (client, admin):
        unknown = await client.post(f"/api/devices/0f8fad5b-d9cb-469f-a165-70867728950e/{action}", auth=SIGNED)
        itself = await client.post(f"/api/devices/{admin.core.hub_device}/{action}", auth=SIGNED)

    assert (unknown.status_code, "no device" in unknown.json()["error"]) == (404, True)
    assert (itself.status_code, "Hub's own device" in itself.json()["error"]) == (409, True)


async def test_the_api_refuses_to_approve_a_revoked_device(tmp_path):
    async with hub(tmp_path) as (client, admin):
        phone = await paired(admin)
        await client.post(f"/api/devices/{phone.device_id}/revoke", auth=SIGNED)

        refused = await client.post(f"/api/devices/{phone.device_id}/approve", auth=SIGNED)

    assert (refused.status_code, "pair it again" in refused.json()["error"]) == (409, True)


async def test_the_api_reports_the_device_settings_the_hub_serves_with(tmp_path):
    settings = DeviceSettings(public_url=PUBLIC_URL, dormant_after=timedelta(days=30))
    async with hub(tmp_path, device_settings=settings) as (client, admin):
        status = (await client.get("/api/status", auth=SIGNED)).json()

    assert status["devices"] == {"public_url": PUBLIC_URL, "dormant_after_days": 30}


async def test_the_device_api_takes_only_requests_signed_with_the_admin_token(tmp_path):
    async with hub(tmp_path) as (client, admin):
        phone = await paired(admin)
        answers = [
            await client.get("/api/devices"),
            await client.post(f"/api/devices/{phone.device_id}/revoke", headers={"Authorization": f"Bearer {phone.token}"}),
        ]

        assert [answer.status_code for answer in answers] == [401, 401]
        assert await state_of(admin, phone.device_id) == DeviceState.ACTIVE
