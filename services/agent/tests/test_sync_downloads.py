"""Blob downloads over the sync app (#4, D7): the whole Blob, one byte range, 304 for the ETag the client holds, 416.
Every response a test gets has passed sync_contract.check against openapi/sync.yaml."""

import asyncio
import hashlib

import pytest
from sync_client import CONTRACT, TOKEN, Waking, admitting, request_with, syncing

from campuslife_hub.host import Hosted
from campuslife_hub.redaction import Redactor
from campuslife_hub.sync import create_sync_app

DATA = b"0123456789"
SHA = hashlib.sha256(DATA).hexdigest()
ETAG = f'"{SHA}"'
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


async def stored(core, tmp_path, data=DATA):
    source = tmp_path / "source"
    source.write_bytes(data)
    return (await core.blobs.put_file(source)).sha256


async def test_a_blob_comes_whole_as_octets_with_its_sha256_as_the_etag(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        whole = await client.get(f"/v1/blobs/{SHA}")

    assert (whole.status_code, whole.content) == (200, DATA)
    assert whole.headers["etag"] == ETAG
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-type"] == "application/octet-stream"
    assert whole.headers["content-length"] == str(len(DATA))


@pytest.mark.parametrize(
    ("asked", "served", "content_range"),
    [
        ("bytes=2-5", b"2345", "bytes 2-5/10"),
        ("bytes=0-0", b"0", "bytes 0-0/10"),
        ("bytes=7-", b"789", "bytes 7-9/10"),
        ("bytes=8-99", b"89", "bytes 8-9/10"),
        ("bytes=9-9223372036854775807", b"9", "bytes 9-9/10"),
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=-99", DATA, "bytes 0-9/10"),
    ],
)
async def test_one_range_comes_as_206_with_the_bytes_it_names_inside_the_blob(tmp_path, asked, served, content_range):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        part = await client.get(f"/v1/blobs/{SHA}", headers={"Range": asked})

    assert (part.status_code, part.content) == (206, served)
    assert part.headers["content-range"] == content_range
    assert part.headers["content-length"] == str(len(served))
    assert part.headers["etag"] == ETAG


async def test_a_download_cut_short_resumes_from_the_byte_it_reached(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        first = await client.get(f"/v1/blobs/{SHA}", headers={"Range": "bytes=0-3"})
        rest = await client.get(f"/v1/blobs/{SHA}", headers={"Range": f"bytes={len(first.content)}-"})

    assert hashlib.sha256(first.content + rest.content).hexdigest() == SHA


@pytest.mark.parametrize("asked", ["bytes=10-", "bytes=10-20", "bytes=-0"])
async def test_a_range_starting_past_the_end_is_416_naming_the_size(tmp_path, asked):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        refused = await client.get(f"/v1/blobs/{SHA}", headers={"Range": asked})

    assert refused.status_code == 416
    assert refused.headers["content-range"] == "bytes */10"
    assert refused.json()["code"] == "invalid_request"


@pytest.mark.parametrize(
    "asked",
    ["bytes=0-1,3-4", "bytes=5-3", "bytes=", "bytes=-", "items=0-1", "bytes=01-x", "bytes=99999999999999999999-", "bytes=9223372036854775808-"],
)
async def test_a_range_the_contract_does_not_accept_is_invalid_request(tmp_path, asked):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        refused = await client.get(f"/v1/blobs/{SHA}", headers={"Range": asked})

    assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")


@pytest.mark.parametrize("header", ["Range", "If-None-Match"])
async def test_a_download_header_sent_twice_is_invalid_request(tmp_path, header):
    value = {"Range": "bytes=0-1", "If-None-Match": ETAG}[header]
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        refused = await client.send(request_with(client, "GET", f"/v1/blobs/{SHA}", {header.lower(): [value, value]}))

    assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")


@pytest.mark.parametrize("headers", [{"If-None-Match": ETAG}, {"If-None-Match": ETAG, "Range": "bytes=2-5"}])
async def test_the_etag_the_client_holds_answers_304_without_the_bytes(tmp_path, headers):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        unchanged = await client.get(f"/v1/blobs/{SHA}", headers=headers)

    assert (unchanged.status_code, unchanged.content) == (304, b"")
    assert unchanged.headers["etag"] == ETAG


async def test_another_etag_gets_the_blob(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        whole = await client.get(f"/v1/blobs/{SHA}", headers={"If-None-Match": f'"{EMPTY_SHA256}"'})

    assert (whole.status_code, whole.content) == (200, DATA)


@pytest.mark.parametrize("held", [f'W/"{SHA}"', "*", SHA, f'"{SHA}", "{EMPTY_SHA256}"'])
async def test_an_if_none_match_the_contract_does_not_accept_is_invalid_request(tmp_path, held):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        refused = await client.get(f"/v1/blobs/{SHA}", headers={"If-None-Match": held})

    assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")


async def test_an_empty_blob_comes_whole_with_no_bytes_and_no_range_fits_in_it(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path, b"")
        whole = await client.get(f"/v1/blobs/{EMPTY_SHA256}")
        ranges = [await client.get(f"/v1/blobs/{EMPTY_SHA256}", headers={"Range": asked}) for asked in ("bytes=0-", "bytes=-1")]

    assert (whole.status_code, whole.content, whole.headers["content-length"]) == (200, b"", "0")
    assert whole.headers["etag"] == f'"{EMPTY_SHA256}"'
    assert [(refused.status_code, refused.headers["content-range"]) for refused in ranges] == [(416, "bytes */0")] * 2


async def test_a_blob_the_hub_does_not_store_is_not_found(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        missing = await client.get(f"/v1/blobs/{SHA}")

    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")


async def test_a_blob_whose_file_is_gone_is_not_found(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        final = core.blobs.path_of(SHA)
        final.chmod(0o600)
        final.unlink()
        missing = await client.get(f"/v1/blobs/{SHA}")

    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")


@pytest.mark.parametrize("path", [SHA.upper(), SHA[:-1], SHA + "0", "x" * 64])
async def test_a_path_sha_that_is_not_a_sha256_is_invalid_request(tmp_path, path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        refused = await client.get(f"/v1/blobs/{path}")

    assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")


async def test_a_download_without_a_known_token_is_unauthorized(tmp_path):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        refused = await client.get(f"/v1/blobs/{SHA}", headers={"Authorization": "Bearer someone-else"})

    assert (refused.status_code, refused.json()["code"]) == (401, "unauthorized")



def watching_handles(core, monkeypatch):
    """The handles the Blob store opens for the sync app from here on."""
    opened = []
    real_open_blob = core.blobs.open_blob

    async def open_blob(sha256):
        found = await real_open_blob(sha256)
        opened.append(found[1])
        return found

    monkeypatch.setattr(core.blobs, "open_blob", open_blob)
    return opened


@pytest.mark.parametrize("headers", [{}, {"Range": "bytes=2-5"}, {"Range": "bytes=10-"}, {"If-None-Match": ETAG}])
async def test_every_answer_closes_the_file_it_opened(tmp_path, monkeypatch, headers):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        opened = watching_handles(core, monkeypatch)
        await client.get(f"/v1/blobs/{SHA}", headers=headers)

    assert [handle.closed for handle in opened] == [True]


async def get_directly(core, phone, sha256, *, client_goes):
    """The ASGI messages the sync app sends for a GET of the Blob, from a client that goes once the request is sent,
    or that stays until the answer ends."""
    app = create_sync_app(Hosted(core, Waking(), Redactor()), admitting(phone))
    arrivals = iter([{"type": "http.request", "body": b"", "more_body": False}])
    sent = []

    async def receive():
        arrival = next(arrivals, None)
        if arrival is not None:
            return arrival
        if not client_goes:
            await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    path = f"/v1/blobs/{sha256}"
    headers = [(name.lower().encode(), value.encode()) for name, value in {**CONTRACT, "Authorization": f"Bearer {TOKEN}"}.items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": headers, "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    await asyncio.wait_for(app(scope, receive, send), 10)
    return sent


def body_of(sent):
    return b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")


async def test_a_client_that_goes_mid_download_stops_the_sending_and_the_file_closes(tmp_path, monkeypatch):
    big = bytes(range(256)) * 4096
    async with syncing(tmp_path) as (core, client, phone, runner):
        big_sha = await stored(core, tmp_path, big)
        opened = watching_handles(core, monkeypatch)
        sent = await get_directly(core, phone, big_sha, client_goes=True)

    assert len(body_of(sent)) < len(big)
    assert [handle.closed for handle in opened] == [True]


async def test_a_blob_file_shorter_than_its_size_ends_the_answer_unfinished_and_is_logged(tmp_path, monkeypatch, caplog):
    async with syncing(tmp_path) as (core, client, phone, runner):
        await stored(core, tmp_path)
        final = core.blobs.path_of(SHA)
        final.chmod(0o600)
        final.write_bytes(DATA[:5])
        opened = watching_handles(core, monkeypatch)
        sent = await get_directly(core, phone, SHA, client_goes=False)

    assert body_of(sent) == DATA[:5]
    assert [message.get("more_body", False) for message in sent if message["type"] == "http.response.body"] == [True]
    assert "5 bytes short of its recorded size" in caplog.text
    assert [handle.closed for handle in opened] == [True]
