import pytest
from conftest import contract_fixture
from test_capture_commit import apply, blob_store, device, feed, new_id, store_bytes

__all__ = ["blob_store", "device"]

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


TAG = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER_ITEM = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"


def create_item(item_id, title="자료구조"):
    return {"mutation_id": new_id(), "op": "create", "entity": "item", "id": item_id, "data": {"kind": "note", "title": title, "fields": {}}}


def relation(source, kind, target):
    relation_id = f"{source}|{kind}|{target}"
    return {"mutation_id": new_id(), "op": "create", "entity": "relation", "id": relation_id, "data": {"from": source, "type": kind, "to": target}}


async def test_a_created_tag_reaches_the_feed_and_its_resend_writes_nothing(db, device):
    create = mutation("create_tag")

    assert (await apply(db, device, create))[0]["result"] == "applied"
    before = await feed(db)
    assert rows(before, "tag")[TAG]["data"] == {"id": TAG, **create["data"]}
    assert (await apply(db, device, {**create, "mutation_id": new_id()}))[0]["result"] == "applied"
    assert await feed(db) == before


async def test_a_create_reusing_a_tag_id_with_other_content_is_id_reuse(db, device):
    await apply(db, device, mutation("create_tag"))
    other = mutation("create_tag")
    other["data"]["color"] = "#000000"

    assert (await apply(db, device, other))[0].get("reason") == "id_reuse"


async def test_a_relation_between_existing_items_reaches_the_feed(db, device):
    await apply(db, device, create_item(ITEM), create_item(OTHER_ITEM))
    create = relation(f"item:{ITEM}", "about", f"item:{OTHER_ITEM}")

    assert (await apply(db, device, create))[0]["result"] == "applied"

    assert rows(await feed(db), "relation")[create["id"]]["data"] == {"id": create["id"], **create["data"]}


async def test_tagging_an_item_is_a_tagged_relation_to_an_existing_tag(db, device):
    await apply(db, device, create_item(OTHER_ITEM), mutation("create_tag"))
    tagging = mutation("create_relation_tagged")

    assert (await apply(db, device, tagging))[0]["result"] == "applied"

    assert rows(await feed(db), "relation")[tagging["id"]]["data"] == {"id": tagging["id"], **tagging["data"]}


async def test_creating_an_existing_relation_again_is_applied_and_writes_nothing(db, device):
    await apply(db, device, create_item(ITEM), create_item(OTHER_ITEM))
    create = relation(f"item:{ITEM}", "about", f"item:{OTHER_ITEM}")
    await apply(db, device, create)
    before = await feed(db)

    assert (await apply(db, device, {**create, "mutation_id": new_id()}))[0]["result"] == "applied"
    assert await feed(db) == before


@pytest.mark.parametrize(
    "target",
    [f"item:{OTHER_ITEM}", f"tag:{TAG}", "blob:" + "ab" * 32, "artifact:16fd2706-8baf-433b-82eb-8c7fada847da"],
    ids=["item", "tag", "blob", "artifact"],
)
async def test_a_relation_to_something_the_hub_lacks_is_link_target_missing_and_writes_nothing(db, device, target):
    await apply(db, device, create_item(ITEM))
    before = await feed(db)
    create = relation(f"item:{ITEM}", "about", target)

    assert await apply(db, device, create) == [{"mutation_id": create["mutation_id"], "result": "rejected", "reason": "link_target_missing"}]
    assert await feed(db) == before


async def test_a_relation_from_something_the_hub_lacks_is_link_target_missing(db, device):
    await apply(db, device, create_item(OTHER_ITEM))
    create = relation(f"item:{ITEM}", "about", f"item:{OTHER_ITEM}")

    assert (await apply(db, device, create))[0].get("reason") == "link_target_missing"


async def test_a_relation_between_stored_blobs_is_applied(db, device, blob_store, tmp_path):
    pptx, pdf = [await store_bytes(blob_store, tmp_path, content) for content in (b"slides.pptx", b"slides.pdf")]
    rendition = relation(f"blob:{pdf}", "rendition_of", f"blob:{pptx}")

    assert (await apply(db, device, rendition))[0]["result"] == "applied"

    assert rows(await feed(db), "relation")[rendition["id"]]["data"] == {"id": rendition["id"], **rendition["data"]}
