"""The sync listener's app (#4): the envelope every response passes through, GET /v1/hub, GET /v1/changes and
POST /v1/mutations. Every response a test gets has passed sync_contract.check against openapi/sync.yaml."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import httpx
from campuslife_contracts import CONTRACT_VERSION, MIN_CONTRACT_VERSION, contract_root
from conftest import contract_fixture
from fastapi import APIRouter, Request
from fastapi.responses import Response
from sync_contract import checked

from campuslife_hub import jobs
from campuslife_hub.clock import now_instant
from campuslife_hub.core import open_core
from campuslife_hub.devices import DeviceId, Refusal, RefusalCode
from campuslife_hub.host import Hosted
from campuslife_hub.redaction import Redactor
from campuslife_hub.sync import create_sync_app

TOKEN = "phone-token-" + "x" * 40
CONTRACT = {"CampusLife-Contract": str(CONTRACT_VERSION)}


class Waking:
    """The job runner as the sync app sees it: something to wake once a commit leaves jobs to run."""

    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1


def admitting(device):
    """An authenticator for tests: TOKEN is the device, every other bearer is refused as unknown."""

    async def authenticate(bearer):
        return device if bearer == TOKEN else Refusal(RefusalCode.UNAUTHORIZED)

    return authenticate


async def add_phone(core) -> DeviceId:
    device = str(uuid.uuid4())
    await core.db.write(
        lambda tx: tx.execute(
            "INSERT INTO devices (id, platform, device_name, app_version, state, created_at) VALUES (?, 'android', 'phone', '0.1.0', 'active', ?)",
            (device, now_instant()),
        )
    )
    return DeviceId(device)


def client_of(app, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://hub.tailnet.test",
        headers={**CONTRACT, "Authorization": f"Bearer {TOKEN}", **(headers or {})},
        event_hooks={"response": [checked]},
    )


def request_with(client, method, path, replaced):
    """A request with the client's headers, each header named in replaced sent once per value given instead."""
    kept = [(name, value) for name, value in client.headers.multi_items() if name.lower() not in replaced]
    added = [(name, value) for name, values in replaced.items() for value in values]
    return httpx.Request(method, client.base_url.join(path), headers=[*kept, *added])


@asynccontextmanager
async def syncing(tmp_path, *, authenticate=None, routers=(), **options):
    """The sync app over a new Hub with one paired phone, a client that sends its token, and the runner it wakes."""
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        phone = await add_phone(core)
        runner = Waking()
        app = create_sync_app(Hosted(core, runner, Redactor()), authenticate or admitting(phone), routers, **options)
        async with client_of(app) as client:
            yield core, client, phone, runner


async def test_get_hub_reports_the_identity_and_the_contract_versions_with_the_hub_headers(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        response = await client.get("/v1/hub")

    assert response.status_code == 200
    assert response.json() == {
        "hub_id": core.identity.hub_id, "epoch": core.identity.epoch,
        "contract_version": CONTRACT_VERSION, "min_contract_version": MIN_CONTRACT_VERSION,
    }
    assert response.headers["campuslife-hub-id"] == core.identity.hub_id
    assert response.headers["campuslife-epoch"] == core.identity.epoch


SHA = "ab" * 32
OPERATIONS = [
    ("POST", "/v1/blobs/uploads"), ("PUT", f"/v1/blobs/uploads/{SHA}/chunks/0"), ("POST", f"/v1/blobs/uploads/{SHA}/finalize"),
    ("GET", f"/v1/blobs/{SHA}"), ("POST", "/v1/mutations"), ("GET", "/v1/changes?since=0"), ("GET", "/v1/hub"),
    ("POST", "/v1/devices/me/status"), ("POST", "/v1/devices/me/token"),
]
ADMIN_PATHS = [("GET", "/api/status"), ("POST", "/api/login"), ("GET", "/login"), ("GET", "/"), ("GET", "/jobs")]
STATUS = {"app_version": "0.1.0", "pending_upload_count": 0, "pending_mutation_count": 0, "open_conflict_count": 0}


def status_router(seen):
    """POST /v1/devices/me/status as #19 will add it through routers: it reads its body with the envelope's helpers."""
    from campuslife_hub.sync_envelope import device_of, judge, read_json

    added = APIRouter()

    @added.post("/v1/devices/me/status")
    async def post_status(request: Request):
        body = await read_json(request)
        judge("device/status", body)
        seen.append((device_of(request), body))
        return Response(status_code=204)

    return added


def pair_router():
    """POST /v1/pair as #19 will add it, refusing every secret: reaching it at all shows pairing needs no token."""
    from campuslife_hub.sync_envelope import SyncError

    added = APIRouter()

    @added.post("/v1/pair")
    async def pair(request: Request):
        raise SyncError(401, RefusalCode.UNAUTHORIZED, "the pair route judged the secret")

    return added


async def test_with_the_default_authenticator_every_path_but_pairing_is_401_with_the_hub_headers(tmp_path):
    from campuslife_hub.sync import refuse_every_bearer

    async with syncing(tmp_path, authenticate=refuse_every_bearer) as (core, client, phone, runner):
        answers = {(method, path): await client.request(method, path) for method, path in [*OPERATIONS, *ADMIN_PATHS, ("GET", "/v1/nowhere")]}

    async with syncing(tmp_path / "pairing", authenticate=refuse_every_bearer, routers=[pair_router()]) as (_, pairing_client, _, _):
        pairing = await pairing_client.post("/v1/pair", json={})

    for answer in answers.values():
        assert answer.status_code == 401
        assert answer.json()["code"] == "unauthorized"
        assert answer.headers["www-authenticate"] == "Bearer"
        assert answer.headers["campuslife-hub-id"] == core.identity.hub_id
    assert (pairing.status_code, pairing.json()["message"]) == (401, "the pair route judged the secret")


async def test_a_route_learns_the_device_the_token_names_and_never_the_token(tmp_path):
    seen = []
    async with syncing(tmp_path, routers=[status_router(seen)]) as (core, client, phone, runner):
        answer = await client.post("/v1/devices/me/status", json=STATUS)
        refused = await client.post("/v1/devices/me/status", json=STATUS, headers={"Authorization": "Bearer someone-else"})

    assert answer.status_code == 204
    assert seen == [(phone, STATUS)]
    assert refused.status_code == 401


async def test_a_refusal_s_code_and_retry_after_reach_the_client(tmp_path):
    async def dormant(bearer):
        return Refusal(RefusalCode.DEVICE_DORMANT, retry_after=30)

    async with syncing(tmp_path, authenticate=dormant) as (core, client, phone, runner):
        answer = await client.get("/v1/changes?since=0")

    assert (answer.status_code, answer.json()["code"], answer.headers["retry-after"]) == (401, "device_dormant", "30")


async def test_the_bearer_token_is_read_only_from_one_authorization_bearer_header(tmp_path):
    bearers = []

    async def recording(bearer):
        bearers.append(bearer)
        return Refusal(RefusalCode.UNAUTHORIZED)

    async with syncing(tmp_path, authenticate=recording) as (core, client, phone, runner):
        for authorization in ([f"bearer {TOKEN}"], [f"Basic {TOKEN}"], ["Bearer"], [f"Bearer {TOKEN}", f"Bearer {TOKEN}"]):
            await client.send(request_with(client, "GET", "/v1/hub", {"authorization": authorization}))
        await client.send(request_with(client, "GET", "/v1/hub", {"authorization": []}))

    assert bearers == [TOKEN, None, None, None, None]


async def test_admin_paths_are_not_on_the_sync_app(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        answers = [await client.request(method, path) for method, path in ADMIN_PATHS]

    assert [(answer.status_code, answer.json()["code"]) for answer in answers] == [(404, "not_found")] * len(ADMIN_PATHS)


async def test_docs_and_the_openapi_document_are_not_served(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        answers = [await client.get(path) for path in ("/docs", "/redoc", "/openapi.json")]

    assert [answer.status_code for answer in answers] == [404, 404, 404]


async def test_campuslife_contract_must_be_sent_once_as_a_version_before_anything_else(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        answers = []
        for contract in ([], ["01"], ["1.0"], [""], ["0"], ["1", "1"]):
            answers.append(await client.send(request_with(client, "GET", "/v1/hub", {"campuslife-contract": contract, "authorization": []})))

    assert [(answer.status_code, answer.json()["code"]) for answer in answers] == [(400, "invalid_request")] * 6


async def test_401_comes_before_contract_too_old_and_get_hub_answers_every_version(tmp_path):
    from campuslife_hub.sync_envelope import ContractVersions

    async with syncing(tmp_path, versions=ContractVersions(current=3, minimum=2)) as (core, client, phone, runner):
        old = {"CampusLife-Contract": "1"}
        unknown = await client.get("/v1/changes?since=0", headers={**old, "Authorization": "Bearer someone-else"})
        too_old = await client.get("/v1/changes?since=0", headers=old)
        too_old_post = await client.post("/v1/mutations", headers=old, json={"mutations": []})
        hub = await client.get("/v1/hub", headers=old)
        current = await client.get("/v1/changes?since=0", headers={"CampusLife-Contract": "2"})
        newer = await client.get("/v1/changes?since=0", headers={"CampusLife-Contract": "4"})

    assert (unknown.status_code, unknown.json()["code"]) == (401, "unauthorized")
    assert (too_old.status_code, too_old.json()["code"]) == (400, "contract_too_old")
    assert (too_old_post.status_code, too_old_post.json()["code"]) == (400, "contract_too_old")
    assert hub.status_code == 200
    assert (hub.json()["contract_version"], hub.json()["min_contract_version"]) == (3, 2)
    assert current.status_code == newer.status_code == 200


async def test_an_unknown_path_is_404_and_a_wrong_method_405_both_as_contract_errors(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        unknown = await client.get("/v1/nowhere")
        wrong = await client.put("/v1/hub")

    assert (unknown.status_code, unknown.json()["code"]) == (404, "not_found")
    assert (wrong.status_code, wrong.json()["code"]) == (405, "invalid_request")
    assert "GET" in wrong.headers["allow"]


async def test_a_route_that_fails_answers_500_internal_error_telling_nothing_and_logs_why(tmp_path, caplog):
    failing = APIRouter()

    @failing.post("/v1/devices/me/status")
    async def post_status(request: Request):
        raise RuntimeError("secret detail of the failure")

    async with syncing(tmp_path, routers=[failing]) as (core, client, phone, runner):
        answer = await client.post("/v1/devices/me/status", json=STATUS)
        after = await client.get("/v1/hub")

    assert (answer.status_code, answer.json()["code"]) == (500, "internal_error")
    assert "secret detail" not in answer.text
    assert after.status_code == 200
    assert [record for record in caplog.records if record.exc_info and "secret detail of the failure" in str(record.exc_info[1])]


async def test_an_authenticator_that_fails_answers_500(tmp_path):
    async def broken(bearer):
        raise OSError("database gone")

    async with syncing(tmp_path, authenticate=broken) as (core, client, phone, runner):
        answer = await client.get("/v1/hub")

    assert (answer.status_code, answer.json()["code"]) == (500, "internal_error")


async def test_a_json_body_is_strict_json_sent_as_application_json_and_at_most_8_mib(tmp_path):
    from campuslife_hub.sync_envelope import MAX_JSON_BODY

    seen = []
    async with syncing(tmp_path, routers=[status_router(seen)]) as (core, client, phone, runner):
        url, as_json = "/v1/devices/me/status", {"Content-Type": "application/json"}
        plain = await client.post(url, content=json.dumps(STATUS), headers={"Content-Type": "text/plain"})
        untyped = await client.post(url, content=json.dumps(STATUS).encode())
        duplicate = await client.post(url, content=b'{"app_version": "1", "app_version": "2"}', headers=as_json)
        latin1 = await client.post(url, content='{"last_error": "caf\xe9"}'.encode("latin-1"), headers=as_json)
        large = await client.post(url, content=b" " * MAX_JSON_BODY + json.dumps(STATUS).encode(), headers=as_json)
        at_limit = await client.post(url, content=json.dumps(STATUS).encode().ljust(MAX_JSON_BODY), headers=as_json)
        charset = await client.post(url, content=json.dumps(STATUS).encode(), headers={"Content-Type": "Application/JSON; charset=utf-8"})

    for refused in (plain, untyped, duplicate, latin1, large):
        assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request"), refused.json()
    assert at_limit.status_code == charset.status_code == 204
    assert len(seen) == 2


async def test_a_body_whose_only_fault_is_nfd_is_not_canonical_and_any_other_fault_invalid_request(tmp_path):
    seen = []
    async with syncing(tmp_path, routers=[status_router(seen)]) as (core, client, phone, runner):
        nfd = await client.post("/v1/devices/me/status", json={**STATUS, "last_error": "Café Wi-Fi"})
        both = await client.post("/v1/devices/me/status", json={**STATUS, "last_error": "Café", "pending_upload_count": -1})
        unknown = await client.post("/v1/devices/me/status", json={**STATUS, "battery": 3})

    assert (nfd.status_code, nfd.json()["code"]) == (400, "not_canonical")
    assert (both.status_code, both.json()["code"]) == (400, "invalid_request")
    assert (unknown.status_code, unknown.json()["code"]) == (400, "invalid_request")
    assert seen == []


def new_id():
    return str(uuid.uuid4())


def tag(name="시험", mutation_id=None, tag_id=None):
    return {"mutation_id": mutation_id or new_id(), "op": "create", "entity": "tag", "id": tag_id or new_id(), "data": {"name": name, "color": "#ff8800"}}


def commit(capture, mutation_id=None):
    return {"mutation_id": mutation_id or new_id(), "op": "capture.commit", "capture": capture}


async def store(core, tmp_path, content: bytes) -> str:
    source = tmp_path / f"source-{uuid.uuid4()}"
    source.write_bytes(content)
    return (await core.blobs.put_file(source)).sha256


def photo_capture(sha):
    """The contract's camera_photo commit with its Blob and without its link, whose Item this Hub does not hold."""
    capture = contract_fixture("valid", "capture/commit", "camera_photo")
    capture["links"] = []
    capture["attachments"][0]["blob_sha256"] = sha
    return capture


async def test_get_changes_pages_the_feed_from_a_cursor(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        empty = await client.get("/v1/changes", params={"since": "0"})
        made = [tag(f"태그 {n}") for n in range(3)]
        assert (await client.post("/v1/mutations", json={"mutations": made})).status_code == 200
        first = await client.get("/v1/changes", params={"since": "0", "limit": "2"})
        rest = await client.get("/v1/changes", params={"since": str(first.json()["next_since"])})

    assert empty.json() == {"hub_id": core.identity.hub_id, "epoch": core.identity.epoch, "changes": [], "next_since": 0, "has_more": False}
    assert [change["id"] for change in first.json()["changes"]] == [made[0]["id"], made[1]["id"]]
    assert first.json()["has_more"] is True
    assert [change["id"] for change in rest.json()["changes"]] == [made[2]["id"]]
    assert rest.json()["has_more"] is False


async def test_get_changes_refuses_a_query_the_contract_does_not_accept(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        answers = [
            await client.get("/v1/changes" + query)
            for query in ("", "?since=", "?since=01", "?since=-1", "?since=9223372036854775808", "?since=0&limit=0", "?since=0&limit=501",
                          "?since=0&cursor=3", "?since=0&since=1")
        ]

    assert [(answer.status_code, answer.json()["code"]) for answer in answers] == [(400, "invalid_request")] * len(answers)


async def test_post_mutations_applies_a_batch_in_order_and_a_resend_gets_the_first_answers(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        batch = [tag("시험"), {**tag(), "data": {"name": "x", "color": "#ff8800", "colour": "#fff"}}]
        first = await client.post("/v1/mutations", json={"mutations": batch})
        again = await client.post("/v1/mutations", json={"mutations": batch})
        reused = await client.post("/v1/mutations", json={"mutations": [{**batch[0], "data": {"name": "다른 이름", "color": "#ff8800"}}]})

    assert [result["result"] for result in first.json()["results"]] == ["applied", "rejected"]
    assert first.json()["results"][1]["reason"] == "schema"
    assert [result["result"] for result in again.json()["results"]] == ["duplicate", "duplicate"]
    assert [result["original"] for result in again.json()["results"]] == first.json()["results"]
    assert reused.json()["results"][0]["result"] == "duplicate"
    assert runner.wakes == 0


async def test_a_commit_waits_for_its_blobs_and_once_applied_wakes_the_runner_and_shows_in_the_feed(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        capture = photo_capture(SHA)
        sent = [commit(capture), tag()]
        waiting = await client.post("/v1/mutations", json={"mutations": sent})
        capture["attachments"][0]["blob_sha256"] = sha = await store(core, tmp_path, b"\xff\xd8 a synthetic photo")
        resent = [commit(capture, sent[0]["mutation_id"]), sent[1]]
        applied = await client.post("/v1/mutations", json={"mutations": resent})
        wakes_after_commit = runner.wakes
        jobs_after_commit = await core.db.read(jobs.counts)
        duplicate = await client.post("/v1/mutations", json={"mutations": resent})
        feed = await client.get("/v1/changes", params={"since": "0"})
        jobs_after_duplicate = await core.db.read(jobs.counts)

    assert [result["result"] for result in waiting.json()["results"]] == ["retry", "not_attempted"]
    assert waiting.json()["results"][0]["missing_blobs"] == [SHA]
    assert [result["result"] for result in applied.json()["results"]] == ["applied", "applied"]
    assert wakes_after_commit == 1
    assert [result["result"] for result in duplicate.json()["results"]] == ["duplicate", "duplicate"]
    assert runner.wakes == 1
    rows = sorted((change["entity"], change["id"]) for change in feed.json()["changes"] if change["entity"] in ("capture", "attachment"))
    assert rows == [("attachment", capture["attachments"][0]["id"]), ("capture", capture["id"])]
    assert sum(jobs_after_commit.values()) >= 1
    assert jobs_after_duplicate == jobs_after_commit
    [attachment] = [change["data"] for change in feed.json()["changes"] if change["entity"] == "attachment"]
    assert attachment["blob_sha256"] == sha


async def test_the_same_mutation_id_with_other_content_is_rejected_id_reuse_inside_a_200(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        capture = contract_fixture("valid", "mutation/mutation", "commit_text_only")["capture"]
        mutation_id = new_id()
        first = await client.post("/v1/mutations", json={"mutations": [commit(capture, mutation_id)]})
        other = await client.post("/v1/mutations", json={"mutations": [commit({**capture, "user_note": "바뀐 메모"})]})

    assert first.json()["results"][0]["result"] == "applied"
    assert other.status_code == 200
    assert (other.json()["results"][0]["result"], other.json()["results"][0]["reason"]) == ("rejected", "id_reuse")


async def test_every_invalid_capture_commit_of_the_contract_is_rejected_inside_a_200(tmp_path):
    cases = sorted((contract_root() / "fixtures" / "invalid" / "capture" / "commit").glob("*[!t].json"))
    cases = [path for path in cases if not path.name.endswith(".expect.json")]
    async with syncing(tmp_path) as (core, client, phone, runner):
        answer = await client.post("/v1/mutations", json={"mutations": [commit(json.loads(path.read_text(encoding="utf-8"))) for path in cases]})

    assert len(cases) > 10
    assert answer.status_code == 200
    assert {result["result"] for result in answer.json()["results"]} == {"rejected"}


async def test_post_mutations_refuses_what_is_not_a_batch_of_1_to_200(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        answers = [
            await client.post("/v1/mutations", json=body)
            for body in (
                [tag()], {"mutations": []}, {"mutations": tag()}, {"mutations": [tag()], "device_id": "x"},
                contract_fixture("invalid", "mutation/batch_request", "too_many"), {"mutations": [{"op": "create"}]},
            )
        ]
        most = await client.post("/v1/mutations", json=contract_fixture("valid", "mutation/batch_request", "two_hundred"))

    assert [(answer.status_code, answer.json()["code"]) for answer in answers] == [(400, "invalid_request")] * len(answers)
    assert most.status_code == 200
    assert len(most.json()["results"]) == 200


async def test_a_device_s_batches_apply_one_after_another(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        first, second = tag("하나"), tag("둘")
        answers = await asyncio.gather(
            client.post("/v1/mutations", json={"mutations": [first, second]}),
            client.post("/v1/mutations", json={"mutations": [second, first]}),
        )

    outcomes = sorted([result["result"] for result in answer.json()["results"]] for answer in answers)
    assert outcomes == [["applied", "applied"], ["duplicate", "duplicate"]]
