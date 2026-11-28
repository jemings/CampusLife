from datetime import UTC, datetime, timedelta

import pytest
from test_capture_commit import apply, blob_store, commit, device, new_id, store_bytes, text_capture

from campuslife_hub import artifacts, inbox, jobs, store, suggestions
from campuslife_hub.jobs import JobSpec, Pipeline
from campuslife_hub.store import Entity, EntityRef

__all__ = ["blob_store", "device"]

RECORDED_AT = datetime(2026, 3, 2, 1, 10, tzinfo=UTC)
TASK = {"kind": "task", "title": "자료 정리", "fields": {"progress": "inbox"}}


async def committed(db, device, capture):
    assert (await apply(db, device, commit(capture)))[0]["result"] == "applied"
    return capture


async def an_artifact(db, blob_store, tmp_path, capture, pipeline="suggest-1", recorded_at=RECORDED_AT):
    content = await store_bytes(blob_store, tmp_path, new_id().encode())
    spec = JobSpec("suggest", f"capture:{capture['id']}", (capture["attachments"][0]["blob_sha256"],), {})
    job = await db.write(lambda tx: jobs.enqueue(tx, spec, Pipeline(pipeline, "local", "m", "1")))
    return (await db.write(lambda tx: artifacts.record(tx, job, "suggestions", content, now=recorded_at)))["id"]


async def proposed(db, artifact_id, title, evidence=()):
    return await db.write(lambda tx: suggestions.propose(tx, artifact_id, "create_item", {**TASK, "title": title}, list(evidence)))


async def relate(db, source, target, relation_type="about"):
    relation_id = f"{source}|{relation_type}|{target}"
    relation = {"id": relation_id, "from": source, "type": relation_type, "to": target}
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.RELATION, relation_id), relation))
    return relation_id


async def unclassified(db):
    return [capture["id"] for capture in (await db.read(inbox.compute)).captures]


def anchors(capture):
    [attachment] = capture["attachments"]
    return {"capture": f"capture:{capture['id']}", "attachment": f"attachment:{attachment['id']}", "blob": f"blob:{attachment['blob_sha256']}"}


async def test_the_inbox_holds_the_open_suggestions_and_not_the_decided_or_superseded(db, device, blob_store, tmp_path):
    capture = await committed(db, device, await text_capture(blob_store, tmp_path))
    older = await an_artifact(db, blob_store, tmp_path, capture)
    newer = await an_artifact(db, blob_store, tmp_path, capture, "suggest-2", RECORDED_AT + timedelta(minutes=5))
    superseded = await proposed(db, older, "다시 제안된 것")
    still_open = await proposed(db, newer, "다시 제안된 것")
    accepted, dismissed, left_open = [await proposed(db, older, title) for title in ("수락할 것", "기각할 것", "남길 것")]
    await apply(db, device, {"mutation_id": new_id(), "op": "suggestion.accept", "suggestion_id": accepted["id"]},
                {"mutation_id": new_id(), "op": "suggestion.dismiss", "suggestion_id": dismissed["id"]})

    open_ids = {suggestion["id"] for suggestion in (await db.read(inbox.compute)).suggestions}

    assert superseded["id"] != still_open["id"]
    assert open_ids == {still_open["id"], left_open["id"]}


async def test_a_capture_no_relation_reaches_is_unclassified(db, device, blob_store, tmp_path):
    capture = await committed(db, device, await text_capture(blob_store, tmp_path))

    assert await unclassified(db) == [capture["id"]]


@pytest.mark.parametrize("anchor", ["capture", "attachment", "blob"])
@pytest.mark.parametrize("end", ["from", "to"])
async def test_a_live_relation_at_any_of_the_three_anchors_on_either_end_classifies_the_capture(db, device, blob_store, tmp_path, anchor, end):
    capture = await committed(db, device, await text_capture(blob_store, tmp_path))
    other = f"item:{new_id()}"
    relation_id = await relate(db, *((anchors(capture)[anchor], other) if end == "from" else (other, anchors(capture)[anchor])))

    assert await unclassified(db) == []

    await db.write(lambda tx: store.write(tx, EntityRef(Entity.RELATION, relation_id), None))
    assert await unclassified(db) == [capture["id"]]


@pytest.mark.parametrize("relation_type", ["rendition_of", "supersedes"])
@pytest.mark.parametrize("end", ["from", "to"])
async def test_a_relation_between_blobs_classifies_the_capture_too(db, device, blob_store, tmp_path, relation_type, end):
    """The rule does not look at the type: a Blob the Hub relates to another Blob, a rendition or a revision, is
    classified by that Relation alone (#9 decision 1)."""
    capture = await committed(db, device, await text_capture(blob_store, tmp_path))
    other = f"blob:{await store_bytes(blob_store, tmp_path, b'another revision')}"
    blob = anchors(capture)["blob"]
    await relate(db, *((blob, other) if end == "from" else (other, blob)), relation_type)

    assert await unclassified(db) == []


async def test_captures_sharing_a_blob_both_leave_when_a_relation_reaches_the_blob(db, device, blob_store, tmp_path):
    """D2 keeps one Blob per sha256, so evidence on it from either Capture's file classifies both."""
    first = await committed(db, device, await text_capture(blob_store, tmp_path, b"same slides"))
    second = await committed(db, device, await text_capture(blob_store, tmp_path, b"same slides"))
    unrelated = await committed(db, device, await text_capture(blob_store, tmp_path, b"other notes"))
    sha = first["attachments"][0]["blob_sha256"]
    assert second["attachments"][0]["blob_sha256"] == sha
    suggestion = await proposed(db, await an_artifact(db, blob_store, tmp_path, first), "슬라이드 복습",
                                [{"v": 1, "kind": "pdf_page", "blob": sha, "page": 2}])

    assert (await apply(db, device, {"mutation_id": new_id(), "op": "suggestion.accept", "suggestion_id": suggestion["id"]}))[0]["result"] == "applied"

    assert await unclassified(db) == [unrelated["id"]]


async def test_making_an_item_of_a_capture_takes_it_out_of_the_inbox(db, device, blob_store, tmp_path):
    """The way out decision 1 names: one batch of the Item and its evidence Relation to the Capture."""
    capture = await committed(db, device, await text_capture(blob_store, tmp_path))
    kept = await committed(db, device, await text_capture(blob_store, tmp_path, b"kept for later"))
    item_id = new_id()
    relation_id = f"item:{item_id}|evidence|capture:{capture['id']}"

    results = await apply(
        db, device,
        {"mutation_id": new_id(), "op": "create", "entity": "item", "id": item_id, "data": TASK},
        {"mutation_id": new_id(), "op": "create", "entity": "relation", "id": relation_id,
         "data": {"from": f"item:{item_id}", "type": "evidence", "to": f"capture:{capture['id']}"}},
    )

    assert [result["result"] for result in results] == ["applied", "applied"]
    assert await unclassified(db) == [kept["id"]]


async def test_a_capture_committed_with_links_is_classified_from_the_start(db, device, blob_store, tmp_path):
    course_id = new_id()
    await apply(db, device, {"mutation_id": new_id(), "op": "create", "entity": "item", "id": course_id,
                             "data": {"kind": "course", "title": "자료구조", "fields": {}}})
    capture = await text_capture(blob_store, tmp_path)
    await committed(db, device, {**capture, "links": [{"type": "about", "to": f"item:{course_id}"}]})

    assert await unclassified(db) == []


async def test_a_capture_without_attachments_is_judged_by_its_own_anchor(db, device, blob_store, tmp_path):
    capture = {"id": new_id(), "created_at": "2026-03-02T00:30:00.000Z", "origin": {"type": "share"}, "url": "https://example.invalid/notice",
               "links": [], "attachments": []}
    await committed(db, device, capture)
    assert await unclassified(db) == [capture["id"]]

    await relate(db, f"item:{new_id()}", f"capture:{capture['id']}")

    assert await unclassified(db) == []


async def test_unclassified_captures_come_the_last_taken_in_first(db, device, blob_store, tmp_path):
    taken = [await committed(db, device, await text_capture(blob_store, tmp_path, bytes([index]))) for index in range(3)]

    assert await unclassified(db) == [capture["id"] for capture in reversed(taken)]
