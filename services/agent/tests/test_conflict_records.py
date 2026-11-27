"""ConflictRecords (D18, D24 decisions 1 and 2): what a patch submitted and the Hub did not write, one record per Mutation,
synced as changes rows of entity conflict_record."""

from datetime import UTC, datetime, timedelta

import pytest
from campuslife_contracts import text_hash
from campuslife_hub import conflict_records, mutations, store
from campuslife_hub.store import Entity, EntityRef
from test_capture_commit import apply, blob_store, device, feed, new_id
from test_user_data import DUE, ITEM, OTHER_ITEM, TAG, entity_with, op, outcome, p13, patch, patch_item, rows, task

__all__ = ["blob_store", "device"]


def records(changes):
    return {row["id"]: row["data"] for row in rows(changes, "conflict_record").values() if not row["deleted"]}


def lost_part(sent, result):
    """The part of a patch's set a record keeps: its conflicted keys, as the patch sent them."""
    return {key: sent["set"][key] for key in result["conflicted_fields"]}


async def test_every_key_a_patch_loses_is_kept_in_one_record_under_its_mutation_id(db, device):
    """E1: a title and a body lost together are both kept; before D24 the title was kept nowhere."""
    await apply(db, device, task(body="첫 줄"), patch_item(body={"from_hash": text_hash("첫 줄"), "to": "둘째 줄"}, title={"from": "과제 제출", "to": "과제 1"}))
    late = patch_item(body={"from_hash": text_hash("첫 줄"), "to": "다른 줄"}, title={"from": "과제 제출", "to": "과제 2"})

    [result] = await apply(db, device, late)

    assert outcome(result) == {
        "result": "conflict", "applied_fields": [], "conflicted_fields": ["body", "title"], "conflict_record_ids": [late["mutation_id"]],
    }
    assert records(await feed(db)) == {
        late["mutation_id"]: {"id": late["mutation_id"], "entity_ref": f"item:{ITEM}", "set": {"body": late["set"]["body"], "title": late["set"]["title"]}},
    }


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
async def test_a_scalar_that_loses_is_kept_as_its_patch_sent_it(db, device, blob_store, tmp_path, entity, key, was, theirs, ours):
    identifier = await entity_with(db, device, blob_store, tmp_path, entity)
    await apply(db, device, patch(entity, identifier, **{key: {"from": was, "to": theirs}}))
    late = patch(entity, identifier, **{key: {"from": was, "to": ours}})

    [result] = await apply(db, device, late)

    assert outcome(result) == {"result": "conflict", "applied_fields": [], "conflicted_fields": [key], "conflict_record_ids": [late["mutation_id"]]}
    assert records(await feed(db))[late["mutation_id"]] == {"id": late["mutation_id"], "entity_ref": f"{entity}:{identifier}", "set": late["set"]}


async def scalar_and_converging_body(db, device):
    await apply(db, device, task(body="첫 줄"), patch_item(body={"from_hash": text_hash("첫 줄"), "to": "둘째 줄"}, title={"from": "과제 제출", "to": "과제 1"}))
    return patch_item(body={"from_hash": text_hash("첫 줄"), "to": "둘째 줄"}, title={"from": "과제 제출", "to": "과제 2"})


async def dates_from_before_all_day(db, device):
    went_all_day = {"all_day": {"from": None, "to": True}, "due_at": {"from": DUE, "to": {"date": "2026-03-09", "tz": "Asia/Seoul"}}}
    await apply(db, device, task(due_at=DUE, body="a"), patch_item(**went_all_day, body={"from_hash": text_hash("a"), "to": "c"}))
    return patch_item(body={"from_hash": text_hash("a"), "to": "b"}, due_at={"from": DUE, "to": "2026-03-10T14:59:00.000Z"})


async def a_lost_date_beside_title_and_body(db, device):
    return await p13(db, device)


async def a_body_that_loses_beside_a_title_that_wins(db, device):
    await apply(db, device, task(body="첫 줄"), patch_item(body={"from_hash": text_hash("첫 줄"), "to": "둘째 줄"}))
    return patch_item(body={"from_hash": text_hash("첫 줄"), "to": "다른 줄"}, title={"from": "과제 제출", "to": "과제 1"})


@pytest.mark.parametrize(
    "arrange", [scalar_and_converging_body, dates_from_before_all_day, a_lost_date_beside_title_and_body, a_body_that_loses_beside_a_title_that_wins]
)
async def test_a_record_keeps_exactly_the_keys_that_did_not_win(db, device, arrange):
    """The record's set is the conflicted part of what was sent: no key that won or converged, every key held back
    with a date group that lost (D24 decision 1)."""
    late = await arrange(db, device)

    [result] = await apply(db, device, late)

    kept = records(await feed(db))[late["mutation_id"]]["set"]
    assert kept == lost_part(late, result)
    assert kept.keys() <= late["set"].keys()
    assert kept.keys().isdisjoint(result["applied_fields"])


async def test_a_patch_that_wins_every_key_keeps_no_record(db, device):
    await apply(db, device, task())

    [result] = await apply(db, device, patch_item(title={"from": "과제 제출", "to": "과제 1"}))

    assert "conflict_record_ids" not in result
    assert records(await feed(db)) == {}


async def test_a_body_that_loses_beside_a_title_that_wins_keeps_only_the_body(db, device):
    late = await a_body_that_loses_beside_a_title_that_wins(db, device)

    [result] = await apply(db, device, late)

    assert outcome(result) == {"result": "conflict", "applied_fields": ["title"], "conflicted_fields": ["body"], "conflict_record_ids": [late["mutation_id"]]}
    assert records(await feed(db))[late["mutation_id"]]["set"] == {"body": late["set"]["body"]}


@pytest.mark.parametrize("gone", ["soft-deleted", "never held"])
async def test_every_key_of_a_patch_of_an_item_that_is_not_live_is_kept(db, device, gone):
    if gone == "soft-deleted":
        await apply(db, device, task(), op("delete", "item", ITEM))
    late = patch_item(title={"from": "과제 제출", "to": "과제 1"}, body={"from_hash": None, "to": "메모"})

    [result] = await apply(db, device, late)

    assert outcome(result) == {"result": "rejected", "reason": "deleted", "conflict_record_ids": [late["mutation_id"]]}
    assert records(await feed(db)) == {late["mutation_id"]: {"id": late["mutation_id"], "entity_ref": f"item:{ITEM}", "set": late["set"]}}


async def test_a_patch_of_a_tag_the_hub_never_had_is_kept_under_the_tag(db, device):
    late = patch("tag", TAG, color={"from": "#ff8800", "to": "#0088ff"})

    [result] = await apply(db, device, late)

    assert result["conflict_record_ids"] == [late["mutation_id"]]
    assert records(await feed(db))[late["mutation_id"]]["entity_ref"] == f"tag:{TAG}"


async def test_a_patch_rejected_for_another_reason_keeps_no_record(db, device):
    await apply(db, device, task())

    results = await apply(db, device, patch_item(fields__course_id={"from": None, "to": OTHER_ITEM}), patch_item(body={"from_hash": None, "to": "a\rb"}))

    assert [(result["result"], result["reason"]) for result in results] == [("rejected", "schema"), ("rejected", "not_canonical")]
    assert all("conflict_record_ids" not in result for result in results)
    assert records(await feed(db)) == {}


async def lost_title(db, device):
    await apply(db, device, task(), patch_item(title={"from": "과제 제출", "to": "과제 1"}))
    late = patch_item(title={"from": "과제 제출", "to": "과제 2"})
    await apply(db, device, late)
    return late


async def test_a_resend_within_ninety_days_names_the_same_record(db, device):
    late = await lost_title(db, device)
    before = await feed(db)

    [again] = await apply(db, device, late)

    assert again["original"]["conflict_record_ids"] == [late["mutation_id"]]
    assert await feed(db) == before


async def test_a_resend_judged_again_after_ninety_days_names_its_record_and_makes_no_second_one(db, device):
    """E2: the record's id is the mutation_id, so judging a resend again cannot keep the values twice."""
    late = await lost_title(db, device)
    before = await feed(db)
    await mutations.forget_old_results(db, datetime.now(UTC) + timedelta(days=91))

    [again] = await apply(db, device, late)

    assert outcome(again) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["title"], "conflict_record_ids": [late["mutation_id"]]}
    assert await feed(db) == before


async def test_a_resend_judged_again_does_not_open_a_resolved_record(db, device):
    """D24 decision 2: a resolved record stays resolved, so the user is not asked again about what they settled."""
    late = await lost_title(db, device)
    resolved = {"id": late["mutation_id"], "entity_ref": f"item:{ITEM}", "resolved": {"at": "2026-03-03T09:15:00.000Z", "mutation_id": new_id()}}
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.CONFLICT_RECORD, late["mutation_id"]), resolved))
    before = await feed(db)
    await mutations.forget_old_results(db, datetime.now(UTC) + timedelta(days=91))

    [again] = await apply(db, device, late)

    assert again["conflict_record_ids"] == [late["mutation_id"]]
    assert await feed(db) == before


async def test_the_strictest_cloud_policy_an_open_record_of_an_attachment_submitted_is_read_for_provider_calls(db, device, blob_store, tmp_path):
    """D24 decision 11: while a stricter policy waits in an open record, the Hub's value may be looser than the user's
    wish, so #8 takes the minimum of the two; a resolved record no longer asks for anything."""
    attachment = await entity_with(db, device, blob_store, tmp_path, "attachment")
    other = await entity_with(db, device, blob_store, tmp_path, "attachment")
    await apply(db, device, patch("attachment", attachment, cloud_policy={"from": "local_only", "to": "cloud_allowed"}))
    asked = [patch("attachment", attachment, cloud_policy={"from": "local_only", "to": policy}) for policy in ("local_only", "blocked")]
    await apply(db, device, *asked)
    settled = {"id": asked[1]["mutation_id"], "entity_ref": f"attachment:{attachment}", "resolved": {"at": "2026-03-03T09:15:00.000Z", "mutation_id": new_id()}}

    before = await db.read(lambda snap: conflict_records.strictest_submitted_cloud_policy(snap, attachment))
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.CONFLICT_RECORD, settled["id"]), settled))
    after = await db.read(lambda snap: conflict_records.strictest_submitted_cloud_policy(snap, attachment))
    untouched = await db.read(lambda snap: conflict_records.strictest_submitted_cloud_policy(snap, other))

    assert (before, after, untouched) == ("blocked", "local_only", None)
