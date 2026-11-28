"""conflict.resolve (D24 decision 3): one transaction that may patch the record's entity and resolves the record."""

from datetime import UTC, datetime, timedelta

from campuslife_contracts import text_hash, validate
from test_capture_commit import apply, blob_store, device, feed, new_id
from test_conflict_records import lost_title, records
from test_purge import PurgeHooks, lecture
from test_user_data import ITEM, TAG, item_row, op, outcome, p13, patch, patch_item, task

from campuslife_hub import audit, mutations, purge
from campuslife_hub.audit import AuditEvent

__all__ = ["blob_store", "device"]


def resolving(record_id, entity="item", **changes):
    mutation = {"mutation_id": new_id(), "op": "conflict.resolve", "conflict_record_id": record_id, "entity": entity}
    return {**mutation, "set": changes} if changes else mutation


async def test_resolving_without_set_keeps_the_hubs_values_and_resolves_the_record(db, device):
    late = await lost_title(db, device)
    before = await item_row(db)
    keep = resolving(late["mutation_id"])

    [result] = await apply(db, device, keep)

    assert outcome(result) == {"result": "applied"}
    [record] = records(await feed(db)).values()
    assert record == {"id": late["mutation_id"], "entity_ref": f"item:{ITEM}", "resolved": {"at": record["resolved"]["at"], "mutation_id": keep["mutation_id"]}}
    assert validate("conflict_record/conflict_record", record) == []
    assert (await item_row(db))["data"] == before["data"]


async def test_resolving_with_set_writes_it_by_compare_and_set_and_resolves_the_record(db, device):
    late = await lost_title(db, device)
    mine = resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 2"})

    [result] = await apply(db, device, mine)

    assert outcome(result) == {"result": "applied", "applied_fields": ["title"]}
    assert (await item_row(db))["data"]["title"] == "과제 2"
    assert records(await feed(db))[late["mutation_id"]]["resolved"]["mutation_id"] == mine["mutation_id"]


async def test_a_key_of_set_that_loses_moves_to_a_new_record_and_the_first_is_resolved(db, device):
    """Nothing is lost: what the resolution could not write waits in its own record (D24 decision 3)."""
    late = await lost_title(db, device)
    await apply(db, device, patch_item(title={"from": "과제 1", "to": "과제 3"}))
    mine = resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 2"})

    [result] = await apply(db, device, mine)

    assert outcome(result) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["title"], "conflict_record_ids": [mine["mutation_id"]]}
    kept = records(await feed(db))
    assert kept[late["mutation_id"]]["resolved"]["mutation_id"] == mine["mutation_id"]
    assert kept[mine["mutation_id"]] == {"id": mine["mutation_id"], "entity_ref": f"item:{ITEM}", "set": mine["set"]}
    assert (await item_row(db))["data"]["title"] == "과제 3"


async def test_a_date_group_resolves_whole_and_a_part_of_it_is_refused_leaving_the_record_open(db, device):
    """E4: starts_at alone cannot follow an all_day it does not carry; the record keeps the whole group."""
    late = await p13(db, device)
    await apply(db, device, late)
    moved = (await item_row(db))["data"]["starts_at"]
    part = resolving(late["mutation_id"], starts_at={"from": moved, "to": {"date": "2026-03-02", "tz": "Asia/Seoul"}})
    whole = resolving(late["mutation_id"], all_day={"from": None, "to": True}, starts_at={"from": moved, "to": {"date": "2026-03-02", "tz": "Asia/Seoul"}})

    [refused] = await apply(db, device, part)
    assert outcome(refused) == {"result": "rejected", "reason": "schema"}
    assert "set" in records(await feed(db))[late["mutation_id"]]

    [result] = await apply(db, device, whole)
    assert outcome(result) == {"result": "applied", "applied_fields": ["all_day", "starts_at"]}
    assert records(await feed(db))[late["mutation_id"]]["resolved"]["mutation_id"] == whole["mutation_id"]
    assert (await item_row(db))["data"]["starts_at"] == {"date": "2026-03-02", "tz": "Asia/Seoul"}


async def test_set_sent_to_an_item_in_the_trash_is_refused_and_keeps_nothing_new(db, device):
    """No implicit restore: the record stays open and no second record is made (D24 decision 3)."""
    late = await lost_title(db, device)
    await apply(db, device, op("delete", "item", ITEM))
    before = await feed(db)

    [result] = await apply(db, device, resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 2"}))

    assert outcome(result) == {"result": "rejected", "reason": "deleted"}
    assert await feed(db) == before


async def test_a_resolve_sent_with_the_restore_before_it_writes_set(db, device):
    late = await lost_title(db, device)
    await apply(db, device, op("delete", "item", ITEM))

    results = await apply(db, device, op("restore", "item", ITEM), resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 2"}))

    assert [outcome(result) for result in results] == [{"result": "applied"}, {"result": "applied", "applied_fields": ["title"]}]
    assert (await item_row(db))["data"]["title"] == "과제 2"


async def test_text_that_is_not_canonical_is_refused_before_the_item_in_the_trash_is(db, device):
    """The same order as a client's patch: not_canonical, then deleted."""
    await apply(db, device, task(body="첫 줄"), patch_item(body={"from_hash": text_hash("첫 줄"), "to": "둘째 줄"}))
    late = patch_item(body={"from_hash": text_hash("첫 줄"), "to": "다른 줄"})
    await apply(db, device, late, op("delete", "item", ITEM))

    [result] = await apply(db, device, resolving(late["mutation_id"], body={"from_hash": text_hash("둘째 줄"), "to": "다른\r\n줄"}))

    assert outcome(result) == {"result": "rejected", "reason": "not_canonical"}


async def test_a_resolve_naming_another_entity_than_its_record_is_refused_as_schema(db, device):
    """entity says how set is judged; one that is not the record's makes the Mutation malformed."""
    late = await lost_title(db, device)
    before = await feed(db)

    results = await apply(db, device, resolving(late["mutation_id"], entity="tag"), resolving(late["mutation_id"], entity="tag", name={"from": "과제 1", "to": "과제 2"}))

    assert [outcome(result) for result in results] == [{"result": "rejected", "reason": "schema"}] * 2
    assert await feed(db) == before


async def test_a_record_the_hub_does_not_hold_is_refused_as_deleted_with_nothing_written(db, device):
    await apply(db, device, task())
    before = await feed(db)

    [result] = await apply(db, device, resolving(new_id(), title={"from": "과제 제출", "to": "과제 2"}))

    assert outcome(result) == {"result": "rejected", "reason": "deleted"}
    assert await feed(db) == before


async def test_a_resolve_of_a_record_purged_with_its_note_writes_nothing(db, device, blob_store, tmp_path):
    """The purge took the record, so even a set brings none of the purged text back (D24 decision 2)."""
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    stale = {"mutation_id": new_id(), "op": "patch", "entity": "item", "id": lecture_.note, "set": {"body": {"from_hash": None, "to": "다른 장치에서 쓴 글"}}}
    await apply(db, device, stale)
    await purge.purge_blob(db, blob_store, lecture_.pdf, delete_notes=[lecture_.note], hooks=PurgeHooks())
    before = await feed(db)
    again = "퍼지뒤해소로들여온글ZXCV"

    [result] = await apply(db, device, resolving(stale["mutation_id"], body={"from_hash": text_hash("스택과 큐\n"), "to": again}))

    assert outcome(result) == {"result": "rejected", "reason": "deleted"}
    assert await feed(db) == before


async def test_a_record_of_a_tag_the_hub_never_had_is_closed_by_a_resolve_without_set(db, device):
    """A target that cannot come back leaves only keeping the Hub's values (D24 decision 2)."""
    late = patch("tag", TAG, color={"from": "#ff8800", "to": "#0088ff"})
    await apply(db, device, late)
    keep = resolving(late["mutation_id"], entity="tag")

    [result] = await apply(db, device, keep)

    assert outcome(result) == {"result": "applied"}
    assert records(await feed(db))[late["mutation_id"]]["resolved"]["mutation_id"] == keep["mutation_id"]


async def test_a_second_resolve_finds_the_record_resolved_and_judges_its_set_as_any_patch(db, device):
    """Two devices resolving at once: the first resolves; the second converges on the same value, or keeps another
    in a record of its own, and keeping the Hub's values does nothing (D24 decision 3)."""
    late = await lost_title(db, device)
    first = resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 2"})
    await apply(db, device, first)
    resolved = records(await feed(db))[late["mutation_id"]]
    same, other, keep = (
        resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 2"}),
        resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 4"}),
        resolving(late["mutation_id"]),
    )

    results = await apply(db, device, same, other, keep)

    assert [outcome(result) for result in results] == [
        {"result": "applied", "applied_fields": ["title"]},
        {"result": "conflict", "applied_fields": [], "conflicted_fields": ["title"], "conflict_record_ids": [other["mutation_id"]]},
        {"result": "applied"},
    ]
    kept = records(await feed(db))
    assert kept[late["mutation_id"]] == resolved
    assert kept.keys() == {late["mutation_id"], other["mutation_id"]}
    assert (await item_row(db))["data"]["title"] == "과제 2"


async def test_a_resolve_judged_again_after_ninety_days_changes_nothing(db, device):
    late = await lost_title(db, device)
    await apply(db, device, patch_item(title={"from": "과제 1", "to": "과제 3"}))
    lost = resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 2"})
    keep = resolving(lost["mutation_id"])
    await apply(db, device, lost, keep)
    before = await feed(db)
    await mutations.forget_old_results(db, datetime.now(UTC) + timedelta(days=91))

    results = await apply(db, device, lost, keep)

    assert [outcome(result) for result in results] == [
        {"result": "conflict", "applied_fields": [], "conflicted_fields": ["title"], "conflict_record_ids": [lost["mutation_id"]]},
        {"result": "applied"},
    ]
    assert await feed(db) == before


async def test_resolving_a_record_is_audited_by_reference_only(db, device):
    """D3: the audit says who resolved which record by which Mutation, and holds none of the values."""
    late = await lost_title(db, device)
    first = resolving(late["mutation_id"], title={"from": "과제 1", "to": "과제 2"})

    await apply(db, device, resolving(late["mutation_id"], entity="tag"), first, resolving(late["mutation_id"]))

    [entry] = await db.read(audit.entries)
    assert (entry.event, entry.subject, entry.details) == (
        AuditEvent.CONFLICT_RESOLVED, device, {"conflict_record_id": late["mutation_id"], "entity_ref": f"item:{ITEM}", "mutation_id": first["mutation_id"]},
    )
