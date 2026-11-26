import asyncio
import inspect
import json
import secrets

import httpx
import pytest

from campuslife_contracts import contract_root
from campuslife_hub.admin_auth import HELLO_PATH, hub_proof, signed_authorization
from campuslife_hub.data_root import Layout
from campuslife_hub.db import Database
from campuslife_hub.identity import ensure_identity
from campuslife_hub.lock import HubLock


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run `async def` tests to completion on a fresh event loop."""
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    arguments = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(pyfuncitem.obj(**arguments))
    return True


class SignedAs(httpx.Auth):
    """Signs each admin API request with the admin token as the CLI does, once the Hub has proved it holds it."""

    requires_response_body = True

    def __init__(self, token):
        self.token = token

    def auth_flow(self, request):
        client_nonce = secrets.token_hex(32)
        same_origin = {name: value for name, value in request.headers.items() if name in ("host", "origin")}
        answer = yield httpx.Request("POST", request.url.join(HELLO_PATH), headers=same_origin, json={"nonce": client_nonce})
        if answer.status_code == 200:
            hub_nonce = answer.json()["nonce"]
            assert answer.json()["proof"] == hub_proof(self.token, client_nonce, hub_nonce)
            request.headers["authorization"] = signed_authorization(
                self.token, hub_nonce, request.method, request.url.path, request.url.query.decode(), request.content
            )
        yield request


def contract_fixture(kind: str, schema_id: str, case: str):
    path = contract_root() / "fixtures" / kind / schema_id / f"{case}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path):
    """A migrated Hub database with its identity issued, opened under its own hub.lock."""
    layout = Layout(tmp_path)
    with HubLock.acquire(layout.lock_file) as lock:
        database = asyncio.run(Database.open(layout, lock=lock, create=True))
        asyncio.run(database.write(ensure_identity))
        yield database
        asyncio.run(database.close())
