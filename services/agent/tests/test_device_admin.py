"""Devices on the admin listener (#19, D6, D23): the admin API the CLI uses and the local UI's device pages, over the
admin app as test_admin serves it."""

import json
import re
from datetime import timedelta

import pytest
import segno
from test_admin import ORIGIN, SIGNED, Clock, hub, log_in, submitted

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


async def test_the_device_page_shows_each_name_escaped_beside_the_start_of_its_id_and_when_it_paired(tmp_path):
    """Two names that look alike are told apart by the id and pairing time beside them (one_line lets ZWJ through)."""
    async with hub(tmp_path, clock=Clock(), **WITH_URL) as (client, admin):
        await log_in(client)
        tricky = await paired(admin, "<script>alert(1)</script>")
        same, other = await paired(admin, "Fold8"), await paired(admin, "Fold‍8")
        shown = (await client.get("/devices")).text

    assert "<script>alert" not in shown and "&lt;script&gt;alert(1)&lt;/script&gt;" in shown
    assert all(f"<code>{device.device_id[:8]}</code>" in shown for device in (tricky, same, other))
    assert "paired 2026-03-02T00:30:00.000Z" in shown
    assert f"<code>{admin.core.hub_device[:8]}</code>" not in shown
    assert '<a href="/devices">Devices</a>' in shown


async def test_each_device_on_the_page_offers_only_the_changes_its_state_allows(tmp_path):
    async with hub(tmp_path, **WITH_URL) as (client, admin):
        await log_in(client)
        dormant = await paired(admin)
        await made_dormant(admin, dormant.device_id)
        active, revoked = await paired(admin), await paired(admin)
        await devices.revoke(admin.core, revoked.device_id, now=admin.clock())
        shown = (await client.get("/devices")).text

    links = set(re.findall(r'href="/devices/([0-9a-f-]+)/(revoke|approve)"', shown))
    assert links == {(dormant.device_id, "revoke"), (dormant.device_id, "approve"), (active.device_id, "revoke")}
    assert "approve only a device you have in hand" in shown


def offer_json(admin, secret):
    return json.dumps({"hub_url": PUBLIC_URL, "hub_id": admin.core.identity.hub_id, "secret": secret}, separators=(",", ":"))


async def test_adding_a_device_shows_the_pairing_offer_as_a_qr_code_and_a_manual_code_that_pair(tmp_path):
    async with hub(tmp_path, clock=Clock(), **WITH_URL) as (client, admin):
        await log_in(client)
        _, offered = await submitted(client, "/devices", "/devices/pairing-offer")
        code = MANUAL_CODE.search(offered.text).group(1)
        secret = code.replace("-", "")
        request = PairRequest(secret, ClientPlatform.IPADOS, "iPad", "0.1.0")
        answer = await devices.pair(admin.core, request, now=admin.clock(), limiter=PairingLimiter(), redactor=admin.redactor)

    assert offered.status_code == 200
    assert segno.make(offer_json(admin, secret), error="m", micro=False).svg_inline(scale=5, dark="#000", light="#fff") in offered.text
    assert f"<code>{PUBLIC_URL}</code>" in offered.text and "2026-03-02T00:35:00.000Z" in offered.text
    assert "style=" not in offered.text and "<script" not in offered.text
    assert offered.headers["cache-control"] == "no-store"
    assert isinstance(answer, devices.Paired)


async def test_a_pairing_offer_is_made_only_by_a_request_from_the_local_ui_itself(tmp_path):
    async with hub(tmp_path, **WITH_URL) as (client, admin):
        await log_in(client)
        refused = await client.post("/devices/pairing-offer")
        offers = await admin.core.db.read(lambda snap: snap.execute("SELECT count(*) FROM pairing_secrets").fetchone()[0])

    assert (refused.status_code, offers) == (403, 0)


async def test_without_a_public_url_adding_a_device_says_how_to_set_one_and_makes_no_offer(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        listed = (await client.get("/devices")).text
        refused = await client.post("/devices/pairing-offer", headers=ORIGIN)
        offers = await admin.core.db.read(lambda snap: snap.execute("SELECT count(*) FROM pairing_secrets").fetchone()[0])

    assert 'action="/devices/pairing-offer"' not in listed
    assert "--public-url" in listed and "tailscale-check" in listed
    assert (refused.status_code, "--public-url" in refused.text, offers) == (409, True, 0)


async def test_revoking_from_the_local_ui_asks_first_then_revokes_that_device_only(tmp_path):
    async with hub(tmp_path, **WITH_URL) as (client, admin):
        await log_in(client)
        phone, kept = await paired(admin, "<b>Fold8</b>"), await paired(admin, "iPad", ClientPlatform.IPADOS)

        asked = await client.get(f"/devices/{phone.device_id}/revoke")
        assert await state_of(admin, phone.device_id) == DeviceState.ACTIVE
        _, done = await submitted(client, f"/devices/{phone.device_id}/revoke", f"/devices/{phone.device_id}/revoke")

        assert (asked.status_code, done.status_code) == (200, 200)
        assert "&lt;b&gt;Fold8&lt;/b&gt;" in asked.text
        assert "cannot be undone" in asked.text
        assert (await state_of(admin, phone.device_id), await state_of(admin, kept.device_id)) == (DeviceState.REVOKED, DeviceState.ACTIVE)


async def test_approving_from_the_local_ui_shows_when_the_device_was_last_seen_and_refused_and_warns(tmp_path):
    clock = Clock()
    async with hub(tmp_path, clock=clock, **WITH_URL) as (client, admin):
        phone = await paired(admin)
        assert await devices.authenticate(admin.core, phone.token, now=clock(), redactor=admin.redactor) == phone.device_id
        clock.now += timedelta(days=91)
        assert (await devices.authenticate(admin.core, phone.token, now=clock(), redactor=admin.redactor)).code == "device_dormant"
        await log_in(client)

        asked, done = await submitted(client, f"/devices/{phone.device_id}/approve", f"/devices/{phone.device_id}/approve")

        assert "last seen 2026-03-02T00:30:00.000Z" in asked.text and "last refused 2026-06-01T00:30:00.000Z" in asked.text
        assert "a lost device approved here syncs again" in asked.text
        assert done.status_code == 200
        assert await state_of(admin, phone.device_id) == DeviceState.ACTIVE


@pytest.mark.parametrize("action", ["revoke", "approve"])
async def test_the_local_ui_says_when_a_device_cannot_be_changed(tmp_path, action):
    async with hub(tmp_path, **WITH_URL) as (client, admin):
        await log_in(client)
        revoked = await paired(admin)
        await devices.revoke(admin.core, revoked.device_id, now=admin.clock())
        unknown = "0f8fad5b-d9cb-469f-a165-70867728950e"
        answers = [
            await client.get(f"/devices/{unknown}/{action}"), await client.post(f"/devices/{unknown}/{action}", headers=ORIGIN),
            await client.get(f"/devices/{admin.core.hub_device}/{action}"), await client.post(f"/devices/{admin.core.hub_device}/{action}", headers=ORIGIN),
            await client.get(f"/devices/{revoked.device_id}/{action}"), await client.post(f"/devices/{revoked.device_id}/{action}", headers=ORIGIN),
        ]
        assert await state_of(admin, revoked.device_id) == DeviceState.REVOKED

    assert [answer.status_code for answer in answers] == [404, 404, 404, 404, 409, 409]


async def test_the_local_ui_approves_only_a_dormant_device(tmp_path):
    async with hub(tmp_path, **WITH_URL) as (client, admin):
        await log_in(client)
        phone = await paired(admin)
        asked = await client.get(f"/devices/{phone.device_id}/approve")
        done = await client.post(f"/devices/{phone.device_id}/approve", headers=ORIGIN)

    assert (asked.status_code, done.status_code) == (409, 409)
    assert "only a dormant device is approved" in asked.text and "nothing changed" in done.text
