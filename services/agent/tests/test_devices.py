from campuslife_contracts import validate
from campuslife_hub.devices import ensure_hub_device


async def test_the_hub_has_one_device_row_of_its_own(db):
    first = await db.write(ensure_hub_device)
    second = await db.write(ensure_hub_device)
    assert first == second
    assert validate("common/uuid", first) == []
