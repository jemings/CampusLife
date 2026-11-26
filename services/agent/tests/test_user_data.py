from datetime import UTC, datetime, timedelta

import pytest
from conftest import contract_fixture
from campuslife_contracts import text_hash, validate
from campuslife_hub import mutations
from campuslife_hub.clock import instant
from campuslife_hub.conflict_stash import kept
from campuslife_hub.mutations import UnsupportedOp
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


def patch(entity, identifier, **changes):
    return {"mutation_id": new_id(), "op": "patch", "entity": entity, "id": identifier, "set": changes}


def patch_item(**changes):
    return patch("item", ITEM, **{key.replace("__", "."): change for key, change in changes.items()})


def task(**data):
    return {
        "mutation_id": new_id(), "op": "create", "entity": "item", "id": ITEM,
        "data": {"kind": "task", "title": "과제 제출", "fields": {"progress": "planned"}, **data},
    }


async def item_row(db):
    return rows(await feed(db), "item")[ITEM]


def outcome(result):
    return {name: value for name, value in result.items() if name != "mutation_id"}


async def test_a_patch_whose_from_holds_is_applied_with_its_fields(db, device):
    await apply(db, device, task())
    before = await item_row(db)

    [result] = await apply(db, device, patch_item(title={"from": "과제 제출", "to": "과제 1 제출"}))

    assert outcome(result) == {"result": "applied", "applied_fields": ["title"]}
    after = await item_row(db)
    assert after["data"] == {**before["data"], "title": "과제 1 제출", "rev": after["rev"]}
    assert after["rev"] > before["rev"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"title": {"from": "과제 제출", "to": "과제 1"}}, {"due_at": {"from": None, "to": "2026-03-09T14:59:00.000Z"}}),
        ({"fields__progress": {"from": "planned", "to": "completed"}}, {"fields__reminders": {"from": None, "to": ["2026-03-08T12:00:00.000Z"]}}),
    ],
    ids=["title and due_at", "fields.progress and fields.reminders"],
)
async def test_two_patches_of_different_fields_from_the_same_state_both_apply(db, device, first, second):
    await apply(db, device, task())

    results = await apply(db, device, patch_item(**first), patch_item(**second))

    assert [result["result"] for result in results] == ["applied", "applied"]
    data = (await item_row(db))["data"]
    for change_set in (first, second):
        for key, change in change_set.items():
            path = key.split("__")
            assert (data[path[0]] if len(path) == 1 else data[path[0]][path[1]]) == change["to"]


@pytest.mark.parametrize(
    ("key", "first", "second", "kept"),
    [
        ("title", {"from": "과제 제출", "to": "과제 1"}, {"from": "과제 제출", "to": "과제 2"}, ("title",)),
        ("fields__progress", {"from": "planned", "to": "completed"}, {"from": "planned", "to": "cancelled"}, ("fields", "progress")),
    ],
)
async def test_the_second_of_two_patches_of_one_field_from_the_same_state_is_a_conflict(db, device, key, first, second, kept):
    await apply(db, device, task())

    results = await apply(db, device, patch_item(**{key: first}), patch_item(**{key: second}))

    wire_key = key.replace("__", ".")
    assert [outcome(result) for result in results] == [
        {"result": "applied", "applied_fields": [wire_key]},
        {"result": "conflict", "applied_fields": [], "conflicted_fields": [wire_key]},
    ]
    data = (await item_row(db))["data"]
    assert (data[kept[0]] if len(kept) == 1 else data[kept[0]][kept[1]]) == first["to"]


async def test_a_patch_with_a_stale_field_applies_its_other_fields(db, device):
    await apply(db, device, task(), patch_item(title={"from": "과제 제출", "to": "과제 1"}))

    [result] = await apply(db, device, patch_item(title={"from": "과제 제출", "to": "과제 2"}, fields__progress={"from": "planned", "to": "completed"}))

    assert outcome(result) == {"result": "conflict", "applied_fields": ["fields.progress"], "conflicted_fields": ["title"]}
    data = (await item_row(db))["data"]
    assert (data["title"], data["fields"]["progress"]) == ("과제 1", "completed")


async def test_a_stale_patch_to_the_value_already_there_is_applied_and_writes_nothing(db, device):
    await apply(db, device, task(), patch_item(fields__progress={"from": "planned", "to": "completed"}))
    before = await feed(db)

    [result] = await apply(db, device, patch_item(fields__progress={"from": "planned", "to": "completed"}))

    assert outcome(result) == {"result": "applied", "applied_fields": ["fields.progress"]}
    assert await feed(db) == before


async def test_a_number_compares_by_value_not_by_how_it_is_written(db, device):
    course = mutation("create_course", id=ITEM)
    await apply(db, device, course)

    [result] = await apply(db, device, patch_item(fields__credits={"from": 3.0, "to": 2}))

    assert outcome(result) == {"result": "applied", "applied_fields": ["fields.credits"]}
    assert (await item_row(db))["data"]["fields"]["credits"] == 2


async def test_a_body_compares_by_the_text_hash_of_what_is_stored(db, device):
    await apply(db, device, task())

    results = await apply(
        db,
        device,
        patch_item(body={"from_hash": None, "to": ""}),
        patch_item(body={"from_hash": text_hash(""), "to": "첫 줄"}),
        patch_item(body={"from_hash": text_hash("첫 줄"), "to": "첫 줄\n둘째 줄"}),
        patch_item(body={"from_hash": text_hash("첫 줄"), "to": "다른 줄"}),
    )

    assert [result["result"] for result in results] == ["applied", "applied", "applied", "conflict"]
    assert (await item_row(db))["data"]["body"] == "첫 줄\n둘째 줄"


async def test_a_body_patched_to_text_with_a_carriage_return_is_not_canonical(db, device):
    await apply(db, device, task())
    before = await feed(db)

    [result] = await apply(db, device, patch_item(body={"from_hash": None, "to": "첫 줄\r\n"}))

    assert outcome(result) == {"result": "rejected", "reason": "not_canonical"}
    assert await feed(db) == before


async def test_to_null_removes_an_optional_column_and_from_null_names_an_absent_one(db, device):
    await apply(db, device, task(due_at="2026-03-09T14:59:00.000Z"))

    results = await apply(
        db,
        device,
        patch_item(due_at={"from": "2026-03-09T14:59:00.000Z", "to": None}, fields__reminders={"from": None, "to": []}),
        patch_item(fields__reminders={"from": [], "to": None}),
    )

    assert [result["result"] for result in results] == ["applied", "applied"]
    data = (await item_row(db))["data"]
    assert "due_at" not in data
    assert data["fields"] == {"progress": "planned"}


async def test_a_field_the_items_kind_does_not_define_is_rejected_as_schema(db, device):
    await apply(db, device, task())
    before = await feed(db)

    [result] = await apply(db, device, patch_item(fields__course_id={"from": None, "to": OTHER_ITEM}))

    assert outcome(result) == {"result": "rejected", "reason": "schema"}
    assert await feed(db) == before


async def test_a_patch_leaving_dates_that_do_not_follow_all_day_is_rejected_as_schema(db, device):
    await apply(db, device, task(starts_at="2026-03-02T00:30:00.000Z", due_at="2026-03-09T14:59:00.000Z"))
    before = await feed(db)

    [result] = await apply(db, device, mutation("patch_all_day_dates"))

    assert outcome(result) == {"result": "rejected", "reason": "schema"}
    assert await feed(db) == before


async def test_a_patch_turning_all_day_on_with_its_dates_is_applied(db, device):
    await apply(db, device, task(starts_at="2026-03-02T00:30:00.000Z"))

    [result] = await apply(db, device, mutation("patch_all_day_dates"))

    assert outcome(result) == {"result": "applied", "applied_fields": ["all_day", "starts_at"]}
    data = (await item_row(db))["data"]
    assert (data["all_day"], data["starts_at"]) == (True, {"date": "2026-03-02", "tz": "Asia/Seoul"})


MORNING = "2026-03-02T00:30:00.000Z"
DUE = "2026-03-09T14:59:00.000Z"
DUE_DATE = {"date": "2026-03-09", "tz": "Asia/Seoul"}


async def test_a_date_patch_from_before_the_item_went_all_day_is_a_conflict_not_a_schema_rejection(db, device):
    await apply(db, device, task(starts_at=MORNING), mutation("patch_all_day_dates"))
    before = await feed(db)

    [result] = await apply(db, device, patch_item(starts_at={"from": MORNING, "to": "2026-03-03T00:30:00.000Z"}))

    assert outcome(result) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["starts_at"]}
    assert await feed(db) == before


async def test_a_patch_from_before_the_item_went_all_day_conflicts_on_its_date_and_applies_its_body(db, device):
    await apply(db, device, task(due_at=DUE, body="a"), patch_item(all_day={"from": None, "to": True}, due_at={"from": DUE, "to": DUE_DATE}))
    before = await item_row(db)

    [result] = await apply(db, device, patch_item(body={"from_hash": text_hash("a"), "to": "b"}, due_at={"from": DUE, "to": "2026-03-10T14:59:00.000Z"}))

    assert outcome(result) == {"result": "conflict", "applied_fields": ["body"], "conflicted_fields": ["due_at"]}
    after = await item_row(db)
    assert after["data"] == {**before["data"], "body": "b", "rev": after["rev"]}


async def p13(db, device):
    """Another device moved starts_at; this patch, from the old state, turns all_day on and edits title and body."""
    await apply(db, device, task(starts_at=MORNING, body="a"), patch_item(starts_at={"from": MORNING, "to": "2026-03-02T01:00:00.000Z"}))
    return patch_item(
        all_day={"from": None, "to": True},
        starts_at={"from": MORNING, "to": {"date": "2026-03-02", "tz": "Asia/Seoul"}},
        title={"from": "과제 제출", "to": "과제 1"},
        body={"from_hash": text_hash("a"), "to": "b"},
    )


async def test_a_conflict_in_the_dates_holds_back_the_other_dates_and_nothing_else(db, device):
    late = await p13(db, device)
    before = await item_row(db)

    [result] = await apply(db, device, late)

    assert outcome(result) == {"result": "conflict", "applied_fields": ["title", "body"], "conflicted_fields": ["all_day", "starts_at"]}
    after = await item_row(db)
    assert after["data"] == {**before["data"], "title": "과제 1", "body": "b", "rev": after["rev"]}


@pytest.mark.parametrize(("moved", "to"), [("starts_at", "2026-03-02T02:00:00.000Z"), ("due_at", "2026-03-09T12:00:00.000Z")])
async def test_a_date_that_won_is_held_back_when_the_other_date_lost(db, device, moved, to):
    dates = {"starts_at": MORNING, "due_at": DUE}
    await apply(db, device, task(**dates), patch_item(**{moved: {"from": dates[moved], "to": to}}))
    before = await feed(db)

    [result] = await apply(db, device, patch_item(starts_at={"from": MORNING, "to": "2026-03-02T03:00:00.000Z"}, due_at={"from": DUE, "to": "2026-03-10T14:59:00.000Z"}))

    assert outcome(result) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["starts_at", "due_at"]}
    assert await feed(db) == before


async def test_all_day_that_won_is_held_back_when_its_date_lost(db, device):
    await apply(db, device, task(starts_at=MORNING), patch_item(starts_at={"from": MORNING, "to": None}))
    before = await feed(db)

    [result] = await apply(db, device, mutation("patch_all_day_dates"))

    assert outcome(result) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["all_day", "starts_at"]}
    assert await feed(db) == before


async def test_a_field_the_kind_lacks_conflicts_instead_of_being_written_when_another_key_lost(db, device):
    await apply(db, device, task(), patch_item(title={"from": "과제 제출", "to": "과제 1"}))
    before = await feed(db)

    [result] = await apply(db, device, patch_item(title={"from": "과제 제출", "to": "과제 2"}, fields__course_id={"from": None, "to": OTHER_ITEM}))

    assert outcome(result) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["title", "fields.course_id"]}
    assert await feed(db) == before


@pytest.mark.parametrize("deleted", ["soft-deleted", "absent"])
async def test_a_patch_of_an_item_that_is_not_live_is_rejected_as_deleted(db, device, deleted):
    if deleted == "soft-deleted":
        await apply(db, device, task(), op("delete", "item", ITEM))
    before = await feed(db)

    [result] = await apply(db, device, patch_item(title={"from": "과제 제출", "to": "과제 1"}))

    assert outcome(result) == {"result": "rejected", "reason": "deleted"}
    assert await feed(db) == before


async def test_the_first_create_resent_after_a_patch_is_still_applied(db, device):
    create = task()
    await apply(db, device, create, patch_item(title={"from": "과제 제출", "to": "과제 1"}))

    assert (await apply(db, device, {**create, "mutation_id": new_id()}))[0]["result"] == "applied"
    assert (await item_row(db))["data"]["title"] == "과제 1"


async def test_an_attachments_cloud_policy_is_patched_by_compare_and_set(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    attachment = capture["attachments"][0]["id"]
    await apply(db, device, commit(capture))

    results = await apply(
        db,
        device,
        patch("attachment", attachment, cloud_policy={"from": "local_only", "to": "cloud_allowed"}),
        patch("attachment", attachment, cloud_policy={"from": "local_only", "to": "blocked"}),
    )

    assert [result["result"] for result in results] == ["applied", "conflict"]
    assert rows(await feed(db), "attachment")[attachment]["data"]["cloud_policy"] == "cloud_allowed"


async def test_a_tag_is_patched_by_compare_and_set_and_a_deleted_one_is_rejected(db, device):
    await apply(db, device, mutation("create_tag"))

    assert (await apply(db, device, mutation("patch_tag_color")))[0]["result"] == "applied"
    assert rows(await feed(db), "tag")[TAG]["data"]["color"] == "#0088ff"
    await apply(db, device, mutation("delete_tag"))
    assert (await apply(db, device, patch("tag", TAG, name={"from": "시험", "to": "중간고사"})))[0].get("reason") == "deleted"


async def test_a_batch_applies_in_order_so_a_patch_sees_the_create_before_it(db, device):
    create = task()
    edit = patch_item(title={"from": "과제 제출", "to": "과제 1"})

    results = await apply(db, device, create, edit, edit)

    assert [outcome(result) for result in results] == [
        {"result": "applied"},
        {"result": "applied", "applied_fields": ["title"]},
        {"result": "duplicate", "original": {"mutation_id": edit["mutation_id"], "result": "applied", "applied_fields": ["title"]}},
    ]


async def stashed(db):
    return [
        {"device_id": value.device_id, "mutation_id": value.mutation_id, "entity": value.entity, "entity_id": value.entity_id,
         "field": value.field, "change": value.change, "theirs_rev": value.theirs_rev}
        for value in await db.read(kept)
    ]


async def test_a_body_that_loses_compare_and_set_is_kept_in_the_stash_and_not_synced(db, device):
    await apply(db, device, task(body="첫 줄"), patch_item(body={"from_hash": text_hash("첫 줄"), "to": "둘째 줄"}))
    theirs = await item_row(db)
    before = await feed(db)
    losing = patch_item(body={"from_hash": text_hash("첫 줄"), "to": "다른 줄"}, title={"from": "과제 제출", "to": "과제 1"})

    [result] = await apply(db, device, losing)

    assert outcome(result) == {"result": "conflict", "applied_fields": ["title"], "conflicted_fields": ["body"]}
    assert await stashed(db) == [
        {"device_id": device, "mutation_id": losing["mutation_id"], "entity": "item", "entity_id": ITEM,
         "field": "body", "change": losing["set"]["body"], "theirs_rev": theirs["rev"]}
    ]
    assert [row for row in await feed(db) if row["entity"] != "item"] == [row for row in before if row["entity"] != "item"]


async def test_a_scalar_that_loses_and_a_body_that_converges_are_not_stashed(db, device):
    await apply(db, device, task(body="첫 줄"), patch_item(body={"from_hash": text_hash("첫 줄"), "to": "둘째 줄"}, title={"from": "과제 제출", "to": "과제 1"}))

    results = await apply(db, device, patch_item(body={"from_hash": text_hash("첫 줄"), "to": "둘째 줄"}, title={"from": "과제 제출", "to": "과제 2"}))

    assert outcome(results[0]) == {"result": "conflict", "applied_fields": ["body"], "conflicted_fields": ["title"]}
    assert await stashed(db) == []


async def entity_with(db, device, blob_store, tmp_path, entity):
    """A live entity of each patchable kind, and its id."""
    if entity == "attachment":
        capture = await text_capture(blob_store, tmp_path)
        await apply(db, device, commit(capture))
        return capture["attachments"][0]["id"]
    if entity == "tag":
        await apply(db, device, mutation("create_tag"))
        return TAG
    await apply(db, device, task(due_at=DUE))
    return ITEM


@pytest.mark.parametrize(
    ("entity", "key", "was", "theirs", "ours"),
    [
        ("item", "title", "과제 제출", "과제 1", "과제 2"),
        ("item", "due_at", DUE, "2026-03-10T14:59:00.000Z", "2026-03-11T14:59:00.000Z"),
        ("item", "fields.progress", "planned", "in_progress", "completed"),
        ("attachment", "cloud_policy", "local_only", "cloud_allowed", "blocked"),
        ("tag", "name", "시험", "기말", "중간"),
    ],
)
async def test_a_column_that_is_not_x_text_and_loses_is_not_stashed(db, device, blob_store, tmp_path, entity, key, was, theirs, ours):
    identifier = await entity_with(db, device, blob_store, tmp_path, entity)
    await apply(db, device, patch(entity, identifier, **{key: {"from": was, "to": theirs}}))

    [result] = await apply(db, device, patch(entity, identifier, **{key: {"from": was, "to": ours}}))

    assert outcome(result) == {"result": "conflict", "applied_fields": [], "conflicted_fields": [key]}
    assert await stashed(db) == []


async def test_every_value_of_a_patch_of_a_deleted_item_is_kept_in_the_stash(db, device):
    await apply(db, device, task(), op("delete", "item", ITEM))
    deleted = await item_row(db)
    late = patch_item(title={"from": "과제 제출", "to": "과제 1"}, body={"from_hash": None, "to": "메모"})

    [result] = await apply(db, device, late)

    assert outcome(result) == {"result": "rejected", "reason": "deleted"}
    assert [(value["field"], value["change"], value["theirs_rev"]) for value in await stashed(db)] == [
        ("title", late["set"]["title"], deleted["rev"]),
        ("body", late["set"]["body"], deleted["rev"]),
    ]


async def test_a_patch_of_an_entity_the_hub_never_had_is_stashed_without_a_rev(db, device):
    late = patch("tag", TAG, color={"from": "#ff8800", "to": "#0088ff"})

    await apply(db, device, late)

    assert [(value["entity"], value["field"], value["theirs_rev"]) for value in await stashed(db)] == [("tag", "color", None)]


async def test_a_rejected_patch_of_a_live_item_stashes_nothing(db, device):
    await apply(db, device, task())

    await apply(db, device, patch_item(fields__course_id={"from": None, "to": OTHER_ITEM}), patch_item(body={"from_hash": None, "to": "a\rb"}))

    assert await stashed(db) == []


async def test_a_body_that_loses_beside_a_date_from_before_all_day_is_kept_in_the_stash(db, device):
    went_all_day = {"all_day": {"from": None, "to": True}, "due_at": {"from": DUE, "to": DUE_DATE}}
    await apply(db, device, task(due_at=DUE, body="a"), patch_item(**went_all_day, body={"from_hash": text_hash("a"), "to": "c"}))
    late = patch_item(body={"from_hash": text_hash("a"), "to": "b"}, due_at={"from": DUE, "to": "2026-03-10T14:59:00.000Z"})

    [result] = await apply(db, device, late)

    assert outcome(result) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["body", "due_at"]}
    assert [(value["field"], value["change"]) for value in await stashed(db)] == [("body", late["set"]["body"])]


async def test_keys_held_back_with_the_dates_are_not_stashed_and_the_others_apply(db, device):
    [result] = await apply(db, device, await p13(db, device))

    assert result["applied_fields"] == ["title", "body"]
    assert await stashed(db) == []


async def test_a_result_is_answered_as_duplicate_for_ninety_days_and_then_judged_again(db, device):
    create, edit = task(), patch_item(title={"from": "과제 제출", "to": "과제 1"})
    await apply(db, device, create, edit)

    await mutations.forget_old_results(db, datetime.now(UTC) + timedelta(days=89))
    assert [result["result"] for result in await apply(db, device, create, edit)] == ["duplicate", "duplicate"]

    await mutations.forget_old_results(db, datetime.now(UTC) + timedelta(days=91))
    before = await feed(db)
    assert [outcome(result) for result in await apply(db, device, create, edit)] == [
        {"result": "applied"},
        {"result": "applied", "applied_fields": ["title"]},
    ]
    assert await feed(db) == before


@pytest.mark.parametrize(
    ("age", "answer"),
    [(timedelta(days=90, milliseconds=-1), "duplicate"), (timedelta(days=90), "duplicate"), (timedelta(days=90, milliseconds=1), "applied")],
    ids=["a millisecond short", "exactly ninety days", "a millisecond past"],
)
async def test_a_result_is_kept_for_ninety_days_to_the_millisecond(db, device, monkeypatch, age, answer):
    recorded = datetime(2026, 3, 2, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(mutations, "now_instant", lambda: instant(recorded))
    create = mutation("create_tag")
    await apply(db, device, create)

    await mutations.forget_old_results(db, recorded + age)

    assert (await apply(db, device, create))[0]["result"] == answer


async def test_a_delete_resent_after_ninety_days_deletes_again(db, device):
    """Accepted (D18): a delete has no from to compare with the row, so a resend older than its result applies again."""
    delete = op("delete", "item", ITEM)
    await apply(db, device, create_item(ITEM), delete, op("restore", "item", ITEM))

    await mutations.forget_old_results(db, datetime.now(UTC) + timedelta(days=91))

    assert (await apply(db, device, delete))[0]["result"] == "applied"
    assert "deleted_at" in (await item_row(db))["data"]


ONE_PER_OP = {
    "capture.commit": ("commit_text_only", False),
    "create": ("create_task", False),
    "patch": ("patch_title", False),
    "delete": ("delete_item", False),
    "restore": ("restore_item", False),
    "suggestion.accept": ("suggestion_accept", True),
    "suggestion.dismiss": ("suggestion_dismiss", True),
}


def test_every_contract_op_has_a_case_below():
    assert ONE_PER_OP.keys() == mutations.CONTRACT_OPS


@pytest.mark.parametrize(("op_name", "case", "refused"), [(name, *case) for name, case in ONE_PER_OP.items()])
async def test_only_an_op_without_a_handler_refuses_the_batch(db, device, op_name, case, refused):
    sent = mutation(case)
    assert sent["op"] == op_name

    if refused:
        with pytest.raises(UnsupportedOp):
            await mutations.apply(db, [sent], device)
    else:
        assert (await apply(db, device, sent))[0]["mutation_id"] == sent["mutation_id"]
