"""Blob uploads over the sync app (#4, D7, E19): open, chunks in any order, finalize, and the commit that follows.
Every response a test gets has passed sync_contract.check against openapi/sync.yaml."""

import hashlib
import uuid

import pytest
from conftest import contract_fixture
from sync_client import syncing

CHUNK = 4
DATA = b"0123456789"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pieces(data):
    return [data[start : start + CHUNK] for start in range(0, len(data), CHUNK)]


def upload(data):
    return {"sha256": sha(data), "size": len(data)}


def chunk_url(data, index):
    return f"/v1/blobs/uploads/{sha(data)}/chunks/{index}"


def finalize_url(data):
    return f"/v1/blobs/uploads/{sha(data)}/finalize"


async def send_all(client, data, order=None):
    for index in order if order is not None else range(len(pieces(data))):
        answer = await client.put(chunk_url(data, index), content=pieces(data)[index])
        assert answer.status_code == 204, answer.json()


def commit_of(data):
    """The contract's camera_photo commit, carrying data as its one photo and without its link to an Item."""
    capture = contract_fixture("valid", "capture/commit", "camera_photo")
    capture["links"] = []
    capture["attachments"][0]["blob_sha256"] = sha(data)
    return {"mutation_id": str(uuid.uuid4()), "op": "capture.commit", "capture": capture}


async def test_an_upload_opens_takes_its_chunks_in_reverse_order_and_finalizes_into_a_stored_blob(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        opened = await client.post("/v1/blobs/uploads", json=upload(DATA))
        await send_all(client, DATA, order=[2, 1, 0])
        finalized = await client.post(finalize_url(DATA))
        again = await client.post("/v1/blobs/uploads", json=upload(DATA))

    assert (opened.status_code, opened.json()) == (201, {**upload(DATA), "chunk_size": CHUNK, "received": []})
    assert (finalized.status_code, finalized.json()) == (200, upload(DATA))
    assert (again.status_code, again.json()) == (200, upload(DATA))
    assert core.blobs.path_of(sha(DATA)).read_bytes() == DATA


async def test_asking_again_resumes_with_the_chunks_received_and_only_the_rest_are_sent(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        await send_all(client, DATA, order=[1])
        resumed = await client.post("/v1/blobs/uploads", json=upload(DATA))
        await send_all(client, DATA, order=[i for i in range(3) if i not in resumed.json()["received"]])
        finalized = await client.post(finalize_url(DATA))

    assert (resumed.status_code, resumed.json()["received"]) == (201, [1])
    assert finalized.status_code == 200


async def test_an_upload_resumes_after_the_hub_restarts(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        await send_all(client, DATA, order=[0, 2])
    async with syncing(tmp_path, chunk_size=CHUNK, phone=phone) as (core, client, phone, runner):
        resumed = await client.post("/v1/blobs/uploads", json=upload(DATA))
        await send_all(client, DATA, order=[1])
        finalized = await client.post(finalize_url(DATA))

    assert resumed.json()["received"] == [0, 2]
    assert finalized.json() == upload(DATA)


async def test_the_same_bytes_again_are_a_no_op_and_other_bytes_for_a_stored_index_are_chunk_mismatch(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        first = await client.put(chunk_url(DATA, 0), content=b"0123")
        same = await client.put(chunk_url(DATA, 0), content=b"0123")
        other = await client.put(chunk_url(DATA, 0), content=b"9999")

    assert first.status_code == same.status_code == 204
    assert (other.status_code, other.json()["code"]) == (409, "chunk_mismatch")


async def test_a_finalize_whose_bytes_are_not_the_sha256_stores_nothing_and_discards_the_chunks(tmp_path):
    claimed = {"sha256": sha(b"something else"), "size": len(DATA)}
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=claimed)
        for index, piece in enumerate(pieces(DATA)):
            assert (await client.put(f"/v1/blobs/uploads/{claimed['sha256']}/chunks/{index}", content=piece)).status_code == 204
        finalized = await client.post(f"/v1/blobs/uploads/{claimed['sha256']}/finalize")
        reopened = await client.post("/v1/blobs/uploads", json=claimed)

    assert (finalized.status_code, finalized.json()["code"]) == (409, "hash_mismatch")
    assert not core.blobs.path_of(claimed["sha256"]).exists()
    assert (reopened.status_code, reopened.json()["received"]) == (201, [])


async def test_a_finalize_with_chunks_missing_is_incomplete_and_names_them(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        await send_all(client, DATA, order=[1])
        finalized = await client.post(finalize_url(DATA))

    assert (finalized.status_code, finalized.json()["code"], finalized.json()["details"]) == (409, "incomplete", {"missing_chunks": [0, 2]})


async def test_an_empty_blob_has_no_chunks_and_finalizes_at_once(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        opened = await client.post("/v1/blobs/uploads", json=upload(b""))
        finalized = await client.post(finalize_url(b""))

    assert opened.json() == {"sha256": EMPTY_SHA256, "size": 0, "chunk_size": CHUNK, "received": []}
    assert finalized.json() == {"sha256": EMPTY_SHA256, "size": 0}


async def test_finalize_of_a_stored_blob_is_200_with_or_without_a_session_and_of_nothing_404(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        unknown = await client.post(finalize_url(DATA))
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        await send_all(client, DATA)
        first = await client.post(finalize_url(DATA))
        retried = await client.post(finalize_url(DATA))

    assert (unknown.status_code, unknown.json()["code"]) == (404, "not_found")
    assert first.status_code == retried.status_code == 200


async def test_a_chunk_without_an_open_upload_is_404(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        answer = await client.put(chunk_url(DATA, 0), content=b"0123")

    assert (answer.status_code, answer.json()["code"]) == (404, "not_found")


async def test_an_upload_the_hub_has_no_room_for_is_hub_storage_full(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK, free_space=lambda path: 2 * len(DATA) - 1) as (core, client, phone, runner):
        answer = await client.post("/v1/blobs/uploads", json=upload(DATA))

    assert (answer.status_code, answer.json()["code"]) == (400, "hub_storage_full")


@pytest.mark.parametrize(
    "body",
    [{**upload(DATA), "chunk_size": CHUNK}, {"sha256": sha(DATA)}, {"sha256": sha(DATA).upper(), "size": 10}, {"sha256": sha(DATA), "size": -1}, [1]],
    ids=["chunk_size sent", "size missing", "sha256 uppercase", "size negative", "not an object"],
)
async def test_an_open_the_contract_does_not_accept_is_invalid_request(tmp_path, body):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        answer = await client.post("/v1/blobs/uploads", json=body)

    assert (answer.status_code, answer.json()["code"]) == (400, "invalid_request")


async def test_asking_again_with_another_size_while_the_upload_is_open_is_invalid_request(tmp_path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        answer = await client.post("/v1/blobs/uploads", json={**upload(DATA), "size": 11})

    assert (answer.status_code, answer.json()["code"]) == (400, "invalid_request")


@pytest.mark.parametrize("index", ["01", "00", " 1", "-1", "1e1", "x", "1.0", "+1", "3", "99999999999999999999"])
async def test_a_chunk_index_that_is_not_one_of_the_upload_s_written_plainly_is_invalid_request(tmp_path, index):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        answer = await client.put(f"/v1/blobs/uploads/{sha(DATA)}/chunks/{index}", content=b"0123")

    assert (answer.status_code, answer.json()["code"]) == (400, "invalid_request")


@pytest.mark.parametrize("path", ["/v1/blobs/uploads/ABC/chunks/0", "/v1/blobs/uploads/ABC/finalize"])
async def test_a_path_sha_that_is_not_a_sha256_is_invalid_request(tmp_path, path):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        answer = await client.request("PUT" if "chunks" in path else "POST", path, content=b"0123" if "chunks" in path else None)

    assert (answer.status_code, answer.json()["code"]) == (400, "invalid_request")


@pytest.mark.parametrize("content", [b"012", b"01234", b""], ids=["short", "long", "empty"])
async def test_a_chunk_of_another_length_is_invalid_request_and_stores_nothing(tmp_path, content):
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        answer = await client.put(chunk_url(DATA, 0), content=content)
        resumed = await client.post("/v1/blobs/uploads", json=upload(DATA))

    assert (answer.status_code, answer.json()["code"]) == (400, "invalid_request")
    assert resumed.json()["received"] == []


async def test_a_chunk_sent_without_a_length_is_read_only_one_byte_past_its_own(tmp_path):
    read = []

    async def endless():
        for _ in range(1000):
            read.append(CHUNK)
            yield b"x" * CHUNK

    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        answer = await client.put(chunk_url(DATA, 0), content=endless())

    assert (answer.status_code, answer.json()["code"]) == (400, "invalid_request")
    assert answer.request.headers["transfer-encoding"] == "chunked"
    assert len(read) <= 3


async def test_a_missing_blob_commit_retries_then_after_the_upload_applies_and_shows_in_the_feed(tmp_path):
    mutation = commit_of(DATA)
    async with syncing(tmp_path, chunk_size=CHUNK) as (core, client, phone, runner):
        waiting = await client.post("/v1/mutations", json={"mutations": [mutation]})
        await client.post("/v1/blobs/uploads", json=upload(DATA))
        await send_all(client, DATA)
        await client.post(finalize_url(DATA))
        applied = await client.post("/v1/mutations", json={"mutations": [mutation]})
        feed = await client.get("/v1/changes", params={"since": "0"})
        duplicate_upload = await client.post("/v1/blobs/uploads", json=upload(DATA))

    assert waiting.json()["results"][0] == {"mutation_id": mutation["mutation_id"], "result": "retry", "missing_blobs": [sha(DATA)]}
    assert applied.json()["results"][0]["result"] == "applied"
    rows = sorted((change["entity"], change["id"]) for change in feed.json()["changes"] if change["entity"] in ("capture", "attachment"))
    assert rows == [("attachment", mutation["capture"]["attachments"][0]["id"]), ("capture", mutation["capture"]["id"])]
    assert (duplicate_upload.status_code, duplicate_upload.json()) == (200, upload(DATA))
    assert runner.wakes == 1
