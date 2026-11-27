import asyncio
import hashlib
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import SignedAs
from fastapi import APIRouter, HTTPException
from opentelemetry import trace

from campuslife_hub import admin as admin_app
from campuslife_hub import admin_auth, artifacts, backup, jobs, mutations
from campuslife_hub.admin import Admin, create_app
from campuslife_hub.admin_auth import (
    HELLO_LIFETIME, LOGIN_CODE_LIFETIME, MAX_SIGNED_BODY_BYTES, SESSION_COOKIE, SESSION_LIFETIME, AdminAuth, hello_mac,
    hub_proof, signed_authorization,
)
from campuslife_hub.core import open_core
from campuslife_hub.job_runner import JobContext, JobRegistry, JobRunner
from campuslife_hub.jobs import Failed, JobSpec, JobState, Pipeline
from campuslife_hub.listing import AttachmentSummary, CaptureSummary, list_captures
from campuslife_hub.local_import import import_files
from campuslife_hub.purge import PurgeHooks
from campuslife_hub.redaction import Redactor

PORT = 8765
ADDRESS = f"127.0.0.1:{PORT}"
ELSEWHERE = f"127.0.0.1:{PORT + 1}"
BASE = f"http://{ADDRESS}"
ORIGIN = {"origin": BASE}
TOKEN = secrets.token_urlsafe(32)
SIGNED = SignedAs(TOKEN)
EXTRACT = Pipeline("extract-1")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 3, 2, 0, 30, tzinfo=UTC)

    def __call__(self):
        return self.now


@asynccontextmanager
async def hub(tmp_path, *, routers=(), clock=None, raise_app_exceptions=True, stop=lambda: None, served_as=lambda app: app, **admin_options):
    """An admin app over a fresh Hub, and a client that talks to it as a browser on this machine would."""
    async with open_core(tmp_path / "root", create=True, sync_roots=[], purge_hooks=PurgeHooks()) as core:
        redactor = Redactor()
        auth = AdminAuth(TOKEN, redactor, **({"clock": clock} if clock else {}))
        registry = JobRegistry()
        registry.handle("extract.text", EXTRACT, lambda job, context: None)
        admin = Admin(core, JobRunner(JobContext(core.db, core.blobs, registry)), auth, redactor, status=lambda: {"runner": {"restarts": 0}}, stop=stop,
                      **({"clock": clock} if clock else {}), **admin_options)
        app = create_app(admin, port=PORT, routers=routers)
        transport = httpx.ASGITransport(app=served_as(app), raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            yield client, admin


async def log_in(client):
    url = (await client.post("/api/login", auth=SIGNED)).json()["url"]
    response = await client.get(url)
    assert response.status_code == 303
    return response


POLICIES = {
    "no-referrer", "no-referrer-when-downgrade", "same-origin", "origin", "strict-origin", "origin-when-cross-origin",
    "strict-origin-when-cross-origin", "unsafe-url",
}


def browser_origin(page, action):
    """The Origin header a browser gives the POST a form on page submits to action (Fetch, "append a request Origin
    header"). A form's POST is not a CORS request, so the Referrer-Policy page came with decides whether it is null."""
    here, there = page.url, page.url.join(action)
    origin = f"{here.scheme}://{here.host}:{here.port}"
    named = [token.strip() for token in page.headers.get("referrer-policy", "").split(",") if token.strip() in POLICIES]
    policy = named[-1] if named else "strict-origin-when-cross-origin"
    if policy == "no-referrer":
        return "null"
    if policy == "same-origin":
        return origin if (there.scheme, there.host, there.port) == (here.scheme, here.host, here.port) else "null"
    if policy in ("no-referrer-when-downgrade", "strict-origin", "strict-origin-when-cross-origin"):
        return "null" if here.scheme == "https" and there.scheme != "https" else origin
    return origin


async def submitted(client, page_path, action, **request):
    """The page at page_path and the answer to submitting its form to action as a browser would, Origin and all."""
    page = await client.get(page_path)
    assert f'action="{action}"' in page.text, page.text
    return page, await client.post(action, headers={"origin": browser_origin(page, action)}, **request)


def source(tmp_path, name, content=b"synthetic lecture handout"):
    path = tmp_path / "inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


def staging_leftovers(admin):
    staging = admin.core.layout.staging
    return sorted(path.name for path in staging.iterdir()) if staging.exists() else []


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/"), ("GET", "/import"), ("POST", "/import"), ("GET", "/jobs"), ("POST", "/jobs/x/retry"), ("GET", "/attachments/x/original")],
)
async def test_a_page_without_a_session_or_the_token_is_refused_though_it_comes_from_127_0_0_1(tmp_path, method, path):
    async with hub(tmp_path) as (client, _):
        for headers in ({}, {"authorization": "Bearer wrong"}, {"cookie": f"{SESSION_COOKIE}=forged"}):
            response = await client.request(method, path, headers={**ORIGIN, **headers})

            assert response.status_code == 401
            assert "campuslife-hub open" in response.text


@pytest.mark.parametrize("path", ["/api/status", "/api/login"])
async def test_the_admin_api_takes_signed_requests_and_not_a_session(tmp_path, path):
    async with hub(tmp_path) as (client, _):
        await log_in(client)

        assert (await client.post(path, headers=ORIGIN)).status_code in (401, 405)
        assert (await client.get(path)).status_code in (401, 405)
        assert (await client.request("GET" if path == "/api/status" else "POST", path, auth=SIGNED)).status_code == 200


@pytest.mark.parametrize("host", ["campuslife.example:8765", "127.0.0.1:8766", "127.0.0.1", "hub.tailnet.ts.net"])
async def test_a_request_naming_another_host_is_refused_even_signed(tmp_path, host):
    async with hub(tmp_path) as (client, _):
        response = await client.get("/api/status", auth=SIGNED, headers={"host": host})

        assert response.status_code == 400


async def test_the_listener_answers_to_localhost_too(tmp_path):
    async with hub(tmp_path) as (client, _):
        assert (await client.get("/api/status", auth=SIGNED, headers={"host": f"localhost:{PORT}"})).status_code == 200


async def test_a_login_link_opens_one_browser_session_once(tmp_path):
    async with hub(tmp_path) as (client, _):
        url = (await client.post("/api/login", auth=SIGNED)).json()["url"]
        assert url.startswith(f"{BASE}/login?code=")

        first = await client.get(url)

        assert (first.status_code, first.headers["location"]) == (303, "/")
        cookie = first.headers["set-cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
        assert (await client.get("/")).status_code == 200
        client.cookies.clear()
        assert (await client.get(url)).status_code == 401
        assert (await client.get("/login")).status_code == 401


async def test_a_login_code_expires_and_so_does_a_session(tmp_path):
    clock = Clock()
    async with hub(tmp_path, clock=clock) as (client, _):
        late = (await client.post("/api/login", auth=SIGNED)).json()["url"]
        clock.now += LOGIN_CODE_LIFETIME
        assert (await client.get(late)).status_code == 401

        await log_in(client)
        clock.now += SESSION_LIFETIME - timedelta(seconds=1)
        assert (await client.get("/")).status_code == 200
        clock.now += timedelta(seconds=1)
        assert (await client.get("/")).status_code == 401


async def test_logging_out_ends_the_session(tmp_path):
    async with hub(tmp_path) as (client, _):
        await log_in(client)
        session = client.cookies[SESSION_COOKIE]

        response = await client.post("/logout", headers=ORIGIN)

        assert response.status_code == 200
        assert (await client.get("/", headers={"cookie": f"{SESSION_COOKIE}={session}"})).status_code == 401


@pytest.mark.parametrize(
    ("origin", "status"), [(None, 403), ("null", 403), ("http://campuslife.example", 403), (f"http://127.0.0.1:{PORT + 1}", 403), (BASE, 404)]
)
async def test_a_change_under_a_session_needs_this_listeners_origin(tmp_path, origin, status):
    async with hub(tmp_path) as (client, _):
        await log_in(client)

        response = await client.post("/jobs/no-such-job/cancel", headers={} if origin is None else {"origin": origin})

        assert response.status_code == status


async def test_a_request_from_another_origin_is_refused_even_with_the_token(tmp_path):
    async with hub(tmp_path) as (client, _):
        assert (await client.get("/api/status", auth=SIGNED, headers={"origin": "http://campuslife.example"})).status_code == 403


@pytest.mark.parametrize("path", ["/", "/jobs", "/import", "/login"])
async def test_every_page_and_refusal_carries_the_page_headers(tmp_path, path):
    async with hub(tmp_path) as (client, _):
        refused = await client.get(path, headers={"host": "campuslife.example"})
        await log_in(client)
        shown = await client.get(path)

    for response in (refused, shown):
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "same-origin"


@pytest.mark.parametrize(
    ("name", "disposition"),
    [("notes.html", 'attachment; filename="notes.html"'), ("자료구조.pdf", "attachment; filename*=utf-8''%EC%9E%90%EB%A3%8C%EA%B5%AC%EC%A1%B0.pdf")],
)
async def test_an_original_is_sent_as_a_download_that_cannot_run_in_this_origin(tmp_path, name, disposition):
    page = b"<script>document.title = 'ran'</script>"
    async with hub(tmp_path) as (client, admin):
        await import_files(admin.core, [source(tmp_path, name, page)])
        [capture] = await admin.core.db.read(list_captures)
        await log_in(client)

        response = await client.get(f"/attachments/{capture.attachments[0].id}/original")

    assert (response.status_code, response.content) == (200, page)
    assert response.headers["content-disposition"] == disposition
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"


async def test_an_unknown_attachment_or_a_missing_original_is_not_found(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await import_files(admin.core, [source(tmp_path, "week4.txt")])
        [capture] = await admin.core.db.read(list_captures)
        [attachment] = capture.attachments
        blob = admin.core.blobs.path_of(attachment.blob_sha256)
        blob.chmod(0o600)
        blob.unlink()
        await log_in(client)

        assert (await client.get("/attachments/no-such-attachment/original")).status_code == 404
        assert (await client.get(f"/attachments/{attachment.id}/original")).status_code == 404


async def test_the_captures_page_lists_captures_last_first_with_their_files(tmp_path):
    async with hub(tmp_path) as (client, admin):
        first = await import_files(admin.core, [source(tmp_path, "week3.txt")])
        dropped = source(tmp_path, "week4.txt", b"other synthetic handout")
        second = await import_files(admin.core, [dropped], names=["week4 <b>.txt"], cloud_policy="blocked")
        await log_in(client)

        page = (await client.get(f"/?imported={second.capture_id}")).text

    assert page.index("week4 &lt;b&gt;.txt") < page.index("week3.txt")
    assert "<b>" not in page
    assert f"Imported as Capture <code>{second.capture_id}</code>" in page
    assert first.capture_id not in page


def multipart(*files, cloud_policy=""):
    return {"files": [("files", (name, content, "application/octet-stream")) for name, content in files], "data": {"cloud_policy": cloud_policy}}


async def test_files_dropped_on_the_import_form_become_one_capture_and_leave_nothing_in_staging(tmp_path):
    async with hub(tmp_path) as (client, admin):
        woken = []
        admin.runner.wake = lambda: woken.append(True)
        await log_in(client)

        response = await client.post("/import", headers=ORIGIN, **multipart(("자료구조.pdf", b"%PDF synthetic"), ("week 4.txt", b"notes"), cloud_policy="blocked"))

        assert response.status_code == 303
        [capture] = await admin.core.db.read(list_captures)
        assert response.headers["location"] == f"/?imported={capture.id}"
        assert sorted((a.original_filename, a.cloud_policy) for a in capture.attachments) == [("week 4.txt", "blocked"), ("자료구조.pdf", "blocked")]
        assert admin.core.blobs.path_of(capture.attachments[1].blob_sha256).read_bytes() in (b"%PDF synthetic", b"notes")
        assert staging_leftovers(admin) == []
        assert woken == [True]


@pytest.mark.parametrize(
    ("form", "reason"),
    [
        ({"content": b"--x\r\nContent-Disposition: form-data; name=\"files\"; filename=\"\"\r\n\r\n\r\n--x--\r\n", "headers": {"content-type": "multipart/form-data; boundary=x"}}, "at least one file"),
        (multipart(("week4.txt", b"notes"), cloud_policy="cloud_redacted"), "cloud_policy is one of"),
        ({"data": {"cloud_policy": "blocked"}}, "multipart"),
        ({"content": b"not a form", "headers": {"content-type": "text/plain"}}, "multipart"),
        ({"content": b"--x\r\nContent-Disposition: form-data\r\n\r\nno name\r\n--x--\r\n", "headers": {"content-type": "multipart/form-data; boundary=x"}}, ""),
        ({"content": "--x\r\nContent-Disposition: form-data; name=\"files\"; filename=\"bad\xff.txt\"\r\n\r\nx\r\n--x--\r\n".encode("latin-1"), "headers": {"content-type": "multipart/form-data; boundary=x"}}, ""),
    ],
    ids=["no files", "unknown policy", "urlencoded", "not a form", "part without a name", "name not UTF-8"],
)
async def test_an_import_form_that_cannot_be_imported_is_refused_and_leaves_nothing(tmp_path, form, reason):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        headers = {**ORIGIN, **form.pop("headers", {})}

        response = await client.post("/import", headers=headers, **form)

        assert response.status_code == 400
        assert "Import refused" in response.text and reason in response.text
        assert await admin.core.db.read(list_captures) == []
        assert staging_leftovers(admin) == []


async def queued(admin, n, *, failed=False):
    spec = JobSpec("extract.text", f"attachment:{n}", ("a" * 64,), {"n": n})
    job = await admin.core.db.write(lambda tx: jobs.enqueue(tx, spec, EXTRACT))
    if failed:
        now = datetime.now(UTC)
        await admin.core.db.write(lambda tx: jobs.claim(tx, ["extract.text"], now=now))
        job = await admin.core.db.write(lambda tx: jobs.finish(tx, job.id, Failed(f"broken with {TOKEN}")))
    return job


async def test_the_jobs_page_lists_jobs_with_retry_and_cancel_and_masks_secrets_in_errors(tmp_path):
    async with hub(tmp_path) as (client, admin):
        failed = await queued(admin, 1, failed=True)
        waiting = await queued(admin, 2)
        await log_in(client)

        page = (await client.get("/jobs")).text

    assert f'action="/jobs/{failed.id}/retry"' in page and f'action="/jobs/{failed.id}/cancel"' not in page
    assert f'action="/jobs/{waiting.id}/cancel"' in page and f'action="/jobs/{waiting.id}/retry"' not in page
    assert TOKEN not in page
    assert "broken with [redacted]" in page


async def test_a_failed_job_whose_blob_went_has_no_retry(tmp_path):
    async with hub(tmp_path) as (client, admin):
        failed = await queued(admin, 1, failed=True)
        await admin.core.db.write(lambda tx: tx.execute("UPDATE processing_jobs SET job_key = ? WHERE id = ?", (f"{jobs.RELEASED}{failed.id}", failed.id)))
        await log_in(client)

        page = (await client.get("/jobs")).text
        retried = await client.post(f"/jobs/{failed.id}/retry", headers=ORIGIN)

    assert f'action="/jobs/{failed.id}/retry"' not in page
    assert retried.status_code == 409


async def test_retry_and_cancel_from_the_jobs_page_change_the_job(tmp_path):
    async with hub(tmp_path) as (client, admin):
        failed = await queued(admin, 1, failed=True)
        waiting = await queued(admin, 2)
        await log_in(client)

        retried = await client.post(f"/jobs/{failed.id}/retry", headers=ORIGIN)
        cancelled = await client.post(f"/jobs/{waiting.id}/cancel", headers=ORIGIN)
        again = await client.post(f"/jobs/{waiting.id}/retry", headers=ORIGIN)

        assert (retried.status_code, retried.headers["location"], cancelled.status_code) == (303, "/jobs", 303)
        assert again.status_code == 409
        states = [await admin.core.db.read(lambda snap, job=job: jobs.get(snap, job.id).state) for job in (failed, waiting)]
        assert states == [JobState.PENDING, JobState.CANCELLED]


async def test_retry_and_cancel_through_the_admin_api(tmp_path):
    async with hub(tmp_path) as (client, admin):
        failed = await queued(admin, 1, failed=True)
        waiting = await queued(admin, 2)

        retried = await client.post(f"/api/jobs/{failed.id}/retry", auth=SIGNED)
        cancelled = await client.post(f"/api/jobs/{waiting.id}/cancel", auth=SIGNED)
        refused = await client.post(f"/api/jobs/{waiting.id}/retry", auth=SIGNED)
        unknown = await client.post("/api/jobs/no-such-job/cancel", auth=SIGNED)

    assert (retried.status_code, retried.json()) == (200, {"id": failed.id, "state": "pending"})
    assert (cancelled.status_code, cancelled.json()) == (200, {"id": waiting.id, "state": "cancelled"})
    assert (refused.status_code, unknown.status_code) == (409, 404)


async def test_the_cli_imports_local_paths_through_the_admin_api(tmp_path):
    async with hub(tmp_path) as (client, admin):
        paths = [str(source(tmp_path, "week4.txt")), str(source(tmp_path, "week5.txt", b"more synthetic notes"))]

        response = await client.post("/api/import", auth=SIGNED, json={"paths": paths, "cloud_policy": "local_only"})

        assert response.status_code == 200
        [capture] = await admin.core.db.read(list_captures)
        assert response.json() == {"capture_id": capture.id, "result": {"mutation_id": response.json()["result"]["mutation_id"], "result": "applied"}}
        assert sorted(a.original_filename for a in capture.attachments) == ["week4.txt", "week5.txt"]


@pytest.mark.parametrize(
    "body",
    [
        {"paths": []}, {"paths": "week4.txt"}, {"paths": ["missing.txt"]}, {"paths": [7]}, {"paths": ["week4.txt"], "cloud_policy": "open"},
        {"paths": ["week4.txt"], "links": "item:1"}, {"paths": ["week4.txt"], "links": [7]}, [], None,
    ],
    ids=["no paths", "paths not a list", "missing file", "path not a str", "unknown policy", "links not a list", "link not a str", "a list", "null"],
)
async def test_an_api_import_that_cannot_be_imported_is_refused(tmp_path, body):
    handout = str(source(tmp_path, "week4.txt"))
    if isinstance(body, dict) and isinstance(body["paths"], list):
        body["paths"] = [handout if path == "week4.txt" else path for path in body["paths"]]
    async with hub(tmp_path) as (client, admin):
        response = await client.post("/api/import", auth=SIGNED, content=json.dumps(body))

        assert response.status_code == 400
        assert await admin.core.db.read(list_captures) == []


async def test_the_status_reports_the_hub_its_jobs_and_what_the_host_adds(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await queued(admin, 1, failed=True)
        await queued(admin, 2)

        status = (await client.get("/api/status", auth=SIGNED)).json()

    assert status["hub_id"] == admin.core.identity.hub_id
    assert status["data_root"] == str(admin.core.layout.root)
    assert (status["jobs"]["failed"], status["jobs"]["pending"], status["jobs"]["running"]) == (1, 1, 0)
    assert status["runner"] == {"restarts": 0}


async def test_routes_other_code_adds_pass_the_same_guard_and_a_failure_in_one_shows_no_detail(tmp_path):
    devices = APIRouter()

    @devices.get("/devices/fails")
    async def fails():
        raise RuntimeError(f"failed holding {TOKEN}")

    async with hub(tmp_path, routers=[devices], raise_app_exceptions=False) as (client, _):
        assert (await client.get("/devices/fails")).status_code == 401
        assert (await client.get("/devices/fails", headers={"host": "campuslife.example"})).status_code == 400
        await log_in(client)

        response = await client.get("/devices/fails")

    assert response.status_code == 500
    assert TOKEN not in response.text and "failed holding" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_stop_through_the_admin_api_asks_the_host_to_stop(tmp_path):
    stops = []
    async with hub(tmp_path, stop=lambda: stops.append(True)) as (client, _):
        await log_in(client)
        assert (await client.post("/api/stop", headers=ORIGIN)).status_code == 401

        response = await client.post("/api/stop", auth=SIGNED)

    assert (response.status_code, response.json(), stops) == (200, {"stopping": True}, [True])


@pytest.mark.parametrize("path", ["/no-such-page", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/api/no-such-call"])
async def test_a_path_no_route_serves_is_guarded_and_fastapi_documents_nothing(tmp_path, path):
    async with hub(tmp_path) as (client, _):
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, auth=SIGNED, headers={"host": "campuslife.example"})).status_code == 400
        await log_in(client)

        response = await client.get(path, auth=SIGNED)

    assert response.status_code == 404
    assert response.headers["x-frame-options"] == "DENY"


async def test_an_error_a_route_raises_is_answered_behind_the_guard(tmp_path):
    devices = APIRouter()

    @devices.get("/devices/teapot")
    async def teapot():
        raise HTTPException(status_code=418, detail="short and stout")

    async with hub(tmp_path, routers=[devices]) as (client, _):
        assert (await client.get("/devices/teapot")).status_code == 401
        await log_in(client)

        response = await client.get("/devices/teapot")

    assert response.status_code == 418
    assert response.headers["content-security-policy"] == admin_auth.PAGE_HEADERS["content-security-policy"]


async def hello(client, nonce=None, *, headers=None):
    nonce = nonce or secrets.token_hex(32)
    return nonce, await client.post("/api/hello", headers=headers, json={"nonce": nonce, "mac": hello_mac(TOKEN, ADDRESS, nonce)})


async def signature(client, method, path, body=b"", *, query="", token=TOKEN, address=ADDRESS):
    _, answer = await hello(client)
    target = f"{path}?{query}" if query else path
    return {"authorization": signed_authorization(token, address, answer.json()["nonce"], method, target, body)}


async def test_the_hub_proves_it_holds_the_token_to_a_client_that_proves_it_too(tmp_path):
    async with hub(tmp_path) as (client, _):
        nonce, answer = await hello(client)
        upper = nonce.upper()
        refused = [
            (await client.post("/api/hello", json=body)).status_code
            for body in (
                {"nonce": "f" * 63, "mac": hello_mac(TOKEN, ADDRESS, "f" * 63)},
                {"nonce": upper, "mac": hello_mac(TOKEN, ADDRESS, upper)},
                {"nonce": nonce, "mac": hello_mac(secrets.token_urlsafe(32), ADDRESS, nonce)},
                {"nonce": nonce, "mac": hello_mac(TOKEN, ELSEWHERE, nonce)},
                {"nonce": nonce, "mac": 7},
                {"nonce": nonce},
                {},
                [nonce],
            )
        ]

    assert answer.status_code == 200
    assert answer.json()["proof"] == hub_proof(TOKEN, ADDRESS, nonce, answer.json()["nonce"])
    assert TOKEN not in answer.text
    assert refused == [401] * 8


async def test_the_hub_proves_itself_and_takes_signatures_only_for_the_port_it_listens_on_whatever_host_is_named(tmp_path):
    async with hub(tmp_path) as (client, _):
        nonce, answer = await hello(client, headers={"host": f"localhost:{PORT}"})
        refused = [
            await client.post("/api/login", headers=await signature(client, "POST", "/api/login", address=ELSEWHERE)),
            await client.get("/api/status", headers=await signature(client, "GET", "/api/status", address=f"localhost:{PORT}")),
        ]
        login = await client.post("/api/login", headers={"host": f"localhost:{PORT}", **await signature(client, "POST", "/api/login")})

    assert answer.json()["proof"] == hub_proof(TOKEN, ADDRESS, nonce, answer.json()["nonce"])
    assert [response.status_code for response in refused] == [401, 401]
    assert login.json()["url"].startswith(f"http://{ADDRESS}/login?code=")


async def test_a_signed_request_is_taken_once_and_only_as_it_was_signed(tmp_path):
    async with hub(tmp_path) as (client, admin):
        once = await signature(client, "GET", "/api/status")
        assert (await client.get("/api/status", headers=once)).status_code == 200
        body = json.dumps({"paths": [str(source(tmp_path, "a.txt"))]}).encode()
        other = json.dumps({"paths": [str(source(tmp_path, "b.txt"))]}).encode()

        refused = [
            await client.get("/api/status", headers=once),
            await client.post("/api/login", headers=await signature(client, "POST", "/api/status")),
            await client.post("/api/login", headers=await signature(client, "GET", "/api/login")),
            await client.get("/api/status", params={"x": "1"}, headers=await signature(client, "GET", "/api/status")),
            await client.post("/api/import", content=other, headers=await signature(client, "POST", "/api/import", body)),
            await client.post("/api/import", content=body, headers=await signature(client, "POST", "/api/import", body, token=secrets.token_urlsafe(32))),
            await client.post("/api/import", content=body, headers={"authorization": f"Bearer {TOKEN}"}),
            await client.post("/api/import", content=body, headers={"authorization": f"CampusLife {TOKEN}"}),
        ]

        assert [response.status_code for response in refused] == [401] * len(refused)
        assert await admin.core.db.read(list_captures) == []


async def test_a_nonce_of_the_hubs_signs_only_within_its_lifetime_and_while_few_wait(tmp_path, monkeypatch):
    clock = Clock()
    async with hub(tmp_path, clock=clock) as (client, _):
        late = await signature(client, "GET", "/api/status")
        clock.now += HELLO_LIFETIME
        assert (await client.get("/api/status", headers=late)).status_code == 401

        monkeypatch.setattr(admin_auth, "MAX_PENDING_HELLOS", 2)
        oldest, kept = await signature(client, "GET", "/api/status"), await signature(client, "GET", "/api/status")
        await hello(client)

        assert (await client.get("/api/status", headers=oldest)).status_code == 401
        assert (await client.get("/api/status", headers=kept)).status_code == 200


async def test_a_client_that_signs_as_the_admin_auth_docstring_says_is_answered(tmp_path):
    def mac(purpose, *parts):
        return hmac.new(TOKEN.encode(), "\0".join((purpose, *parts)).encode(), hashlib.sha256).hexdigest()

    async def signed(client, method, target, body=b""):
        nonce = secrets.token_hex(32)
        answer = (await client.post("/api/hello", json={"nonce": nonce, "mac": mac("campuslife-admin-hello", ADDRESS, nonce)})).json()
        assert answer["proof"] == mac("campuslife-hub-proof", ADDRESS, nonce, answer["nonce"])
        path, _, query = target.partition("?")
        path = path.replace("%20", " ")
        signature = mac("campuslife-admin-request", ADDRESS, answer["nonce"], method, path, query, hashlib.sha256(body).hexdigest())
        return await client.request(method, target, content=body, headers={"authorization": f"CampusLife {answer['nonce']}.{signature}"})

    body = json.dumps({"paths": [str(source(tmp_path, "주간 노트.txt"))]}, ensure_ascii=False).encode()
    async with hub(tmp_path) as (client, _):
        responses = [
            await signed(client, "POST", "/api/import", body),
            await signed(client, "GET", "/api/status?week=4%20notes&x="),
            await signed(client, "POST", "/api/jobs/no%20such%20job/retry"),
        ]

    assert [response.status_code for response in responses] == [200, 200, 404]


def without_raw_path(app):
    async def served(scope, receive, send):
        await app({name: value for name, value in scope.items() if name != "raw_path"}, receive, send)

    return served


@pytest.mark.parametrize("served_as", [lambda app: app, without_raw_path], ids=["raw path given", "no raw path"])
async def test_a_request_signed_for_a_percent_encoded_target_is_taken(tmp_path, served_as):
    async with hub(tmp_path, served_as=served_as) as (client, _):
        responses = [
            await client.get("/api/st%61tus", params={"week": "4 notes"}, auth=SIGNED),
            await client.post("/api/jobs/no%20such%20job%2541-%EA%B3%BC%EC%A0%9C/retry", auth=SIGNED),
        ]

    assert [response.status_code for response in responses] == [200, 404]


async def test_hellos_from_a_program_without_the_token_do_not_push_out_the_nonce_a_client_waits_on(tmp_path, monkeypatch):
    async with hub(tmp_path) as (client, _):
        monkeypatch.setattr(admin_auth, "MAX_PENDING_HELLOS", 2)
        waiting = await signature(client, "GET", "/api/status")
        anonymous = [(await client.post("/api/hello", json={"nonce": secrets.token_hex(32)})).status_code for _ in range(3)]

        assert (await client.get("/api/status", headers=waiting)).status_code == 200
        assert anonymous == [401] * 3


@pytest.mark.parametrize(("method", "path"), [("POST", "/api/hello"), ("GET", "/login"), ("GET", "/static/admin.css")])
async def test_a_request_before_login_carries_at_most_a_kibibyte(tmp_path, method, path):
    pulled = []

    async def endless(chunk=512):
        for _ in range(4096):
            pulled.append(chunk)
            yield b"x" * chunk

    async with hub(tmp_path) as (client, _):
        kibibyte = await client.request(method, path, content=b"x" * 1024)
        response = await client.request(method, path, content=endless())

    assert kibibyte.status_code != 413
    assert response.status_code == 413
    assert sum(pulled) <= 1024 + 512


async def test_a_page_request_that_carries_an_authorization_header_is_not_read_by_the_guard(tmp_path):
    big = b"x" * (MAX_SIGNED_BODY_BYTES + 1)
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        response = await client.post("/import", headers={**ORIGIN, "authorization": "CampusLife x.y"}, **multipart(("big.txt", big), cloud_policy="blocked"))
        captures = await admin.core.db.read(list_captures)

    assert response.status_code == 303
    assert len(captures) == 1


async def test_fastapi_records_nothing_of_a_request_even_where_opentelemetry_is_set_up(tmp_path, monkeypatch):
    spans = []

    class Recording(trace.NoOpTracer):
        def start_span(self, name, *args, attributes=None, **options):
            spans.append((name, attributes))
            return trace.INVALID_SPAN

    class SetUp(trace.TracerProvider):
        def get_tracer(self, *args, **options):
            return Recording()

    monkeypatch.setattr(trace, "get_tracer_provider", SetUp)
    async with hub(tmp_path) as (client, _):
        await log_in(client)
        assert (await client.get("/")).status_code == 200

    assert spans == []


async def test_a_signed_request_past_one_mebibyte_is_refused(tmp_path):
    body = b"x" * (MAX_SIGNED_BODY_BYTES + 1)
    async with hub(tmp_path) as (client, _):
        response = await client.post("/api/import", content=body, headers=await signature(client, "POST", "/api/import", body))

    assert response.status_code == 413


async def test_a_malformed_cookie_another_app_on_127_0_0_1_set_does_not_hide_the_session(tmp_path):
    async with hub(tmp_path) as (client, _):
        await log_in(client)
        session = client.cookies[SESSION_COOKIE]
        client.cookies.clear()

        response = await client.get("/", headers={"cookie": f"x=a b; {SESSION_COOKIE}={session}"})

    assert response.status_code == 200


def planted(name):
    return f'"><meta name="{name}"><a href="http://evil.example/{name}">'


async def test_what_the_captures_page_shows_is_escaped(tmp_path, monkeypatch):
    attachment = AttachmentSummary(planted("attachment"), "a" * 64, planted("file"), planted("policy"))
    capture = CaptureSummary("c", planted("taken"), planted("origin"), [attachment])
    monkeypatch.setattr(admin_app, "latest_captures", lambda snap, limit: [capture])
    async with hub(tmp_path) as (client, _):
        await log_in(client)

        page = (await client.get("/", params={"imported": planted("imported")})).text

    for name in ("attachment", "file", "policy", "taken", "origin", "imported"):
        assert f'<meta name="{name}">' not in page and f'href="http://evil.example/{name}"' not in page
        assert f"&lt;meta name=&quot;{name}&quot;&gt;" in page


async def test_what_the_jobs_page_shows_is_escaped(tmp_path):
    async with hub(tmp_path) as (client, admin):
        spec = JobSpec("extract.text", f"attachment:{planted('subject')}", ("a" * 64,))
        job = await admin.core.db.write(lambda tx: jobs.enqueue(tx, spec, EXTRACT))
        await admin.core.db.write(lambda tx: jobs.claim(tx, ["extract.text"], now=datetime.now(UTC)))
        await admin.core.db.write(lambda tx: jobs.finish(tx, job.id, Failed(planted("error"))))
        await log_in(client)

        page = (await client.get("/jobs")).text

    for name in ("subject", "error"):
        assert f'<meta name="{name}">' not in page and f'href="http://evil.example/{name}"' not in page
        assert f"&lt;meta name=&quot;{name}&quot;&gt;" in page


async def test_imports_and_retries_wake_the_runner(tmp_path):
    async with hub(tmp_path) as (client, admin):
        woken = []
        admin.runner.wake = lambda: woken.append(True)
        first, second = await queued(admin, 1, failed=True), await queued(admin, 2, failed=True)

        imported = await client.post("/api/import", auth=SIGNED, json={"paths": [str(source(tmp_path, "week4.txt"))]})
        assert (imported.status_code, len(woken)) == (200, 1)
        assert (await client.post(f"/api/jobs/{first.id}/retry", auth=SIGNED)).status_code == 200
        assert len(woken) == 2
        await log_in(client)
        assert (await client.post(f"/jobs/{second.id}/retry", headers=ORIGIN)).status_code == 303
        assert len(woken) == 3


async def test_an_api_import_the_disk_refuses_is_refused_with_the_reason(tmp_path):
    async with hub(tmp_path) as (client, admin):
        async def full(path):
            raise OSError(28, "No space left on device")

        admin.core.blobs.put_file = full

        response = await client.post("/api/import", auth=SIGNED, json={"paths": [str(source(tmp_path, "week4.txt"))]})

        assert response.status_code == 400
        assert "No space left on device" in response.json()["error"]
        assert await admin.core.db.read(list_captures) == []


async def test_the_forms_of_the_local_ui_work_with_the_origin_a_browser_sends_them_with(tmp_path):
    drive = tmp_path / "drive"
    async with hub(tmp_path) as (client, _):
        await log_in(client)
        _, backed_up = await submitted(client, "/backup", "/backup", data={"target": str(drive)})
        _, logged_out = await submitted(client, "/", "/logout")

    assert (backed_up.status_code, logged_out.status_code) == (200, 200)
    assert len(backup.generations(drive)) == 1


async def test_the_backup_page_warns_before_the_first_backup_and_backs_up_to_the_folder_given(tmp_path):
    drive = tmp_path / "drive"
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        await import_files(admin.core, [source(tmp_path, "week1.pdf")])

        before = await client.get("/backup")
        made = await client.post("/backup", data={"target": str(drive)}, headers=ORIGIN)
        after = await client.get("/backup")

    assert "No backup yet" in before.text
    assert made.status_code == 200
    [generation] = backup.generations(drive)
    assert generation in made.text and "1 Blobs, 1 copied" in made.text
    assert "No backup yet" not in after.text and "warning" not in after.text
    assert f'value="{drive.resolve()}"' in after.text


async def test_a_backup_from_the_local_ui_keeps_as_many_generations_as_the_hub_is_set_to(tmp_path):
    drive = tmp_path / "drive"
    async with hub(tmp_path, backup_settings=backup.BackupSettings(keep=1)) as (client, admin):
        await log_in(client)
        page = await client.get("/backup")
        first = await client.post("/backup", data={"target": str(drive)}, headers=ORIGIN)
        second = await client.post("/backup", data={"target": str(drive)}, headers=ORIGIN)

    [kept] = backup.generations(drive)
    assert "newest 1 generations" in page.text
    assert kept in second.text and "Removed the older generations" in second.text
    assert kept not in first.text


async def test_the_captures_page_warns_once_the_last_backup_is_older_than_the_warning_period(tmp_path):
    clock = Clock()
    clock.now = datetime.now(UTC)
    async with hub(tmp_path, clock=clock, backup_settings=backup.BackupSettings(keep=5, warn_after=timedelta(days=3))) as (client, admin):
        await log_in(client)
        never = await client.get("/")
        await backup.back_up(admin.core, tmp_path / "drive")
        fresh = await client.get("/")
        clock.now += timedelta(days=3, minutes=1)
        await log_in(client)
        stale = await client.get("/")

    assert "No backup yet" in never.text
    assert 'class="warning"' not in fresh.text
    assert 'class="warning"' in stale.text and "more than 3 days" in stale.text


async def test_the_admin_api_backs_up_and_the_status_reports_the_last_backup(tmp_path):
    drive = tmp_path / "drive"
    async with hub(tmp_path) as (client, admin):
        before = (await client.get("/api/status", auth=SIGNED)).json()["backup"]
        made = await client.post("/api/backup", json={"target": str(drive), "keep": 2}, auth=SIGNED)
        after = (await client.get("/api/status", auth=SIGNED)).json()["backup"]

    assert before == {"last": None, "overdue": True, "warn_after_days": 7, "keep": 5}
    assert made.status_code == 200
    assert made.json()["generation"] == backup.generations(drive)[0]
    assert after["last"]["generation"] == made.json()["generation"] and after["overdue"] is False


@pytest.mark.parametrize("body", [{"target": "inside"}, {"target": ""}, {"target": "elsewhere", "keep": 0}, {}], ids=["data root", "empty", "keep 0", "none"])
async def test_a_backup_the_hub_cannot_make_is_refused_with_the_reason(tmp_path, body):
    async with hub(tmp_path) as (client, admin):
        places = {"inside": str(admin.core.layout.root / "backup"), "elsewhere": str(tmp_path / "drive"), "": ""}
        response = await client.post("/api/backup", json={**body, **({"target": places[body["target"]]} if "target" in body else {})}, auth=SIGNED)

    assert response.status_code == 400
    assert response.json()["error"]
    assert not (tmp_path / "drive").exists()


async def a_fork_note(admin, tmp_path):
    """An imported PDF, an Artifact made from it and a Note forked from that Artifact."""
    await import_files(admin.core, [source(tmp_path, "week3.pdf", b"%PDF synthetic week 3")])
    [(attachment_id, sha)] = await admin.core.db.read(lambda snap: snap.execute("SELECT id, blob_sha256 FROM attachments").fetchall())
    job = await admin.core.db.write(lambda tx: jobs.enqueue(tx, JobSpec("extract.text", f"attachment:{attachment_id}", (sha,), {}), EXTRACT))
    content = (await admin.core.blobs.put_file(source(tmp_path, "text.txt", b"week 3 text"))).sha256
    artifact = await admin.core.db.write(lambda tx: artifacts.record(tx, job, "ocr", content))
    note_id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    create = {"mutation_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301", "op": "create", "entity": "item", "id": note_id,
              "data": {"kind": "note", "title": "3주차 <정리>", "fields": {"forked_from_artifact": artifact["id"]}}}
    assert (await mutations.apply(admin.core.db, [create], admin.core.hub_device))[0]["result"] == "applied"
    return sha, note_id


async def test_the_purge_page_shows_what_goes_asks_about_fork_notes_and_says_backups_still_hold_it(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        sha, note_id = await a_fork_note(admin, tmp_path)

        captures = await client.get("/")
        page = await client.get(f"/blobs/{sha}/purge")

    assert f'href="/blobs/{sha}/purge"' in captures.text
    assert page.status_code == 200
    assert "week3.pdf" in page.text and "1 Artifacts" in page.text
    assert f'name="delete_note" value="{note_id}"' in page.text and "3주차 &lt;정리&gt;" in page.text
    assert "Delete these Notes too?" in page.text
    assert "backup generations" in page.text.lower()


@pytest.mark.parametrize("chosen", [True, False], ids=["notes chosen", "notes kept"])
async def test_purging_from_the_local_ui_removes_the_blob_and_the_notes_chosen(tmp_path, chosen):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        sha, note_id = await a_fork_note(admin, tmp_path)

        response = await client.post(f"/blobs/{sha}/purge", data={"delete_note": note_id} if chosen else {}, headers=ORIGIN)

        note = await admin.core.db.read(lambda snap: snap.execute("SELECT data FROM items WHERE id = ?", (note_id,)).fetchone())
        attachments = await admin.core.db.read(lambda snap: snap.execute("SELECT count(*) FROM attachments").fetchone())
        assert not admin.core.blobs.path_of(sha).exists()

    assert response.status_code == 200 and "Purged" in response.text
    assert attachments == (0,)
    assert (note is None) is chosen
    if not chosen:
        assert "evidence_purged_at" in json.loads(note[0])


async def test_purging_from_the_local_ui_stops_a_job_running_on_what_goes(tmp_path):
    started, stopped = asyncio.Event(), []

    async def slow(job, context):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopped.append(job.id)
            raise

    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        await import_files(admin.core, [source(tmp_path, "week3.pdf", b"%PDF synthetic week 3")])
        [(attachment_id, sha)] = await admin.core.db.read(lambda snap: snap.execute("SELECT id, blob_sha256 FROM attachments").fetchall())
        registry = admin.runner.context.registry
        registry.handle("ocr", Pipeline("ocr-1"), slow)
        job = await admin.core.db.write(lambda tx: registry.enqueue(tx, JobSpec("ocr", f"attachment:{attachment_id}", (sha,), {})))
        running = asyncio.create_task(admin.runner.run_due())
        await asyncio.wait_for(started.wait(), 10)

        response = await client.post(f"/blobs/{sha}/purge", headers=ORIGIN)

        assert stopped == [job.id]
        await asyncio.wait_for(running, 10)
        state = await admin.core.db.read(lambda snap: jobs.get(snap, job.id).state)
    assert response.status_code == 200
    assert state is JobState.CANCELLED


@pytest.mark.parametrize("blob", ["e" * 64, "not-a-sha256"])
async def test_a_purge_page_for_a_blob_the_hub_does_not_hold_is_not_found(tmp_path, blob):
    async with hub(tmp_path) as (client, _):
        await log_in(client)

        assert (await client.get(f"/blobs/{blob}/purge")).status_code == 404
        assert (await client.post(f"/blobs/{blob}/purge", headers=ORIGIN)).status_code == 404
