import pytest
from campuslife_contracts import validate

from campuslife_hub.data_root import Layout
from campuslife_hub.db import Database, NotInitialized
from campuslife_hub.identity import ensure_identity, read_identity
from campuslife_hub.lock import HubLock


async def test_identity_is_two_uuid4s(db):
    identity = await db.read(read_identity)
    assert validate("common/uuid", identity.hub_id) == []
    assert validate("common/uuid", identity.epoch) == []
    assert identity.hub_id != identity.epoch


async def test_issuing_again_keeps_the_identity(db):
    before = await db.read(read_identity)
    again = await db.write(ensure_identity)
    assert again == before == await db.read(read_identity)


async def test_reading_an_identity_never_issued_is_refused(tmp_path):
    layout = Layout(tmp_path)
    with HubLock.acquire(layout.lock_file) as lock:
        bare = await Database.open(layout, lock=lock, create=True)
        try:
            with pytest.raises(NotInitialized, match="init"):
                await bare.read(read_identity)
        finally:
            await bare.close()
