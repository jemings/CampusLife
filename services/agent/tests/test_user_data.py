import pytest
from conftest import contract_fixture
from campuslife_contracts import validate
from test_capture_commit import apply, blob_store, commit, device, feed, new_id, store_bytes, text_capture

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


def op(name, entity, identifier):
    return {"mutation_id": new_id(), "op": name, "entity": entity, "id": identifier}


async def test_deleting_an_item_keeps_its_row_with_the_time_it_was_deleted(db, device):
    await apply(db, device, create_item(ITEM))
    [created] = rows(await feed(db), "item").values()

    assert (await apply(db, device, op("delete", "item", ITEM)))[0]["result"] == "applied"

    [deleted] = rows(await feed(db), "item").values()
    assert deleted["deleted"] is False
    assert deleted["rev"] > created["rev"]
    assert validate("common/instant", deleted["data"].pop("deleted_at")) == []
    assert deleted["data"] == {**created["data"], "rev": deleted["rev"]}


async def test_deleting_a_deleted_or_absent_item_is_applied_and_writes_nothing(db, device):
    await apply(db, device, create_item(ITEM), op("delete", "item", ITEM))
    before = await feed(db)

    results = await apply(db, device, op("delete", "item", ITEM), op("delete", "item", OTHER_ITEM))

    assert [result["result"] for result in results] == ["applied", "applied"]
    assert await feed(db) == before


async def test_restoring_a_deleted_item_removes_the_time_it_was_deleted(db, device):
    await apply(db, device, create_item(ITEM))
    [created] = rows(await feed(db), "item").values()
    await apply(db, device, op("delete", "item", ITEM))

    assert (await apply(db, device, op("restore", "item", ITEM)))[0]["result"] == "applied"

    [restored] = rows(await feed(db), "item").values()
    assert restored["data"] == {**created["data"], "rev": restored["rev"]}


async def test_restoring_a_live_item_is_applied_and_writes_nothing(db, device):
    await apply(db, device, create_item(ITEM))
    before = await feed(db)

    assert (await apply(db, device, op("restore", "item", ITEM)))[0]["result"] == "applied"
    assert await feed(db) == before


async def test_restoring_an_item_the_hub_lacks_is_rejected_as_deleted(db, device):
    restore = op("restore", "item", ITEM)

    assert await apply(db, device, restore) == [{"mutation_id": restore["mutation_id"], "result": "rejected", "reason": "deleted"}]


async def test_deleting_a_relation_leaves_a_tombstone_and_creating_it_again_brings_it_back(db, device):
    await apply(db, device, create_item(ITEM), create_item(OTHER_ITEM))
    create = relation(f"item:{ITEM}", "about", f"item:{OTHER_ITEM}")
    await apply(db, device, create)

    assert (await apply(db, device, op("delete", "relation", create["id"])))[0]["result"] == "applied"
    tombstone = rows(await feed(db), "relation")[create["id"]]
    assert tombstone["deleted"] is True
    assert "data" not in tombstone

    assert (await apply(db, device, {**create, "mutation_id": new_id()}))[0]["result"] == "applied"
    assert rows(await feed(db), "relation")[create["id"]]["data"] == {"id": create["id"], **create["data"]}


@pytest.mark.parametrize("case", ["delete_relation", "delete_tag"])
async def test_deleting_an_absent_relation_or_tag_is_applied_and_writes_nothing(db, device, case):
    assert (await apply(db, device, mutation(case)))[0]["result"] == "applied"
    assert await feed(db) == []


async def test_deleting_a_tag_removes_the_relations_to_it_and_leaves_the_others(db, device):
    await apply(db, device, create_item(ITEM), create_item(OTHER_ITEM), mutation("create_tag"))
    tagged = relation(f"item:{OTHER_ITEM}", "tagged", f"tag:{TAG}")
    from_tag = relation(f"tag:{TAG}", "about", f"item:{ITEM}")
    about = relation(f"item:{ITEM}", "about", f"item:{OTHER_ITEM}")
    await apply(db, device, tagged, from_tag, about)

    assert (await apply(db, device, mutation("delete_tag")))[0]["result"] == "applied"

    changes = await feed(db)
    assert rows(changes, "tag")[TAG]["deleted"] is True
    assert rows(changes, "relation")[tagged["id"]]["deleted"] is True
    assert rows(changes, "relation")[from_tag["id"]]["deleted"] is True
    assert rows(changes, "relation")[about["id"]]["deleted"] is False
    assert (await apply(db, device, {**tagged, "mutation_id": new_id()}))[0].get("reason") == "link_target_missing"


@pytest.mark.parametrize(
    ("name", "entity", "identifier"),
    [
        ("delete", "attachment", ITEM),
        ("restore", "attachment", ITEM),
        ("restore", "tag", TAG),
        ("restore", "relation", f"item:{ITEM}|about|item:{OTHER_ITEM}"),
    ],
)
async def test_an_op_that_does_not_apply_to_the_entity_is_unsupported_entity(db, device, name, entity, identifier):
    unsupported = op(name, entity, identifier)

    assert await apply(db, device, unsupported) == [{"mutation_id": unsupported["mutation_id"], "result": "rejected", "reason": "unsupported_entity"}]


async def test_a_deleted_item_is_still_a_target_for_a_commit_link_and_a_relation(db, device, blob_store, tmp_path):
    await apply(db, device, create_item(ITEM), create_item(OTHER_ITEM), op("delete", "item", ITEM))
    capture = await text_capture(blob_store, tmp_path)
    capture["links"] = [{"type": "about", "to": f"item:{ITEM}"}]

    linking = relation(f"item:{OTHER_ITEM}", "about", f"item:{ITEM}")

    results = await apply(db, device, commit(capture), linking)

    assert [result["result"] for result in results] == ["applied", "applied"]
    relations = rows(await feed(db), "relation")
    source = f"attachment:{capture['attachments'][0]['id']}"
    link = f"{source}|about|item:{ITEM}"
    assert relations[link]["data"] == {"id": link, "from": source, "type": "about", "to": f"item:{ITEM}"}
    assert relations[linking["id"]]["data"] == {"id": linking["id"], **linking["data"]}
