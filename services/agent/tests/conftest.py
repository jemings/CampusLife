import asyncio
import inspect
import json

import pytest

from campuslife_contracts import contract_root
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
