"""Pairing and the device routes over the sync app (#19): POST /v1/pair, POST /v1/devices/me/token and
POST /v1/devices/me/status, and each request's bearer token judged by devices.authenticate. Every response a test
gets has passed sync_contract.check against openapi/sync.yaml."""

import hashlib
import json
import unicodedata
import uuid
from datetime import timedelta

import pytest
from conftest import contract_fixture
from device_bench import UNKNOWN_SECRET, bench
from sync_client import CONTRACT, Waking, client_of

from campuslife_hub import audit, devices, mutations, sync_devices
from campuslife_hub.audit import AuditEvent
from campuslife_hub.host import Hosted


STATUS = {"app_version": "0.1.0", "pending_upload_count": 0, "pending_mutation_count": 0, "open_conflict_count": 0}


def pair_body(offered, **fields):
    return {"secret": offered, "platform": "android", "device_name": "Fold8", "app_version": "0.1.0", **fields}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def tag(name):
    return {"mutation_id": str(uuid.uuid4()), "op": "create", "entity": "tag", "id": str(uuid.uuid4()), "data": {"name": name, "color": "#ff8800"}}


async def uploaded_commit(client, token, data):
    """data uploaded in one chunk by the Device token names, and the camera_photo commit that carries it."""
    digest = hashlib.sha256(data).hexdigest()
    opened = await client.post("/v1/blobs/uploads", json={"sha256": digest, "size": len(data)}, headers=bearer(token))
    assert opened.status_code == 201
    assert (await client.put(f"/v1/blobs/uploads/{digest}/chunks/0", content=data, headers=bearer(token))).status_code == 204
    assert (await client.post(f"/v1/blobs/uploads/{digest}/finalize", headers=bearer(token))).status_code == 200
    capture = contract_fixture("valid", "capture/commit", "camera_photo")
    capture["links"] = []
    capture["attachments"][0]["blob_sha256"] = digest
    return {"mutation_id": str(uuid.uuid4()), "op": "capture.commit", "capture": capture}


async def pair_failures(b):
    return [entry.details for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.PAIR_FAILED]


async def test_a_device_pairs_over_the_sync_port_and_syncs_with_the_token_it_gets(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        offer = await b.offer()

        paired = await client.post("/v1/pair", json=pair_body(offer.secret))

        assert paired.status_code == 200
        record = await b.device(paired.json()["device_id"])
        assert (record.platform, record.device_name) == ("android", "Fold8")
        assert (await client.get("/v1/changes", params={"since": "0"}, headers=bearer(paired.json()["token"]))).status_code == 200


async def test_a_refused_secret_is_401_unauthorized(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        refused = await client.post("/v1/pair", json=pair_body(UNKNOWN_SECRET))

    assert (refused.status_code, refused.json()["code"]) == (401, "unauthorized")
    assert refused.headers["WWW-Authenticate"] == "Bearer"
    assert "Retry-After" not in refused.headers


async def test_an_expired_secret_is_401_unauthorized(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        offer = await b.offer()
        b.clock.advance(minutes=5, seconds=1)

        refused = await client.post("/v1/pair", json=pair_body(offer.secret))

    assert (refused.status_code, refused.json()["code"]) == (401, "unauthorized")


async def test_a_secret_used_twice_is_refused_and_the_device_it_paired_is_revoked(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        offer = await b.offer()
        first = (await client.post("/v1/pair", json=pair_body(offer.secret))).json()

        again = await client.post("/v1/pair", json=pair_body(offer.secret))

        assert (again.status_code, again.json()["code"]) == (401, "unauthorized")
        assert (await changes_with(client, first["token"])).json()["code"] == "device_revoked"


async def test_once_failed_pairings_are_limited_the_refusal_says_when_to_try_again(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        for _ in range(10):
            await client.post("/v1/pair", json=pair_body(UNKNOWN_SECRET))
        b.clock.advance(minutes=4)
        offer = await b.offer()

        limited = await client.post("/v1/pair", json=pair_body(offer.secret))

    assert (limited.status_code, limited.json()["code"], limited.headers["Retry-After"]) == (401, "unauthorized", "360")


@pytest.mark.parametrize(("size", "status"), [(4096, 200), (4097, 400)])
async def test_a_pair_body_is_read_up_to_exactly_4_kib(tmp_path, size, status):
    async with bench(tmp_path) as b, b.sync() as client:
        offer = await b.offer()
        body = json.dumps(pair_body(offer.secret))
        padded = body + " " * (size - len(body.encode()))

        answer = await client.post("/v1/pair", content=padded.encode(), headers={"Content-Type": "application/json"})

    assert (len(padded.encode()), answer.status_code) == (size, status)


async def test_a_pair_body_over_4_kib_is_refused_unread(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        offer = await b.offer()
        padded = json.dumps(pair_body(offer.secret)) + " " * 4096

        refused = await client.post("/v1/pair", content=padded, headers={"Content-Type": "application/json"})

        assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")
        assert await pair_failures(b) == []


async def test_a_device_name_not_in_nfc_is_not_canonical(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        offer = await b.offer()
        refused = await client.post("/v1/pair", json=pair_body(offer.secret, device_name=unicodedata.normalize("NFD", "갤럭시 폴드")))

    assert (refused.status_code, refused.json()["code"]) == (400, "not_canonical")


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"platform": "windows_hub"}, id="the Hub's own platform"),
        pytest.param({"secret": "0000-0000"}, id="a secret not in hex"),
        pytest.param({"app_version": "0.1\n"}, id="an app version on two lines"),
        pytest.param({"owner": "someone"}, id="a member the request does not have"),
    ],
)
async def test_a_pair_request_the_contract_refuses_is_invalid_and_uses_no_secret(tmp_path, fields):
    async with bench(tmp_path) as b, b.sync() as client:
        offer = await b.offer()
        refused = await client.post("/v1/pair", json=pair_body(offer.secret, **fields))

        assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")
        assert (await client.post("/v1/pair", json=pair_body(offer.secret))).status_code == 200


async def test_a_pair_request_without_its_required_members_is_invalid(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        refused = await client.post("/v1/pair", json={"secret": UNKNOWN_SECRET})

    assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")


async def test_the_tailscale_login_a_pairing_comes_with_reaches_the_audit_log(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        offer = await b.offer()
        paired = await client.post("/v1/pair", json=pair_body(offer.secret), headers={"Tailscale-User-Login": "student@example.com"})

        logged = [entry for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.PAIR]
        assert [(entry.subject, entry.details["tailscale_user"]) for entry in logged] == [(paired.json()["device_id"], "student@example.com")]


async def paired_over(client, b):
    offer = await b.offer()
    return (await client.post("/v1/pair", json=pair_body(offer.secret))).json()


async def changes_with(client, token):
    return await client.get("/v1/changes", params={"since": "0"}, headers=bearer(token))


@pytest.mark.parametrize("headers", [pytest.param({}, id="no token"), pytest.param(bearer("A" * 43), id="a token the Hub never issued")])
async def test_a_request_without_a_token_the_hub_issued_is_401_unauthorized(tmp_path, headers):
    async with bench(tmp_path) as b, b.sync() as client:
        await paired_over(client, b)
        refused = await client.get("/v1/changes", params={"since": "0"}, headers=headers)

    assert (refused.status_code, refused.json()["code"]) == (401, "unauthorized")


async def test_a_revoked_devices_token_is_401_device_revoked_and_no_other_devices_is(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        lost, kept = await paired_over(client, b), await paired_over(client, b)
        await b.revoke(lost["device_id"])

        refused = await changes_with(client, lost["token"])

        assert (refused.status_code, refused.json()["code"]) == (401, "device_revoked")
        assert (await changes_with(client, kept["token"])).status_code == 200


async def test_the_dormant_period_the_hub_was_given_decides_when_a_token_is_dormant(tmp_path):
    async with bench(tmp_path, dormant_after=timedelta(days=30)) as b, b.sync() as client:
        paired = await paired_over(client, b)
        b.clock.advance(days=31)

        refused = await changes_with(client, paired["token"])

    assert (refused.status_code, refused.json()["code"]) == (401, "device_dormant")


async def test_a_dormant_device_once_approved_syncs_on_from_its_cursor(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        paired = await paired_over(client, b)
        kept = tag("시험")
        await client.post("/v1/mutations", json={"mutations": [kept]}, headers=bearer(paired["token"]))
        cursor = (await changes_with(client, paired["token"])).json()["next_since"]
        b.clock.advance(days=91)

        refused = await changes_with(client, paired["token"])
        assert (refused.status_code, refused.json()["code"]) == (401, "device_dormant")
        later = tag("과제")
        await mutations.apply(b.core.db, [later], b.core.hub_device)

        await b.approve(paired["device_id"])
        resumed = await client.get("/v1/changes", params={"since": str(cursor)}, headers=bearer(paired["token"]))
        everything = await changes_with(client, paired["token"])

    assert [change["id"] for change in resumed.json()["changes"]] == [later["id"]]
    assert {kept["id"], later["id"]} <= {change["id"] for change in everything.json()["changes"]}


async def test_a_revoked_devices_unsent_capture_comes_again_from_its_new_pairing_without_a_second_capture(tmp_path):
    """The first device's commit was applied but its answer lost, so the capture stayed in its Outbox; the phone is
    revoked, paired again under a new device id, and sends the same mutation again."""
    async with bench(tmp_path) as b, b.sync() as client:
        first = await paired_over(client, b)
        data = b"synthetic whiteboard photo"
        mutation = await uploaded_commit(client, first["token"], data)
        [applied] = (await client.post("/v1/mutations", json={"mutations": [mutation]}, headers=bearer(first["token"]))).json()["results"]
        assert applied["result"] == "applied"
        await b.revoke(first["device_id"])
        again = await paired_over(client, b)

        resent = await client.post("/v1/mutations", json={"mutations": [mutation]}, headers=bearer(again["token"]))
        feed = (await changes_with(client, again["token"])).json()["changes"]

    assert again["device_id"] != first["device_id"]
    assert resent.json()["results"][0] == {"mutation_id": mutation["mutation_id"], "result": "duplicate", "original": applied}
    assert [change["id"] for change in feed if change["entity"] == "capture"] == [mutation["capture"]["id"]]
    assert [change["id"] for change in feed if change["entity"] == "attachment"] == [mutation["capture"]["attachments"][0]["id"]]


async def reissue(client, token):
    return await client.post("/v1/devices/me/token", headers=bearer(token))


async def test_a_reissued_token_works_beside_the_old_one_until_its_first_use_ends_the_old_one(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        paired = await paired_over(client, b)

        reissued = await reissue(client, paired["token"])

        assert reissued.status_code == 200
        assert reissued.json()["device_id"] == paired["device_id"]
        assert (await changes_with(client, paired["token"])).status_code == 200
        assert (await changes_with(client, reissued.json()["token"])).status_code == 200
        ended = await changes_with(client, paired["token"])
        assert (ended.status_code, ended.json()["code"]) == (401, "unauthorized")


async def test_a_reissue_is_audited_at_the_hubs_time(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        paired = await paired_over(client, b)
        b.clock.advance(hours=2)

        assert (await reissue(client, paired["token"])).status_code == 200

        logged = [(entry.at, entry.subject) for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.REISSUE]
        assert logged == [("2026-03-02T11:00:00.000Z", paired["device_id"])]


async def test_asking_again_for_a_token_gives_another_and_the_unused_one_stops_working(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        paired = await paired_over(client, b)
        first = (await reissue(client, paired["token"])).json()["token"]
        second = (await reissue(client, paired["token"])).json()["token"]

        assert len({paired["token"], first, second}) == 3
        assert (await changes_with(client, first)).json()["code"] == "unauthorized"
        assert (await changes_with(client, second)).status_code == 200


async def test_a_token_is_not_reissued_for_a_device_revoked_after_its_request_was_admitted(tmp_path, monkeypatch):
    async with bench(tmp_path) as b, b.sync() as client:
        paired = await paired_over(client, b)
        reissue_for = devices.reissue

        async def revoked_first(core, device, **options):
            await devices.revoke(core, device, now=b.clock())
            return await reissue_for(core, device, **options)

        monkeypatch.setattr(devices, "reissue", revoked_first)
        refused = await reissue(client, paired["token"])

    assert (refused.status_code, refused.json()["code"]) == (401, "device_revoked")


async def test_a_heartbeat_is_kept_as_the_devices_last_status(tmp_path):
    async with bench(tmp_path) as b, b.sync() as client:
        paired = await paired_over(client, b)
        status = contract_fixture("valid", "device/status", "full")

        kept = await client.post("/v1/devices/me/status", json=status, headers=bearer(paired["token"]))

        assert kept.status_code == 204
        record = await b.device(paired["device_id"])
        assert (record.last_status, record.last_status_at) == (status, "2026-03-02T09:00:00.000Z")


@pytest.mark.parametrize(
    ("status", "code"),
    [
        pytest.param({"app_version": "0.1.0"}, "invalid_request", id="without its counts"),
        pytest.param({**STATUS, "pending_upload_count": -1}, "invalid_request", id="a negative count"),
        pytest.param({**STATUS, "last_error": unicodedata.normalize("NFD", "업로드 실패")}, "not_canonical", id="an error not in NFC"),
    ],
)
async def test_a_heartbeat_the_contract_refuses_is_400_and_keeps_nothing(tmp_path, status, code):
    async with bench(tmp_path) as b, b.sync() as client:
        paired = await paired_over(client, b)
        refused = await client.post("/v1/devices/me/status", json=status, headers=bearer(paired["token"]))

        assert (refused.status_code, refused.json()["code"]) == (400, code)
        assert (await b.device(paired["device_id"])).last_status is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/devices"), ("POST", "/v1/devices/pairing-offer"), ("POST", "/v1/devices/{device}/revoke"),
        ("POST", "/v1/devices/{device}/approve"), ("GET", "/api/status"), ("POST", "/api/login"), ("GET", "/"),
    ],
)
async def test_the_sync_port_has_no_way_to_manage_devices(tmp_path, method, path):
    """Offering secrets, revoking, approving and the rest of the admin API live on the admin listener only, never
    behind tailscale serve: a paired Device's token reaches none of them."""
    async with bench(tmp_path) as b, b.sync() as client:
        paired = await paired_over(client, b)
        answer = await client.request(method, path.format(device=paired["device_id"]), headers=bearer(paired["token"]))

    assert (answer.status_code, answer.json()["code"]) == (404, "not_found")


async def test_the_sync_app_serve_runs_counts_failed_pairings_across_its_requests(tmp_path):
    async with bench(tmp_path) as b:
        app = sync_devices.sync_app(Hosted(b.core, Waking(), b.redactor))
        async with client_of(app, CONTRACT) as client:
            refused = [await client.post("/v1/pair", json=pair_body(UNKNOWN_SECRET)) for _ in range(11)]

    assert ["Retry-After" in answer.headers for answer in refused] == [False] * 10 + [True]
    assert {answer.json()["code"] for answer in refused} == {"unauthorized"}
