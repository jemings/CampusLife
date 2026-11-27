import pytest
from campuslife_contracts import validate
from conftest import contract_fixture

from campuslife_hub import changes, store
from campuslife_hub.store import Entity, EntityRef

CAPTURE_ID = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
ITEM_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def capture_row(capture_id=CAPTURE_ID, note=None):
    row = {
        "id": capture_id,
        "device_id": "16fd2706-8baf-433b-82eb-8c7fada847da",
        "created_at": "2026-03-02T00:30:00.000Z",
        "origin": {"type": "manual"},
    }
    if note is not None:
        row["user_note"] = note
    return row


def item_row(item_id=ITEM_ID):
    return {"id": item_id, "kind": "course", "schema_version": 1, "title": "자료구조", "fields": {}, "rev": 0}


async def page(db, since, limit):
    body = await db.read(lambda snap: changes.page(snap, since, limit))
    assert validate("sync/changes_page", body) == []
    return body


async def test_written_rows_come_back_in_rev_order_and_fit_the_contract(db):
    capture_rev = await db.write(lambda tx: store.write(tx, EntityRef(Entity.CAPTURE, CAPTURE_ID), capture_row()))
    item_rev = await db.write(lambda tx: store.write(tx, EntityRef(Entity.ITEM, ITEM_ID), item_row()))
    body = await page(db, 0, 500)
    assert capture_rev < item_rev
    assert [(row["entity"], row["id"], row["rev"], row["deleted"]) for row in body["changes"]] == [
        ("capture", CAPTURE_ID, capture_rev, False),
        ("item", ITEM_ID, item_rev, False),
    ]
    assert body["changes"][0]["data"] == capture_row()
    assert body["next_since"] == item_rev
    assert body["has_more"] is False


async def test_the_page_carries_the_hub_identity(db):
    from campuslife_hub.identity import read_identity

    identity = await db.read(read_identity)
    body = await page(db, 0, 10)
    assert (body["hub_id"], body["epoch"]) == (identity.hub_id, identity.epoch)


async def test_rewriting_a_row_moves_it_to_a_new_rev_and_lists_it_once(db):
    ref = EntityRef(Entity.CAPTURE, CAPTURE_ID)
    first = await db.write(lambda tx: store.write(tx, ref, capture_row()))
    second = await db.write(lambda tx: store.write(tx, ref, capture_row(note="고침")))
    body = await page(db, 0, 10)
    assert second > first
    assert [(row["id"], row["rev"]) for row in body["changes"]] == [(CAPTURE_ID, second)]
    assert body["changes"][0]["data"]["user_note"] == "고침"
    assert (await page(db, first, 10))["changes"] == body["changes"]


async def test_deleting_leaves_a_tombstone_without_data(db):
    ref = EntityRef(Entity.ITEM, ITEM_ID)
    await db.write(lambda tx: store.write(tx, ref, item_row()))
    rev = await db.write(lambda tx: store.write(tx, ref, None))
    body = await page(db, 0, 10)
    assert body["changes"] == [{"entity": "item", "id": ITEM_ID, "rev": rev, "deleted": True}]
    assert await db.read(lambda snap: store.get(snap, ref)) is None


async def test_get_reads_the_current_row(db):
    ref = EntityRef(Entity.ITEM, ITEM_ID)
    assert await db.read(lambda snap: store.get(snap, ref)) is None
    rev = await db.write(lambda tx: store.write(tx, ref, item_row()))
    assert await db.read(lambda snap: store.get(snap, ref)) == {**item_row(), "rev": rev}


async def test_paging_by_one_through_rows_of_one_transaction_skips_none(db):
    ids = [CAPTURE_ID, "0f8fad5b-d9cb-469f-a165-70867728950e", "16fd2706-8baf-433b-82eb-8c7fada847da"]

    def write_three(tx):
        return [store.write(tx, EntityRef(Entity.CAPTURE, capture_id), capture_row(capture_id)) for capture_id in ids]

    revs = await db.write(write_three)
    assert len(set(revs)) == 3
    seen, since, has_more = [], 0, True
    while has_more:
        body = await page(db, since, 1)
        assert len(body["changes"]) <= 1
        seen += [row["id"] for row in body["changes"]]
        since, has_more = body["next_since"], body["has_more"]
    assert seen == ids
    assert since == max(revs)


async def test_an_empty_page_keeps_the_cursor(db):
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.ITEM, ITEM_ID), item_row()))
    body = await page(db, 99, 10)
    assert (body["changes"], body["next_since"], body["has_more"]) == ([], 99, False)


def with_evidence_of_an_unknown_kind():
    suggestion = contract_fixture("valid", "suggestion/suggestion", "create_assignment")
    return {**suggestion, "evidence": [contract_fixture("forward", "locator/locator", "kind_unknown"), *suggestion["evidence"]]}


@pytest.mark.parametrize(
    ("entity", "row"),
    [
        (Entity.RELATION, contract_fixture("forward", "relation/relation", "locators_of_unknown_kind")),
        (Entity.ARTIFACT, contract_fixture("forward", "artifact/artifact", "unknown_type")),
        (Entity.SUGGESTION, contract_fixture("forward", "suggestion/suggestion", "action_unknown")),
        (Entity.SUGGESTION, with_evidence_of_an_unknown_kind()),
    ],
    ids=["relation locators of an unknown kind", "artifact of an unknown type", "suggestion of an unknown action",
         "suggestion evidence of an unknown kind"],
)
async def test_open_values_the_hub_may_carry_pass_through_the_feed(db, entity, row):
    await db.write(lambda tx: store.write(tx, EntityRef(entity, row["id"]), row))
    body = await page(db, 0, 10)
    assert body["changes"][0]["data"] == row


async def feed_rows(db):
    return [(row["entity"], row["id"], row["rev"], row.get("data")) for row in (await page(db, 0, 500))["changes"]]


async def test_touching_a_row_sends_it_out_again_unchanged_at_a_new_rev(db):
    capture, item = EntityRef(Entity.CAPTURE, CAPTURE_ID), EntityRef(Entity.ITEM, ITEM_ID)
    await db.write(lambda tx: store.write(tx, capture, capture_row()))
    await db.write(lambda tx: store.write(tx, item, item_row()))

    capture_rev = await db.write(lambda tx: store.touch(tx, capture))
    item_rev = await db.write(lambda tx: store.touch(tx, item))

    assert capture_rev < item_rev
    assert await feed_rows(db) == [
        ("capture", CAPTURE_ID, capture_rev, capture_row()),
        ("item", ITEM_ID, item_rev, {**item_row(), "rev": item_rev}),
    ]


async def test_touching_what_is_not_stored_writes_nothing(db):
    gone = EntityRef(Entity.CAPTURE, CAPTURE_ID)
    await db.write(lambda tx: store.write(tx, gone, capture_row()))
    await db.write(lambda tx: store.write(tx, gone, None))
    before = await feed_rows(db)

    touched = [await db.write(lambda tx: store.touch(tx, ref)) for ref in (gone, EntityRef(Entity.ITEM, ITEM_ID))]

    assert touched == [None, None]
    assert await feed_rows(db) == before
