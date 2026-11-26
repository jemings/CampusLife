import pytest
from conftest import contract_fixture
from test_capture_commit import apply, device, feed, new_id

__all__ = ["device"]

ITEM = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def mutation(case, **members):
    return {**contract_fixture("valid", "mutation/mutation", case), "mutation_id": new_id(), **members}


def rows(changes, entity):
    return {row["id"]: row for row in changes if row["entity"] == entity}


@pytest.mark.parametrize("case", ["create_course", "create_task", "create_all_day_exam"])
async def test_a_created_item_reaches_the_feed_with_its_id_schema_version_and_rev(db, device, case):
    create = mutation(case)

    assert await apply(db, device, create) == [{"mutation_id": create["mutation_id"], "result": "applied"}]

    [row] = rows(await feed(db), "item").values()
    assert row["data"] == {"id": create["id"], "schema_version": 1, **create["data"], "rev": row["rev"]}
    assert row["deleted"] is False


async def test_the_same_create_under_another_mutation_id_is_applied_and_writes_nothing(db, device):
    create = mutation("create_task")
    await apply(db, device, create)
    before = await feed(db)

    again = {**create, "mutation_id": new_id()}
    assert await apply(db, device, again) == [{"mutation_id": again["mutation_id"], "result": "applied"}]

    assert await feed(db) == before


async def test_a_create_reusing_an_item_id_with_other_content_is_id_reuse(db, device):
    await apply(db, device, mutation("create_task"))
    other = mutation("create_course", id=ITEM)

    assert await apply(db, device, other) == [{"mutation_id": other["mutation_id"], "result": "rejected", "reason": "id_reuse"}]
    assert rows(await feed(db), "item")[ITEM]["data"]["kind"] == "task"


async def test_an_item_kind_this_hub_has_no_fields_schema_for_is_rejected_as_schema(db, device):
    create = mutation("create_unknown_kind")

    assert await apply(db, device, create) == [{"mutation_id": create["mutation_id"], "result": "rejected", "reason": "schema"}]
    assert await feed(db) == []


@pytest.mark.parametrize("body", ["첫 줄\r\n둘째 줄", "첫 줄\r둘째 줄"])
async def test_a_body_with_a_carriage_return_is_not_canonical(db, device, body):
    create = mutation("create_task")
    create["data"]["body"] = body

    assert await apply(db, device, create) == [{"mutation_id": create["mutation_id"], "result": "rejected", "reason": "not_canonical"}]
    assert await feed(db) == []
