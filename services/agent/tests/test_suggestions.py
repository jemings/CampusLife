from datetime import UTC, datetime, timedelta

import pytest
from campuslife_contracts import validate
from conftest import contract_fixture
from test_capture_commit import apply, blob_store, device, feed, new_id, store_bytes

from campuslife_hub import artifacts, jobs, store, suggestions
from campuslife_hub.jobs import JobSpec, Pipeline
from campuslife_hub.store import Entity, EntityRef

__all__ = ["blob_store", "device"]

COURSE = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
CAPTURE = "0f8fad5b-d9cb-469f-a165-70867728950e"
FINGERPRINT = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
OTHER_FINGERPRINT = "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"
ASSIGNMENT = {
    "kind": "assignment",
    "title": "과제 2",
    "starts_at": "2026-03-09T00:00:00.000Z",
    "due_at": "2026-03-16T14:59:00.000Z",
    "fields": {"course_id": COURSE, "due_text": "3월 16일 23:59까지"},
}
RECORDED_AT = datetime(2026, 3, 2, 1, 10, tzinfo=UTC)
LATER = RECORDED_AT + timedelta(minutes=5)


async def grounds(db, blob_store, tmp_path, pipeline="suggest-1", recorded_at=RECORDED_AT):
    """An Artifact to propose from, and the PDF Blob and recording Capture its evidence rests on."""
    pdf = await store_bytes(blob_store, tmp_path, b"%PDF synthetic slides")
    content = await store_bytes(blob_store, tmp_path, pipeline.encode())
    capture = {"id": CAPTURE, "device_id": new_id(), "created_at": "2026-03-02T00:30:00.000Z", "origin": {"type": "manual"}}
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.CAPTURE, CAPTURE), capture))
    job = await db.write(lambda tx: jobs.enqueue(tx, JobSpec("suggest", f"capture:{CAPTURE}", (pdf,), {}), Pipeline(pipeline, "local", "m", "1")))
    artifact = await db.write(lambda tx: artifacts.record(tx, job, "suggestions", content, now=recorded_at))
    return artifact["id"], pdf


def page_at(pdf, page=2):
    return {"v": 1, "kind": "pdf_page", "blob": pdf, "page": page}


SPOKEN = {"v": 1, "kind": "media_time", "capture": CAPTURE, "start_ms": 1930000, "end_ms": 1985000}


async def propose(db, artifact_id, action="create_item", proposal=ASSIGNMENT, evidence=(), fingerprint=FINGERPRINT):
    return await db.write(lambda tx: suggestions.propose(tx, artifact_id, action, proposal, list(evidence), fingerprint))


def accepting(suggestion, edits=None):
    mutation = {"mutation_id": new_id(), "op": "suggestion.accept", "suggestion_id": suggestion["id"]}
    return mutation if edits is None else {**mutation, "edits": edits}


def dismissing(suggestion):
    return {"mutation_id": new_id(), "op": "suggestion.dismiss", "suggestion_id": suggestion["id"]}


async def decide(db, device, mutation):
    [result] = await apply(db, device, mutation)
    return {name: value for name, value in result.items() if name != "mutation_id"}


def rows(changes, entity):
    return {row["id"]: row["data"] for row in changes if row["entity"] == entity and not row["deleted"]}


async def state_of(db, suggestion):
    return (await db.read(lambda snap: store.get(snap, EntityRef(Entity.SUGGESTION, suggestion["id"]))))["state"]


def created_item(case="create_task", **data):
    create = contract_fixture("valid", "mutation/mutation", case)
    create = {**create, "mutation_id": new_id(), "id": new_id(), "data": {**create["data"], **data}}
    return create["id"], create


def due_at_patch(item_id, before, after, **more):
    return {"entity": "item", "id": item_id, "set": {"due_at": {"from": before, "to": after}, **more}}


async def test_a_proposal_is_an_open_suggestion_in_the_feed(db, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)

    suggestion = await propose(db, artifact_id, evidence=[page_at(pdf), SPOKEN])

    assert validate("suggestion/suggestion", suggestion) == []
    assert {name: value for name, value in suggestion.items() if name != "id"} == {
        "source_artifact_id": artifact_id, "action": "create_item", "proposed": ASSIGNMENT,
        "evidence": [page_at(pdf), SPOKEN], "fingerprint": FINGERPRINT, "state": "open",
    }
    assert rows(await feed(db), "suggestion") == {suggestion["id"]: suggestion}


async def test_the_same_artifact_proposing_a_fingerprint_again_gets_the_first_suggestion_back(db, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    first = await propose(db, artifact_id, evidence=[page_at(pdf)])
    before = await feed(db)

    again = await propose(db, artifact_id, evidence=[page_at(pdf, 3)])

    assert again == first
    assert await feed(db) == before


@pytest.mark.parametrize("decision", [accepting, dismissing])
async def test_a_fingerprint_the_user_decided_is_not_proposed_again(db, device, blob_store, tmp_path, decision):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    decided = await propose(db, artifact_id)
    await decide(db, device, decision(decided))
    newer_id, _ = await grounds(db, blob_store, tmp_path, pipeline="suggest-2", recorded_at=LATER)
    before = await feed(db)

    assert await propose(db, newer_id) is None
    assert await propose(db, artifact_id) is None

    assert await feed(db) == before


async def test_a_newer_artifact_proposing_an_open_fingerprint_supersedes_the_older_suggestion(db, blob_store, tmp_path):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    older = await propose(db, artifact_id)
    other = await propose(db, artifact_id, fingerprint=OTHER_FINGERPRINT)
    newer_id, _ = await grounds(db, blob_store, tmp_path, pipeline="suggest-2", recorded_at=LATER)

    newer = await propose(db, newer_id)

    assert (newer["source_artifact_id"], newer["state"]) == (newer_id, "open")
    assert [await state_of(db, each) for each in (older, other, newer)] == ["superseded", "open", "open"]


async def test_an_older_artifact_proposing_again_leaves_the_newer_suggestion_open(db, blob_store, tmp_path):
    older_id, _ = await grounds(db, blob_store, tmp_path)
    older = await propose(db, older_id)
    newer_id, _ = await grounds(db, blob_store, tmp_path, pipeline="suggest-2", recorded_at=LATER)
    newer = await propose(db, newer_id)
    before = await feed(db)

    assert await propose(db, older_id) is None

    assert await feed(db) == before
    assert [await state_of(db, each) for each in (older, newer)] == ["superseded", "open"]


async def test_an_artifact_recorded_at_the_same_instant_leaves_the_open_suggestion_as_it_is(db, blob_store, tmp_path):
    first_id, _ = await grounds(db, blob_store, tmp_path)
    first = await propose(db, first_id)
    second_id, _ = await grounds(db, blob_store, tmp_path, pipeline="suggest-2")

    assert await propose(db, second_id) is None

    assert await state_of(db, first) == "open"


async def test_cited_blocks_on_one_page_are_kept_as_one_locator(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)

    suggestion = await propose(db, artifact_id, evidence=[page_at(pdf), SPOKEN, page_at(pdf), SPOKEN])
    assert await decide(db, device, accepting(suggestion)) == {"result": "applied"}

    assert suggestion["evidence"] == [page_at(pdf), SPOKEN]
    [(item_id, _)] = rows(await feed(db), "item").items()
    assert rows(await feed(db), "relation")[f"item:{item_id}|evidence|blob:{pdf}"]["locators"] == [page_at(pdf)]


@pytest.mark.parametrize("action", ["create_item", "add_relation"])
async def test_a_suggestion_kept_with_repeated_evidence_puts_each_locator_on_the_relation_once(db, device, blob_store, tmp_path, action):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item()
    await apply(db, device, create)
    relation_id = f"item:{item_id}|evidence|blob:{pdf}"
    proposals = {"create_item": ASSIGNMENT, "add_relation": {"id": relation_id, "from": f"item:{item_id}", "type": "evidence", "to": f"blob:{pdf}"}}
    kept = {"id": new_id(), "source_artifact_id": artifact_id, "action": action, "proposed": proposals[action],
            "evidence": [page_at(pdf), page_at(pdf, 3), page_at(pdf)], "fingerprint": FINGERPRINT, "state": "open"}
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.SUGGESTION, kept["id"]), kept))

    assert await decide(db, device, accepting(kept)) == {"result": "applied"}

    [locators] = [row["locators"] for row in rows(await feed(db), "relation").values()]
    assert locators == [page_at(pdf), page_at(pdf, 3)]


@pytest.mark.parametrize("anchor", ["blob", "capture"])
async def test_a_proposal_whose_evidence_rests_on_what_the_hub_does_not_hold_is_refused(db, blob_store, tmp_path, anchor):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    missing = {"blob": {"v": 1, "kind": "document", "blob": "e" * 64}, "capture": {**SPOKEN, "capture": new_id()}}[anchor]
    before = await feed(db)

    with pytest.raises(suggestions.InvalidSuggestion):
        await propose(db, artifact_id, evidence=[page_at(pdf), missing])

    assert await feed(db) == before


@pytest.mark.parametrize("line_break", ["\r\n", "\r"])
async def test_proposed_text_is_kept_with_lf_line_ends_and_accepted_as_it_is(db, device, blob_store, tmp_path, line_break):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item()
    await apply(db, device, create)
    created = await propose(db, artifact_id, proposal={**ASSIGNMENT, "body": f"1주차{line_break}2주차"})
    patching = await propose(db, artifact_id, "patch_item", {"entity": "item", "id": item_id, "set": {"body": {"from_hash": None, "to": f"a{line_break}b"}}},
                             fingerprint=OTHER_FINGERPRINT)

    results = [await decide(db, device, accepting(each)) for each in (created, patching)]

    assert (created["proposed"]["body"], patching["proposed"]["set"]["body"]["to"]) == ("1주차\n2주차", "a\nb")
    assert results == [{"result": "applied"}, {"result": "applied", "applied_fields": ["body"]}]
    assert sorted(item["body"] for item in rows(await feed(db), "item").values()) == ["1주차\n2주차", "a\nb"]


@pytest.mark.parametrize(
    ("action", "proposal", "artifact"),
    [
        ("merge_items", {"keep": COURSE}, "recorded"),
        ("create_item", {"kind": "assignment", "fields": {"course_id": COURSE}}, "recorded"),
        ("create_item", ASSIGNMENT, "unknown"),
    ],
    ids=["action this hub cannot accept", "proposal outside the contract", "artifact not recorded"],
)
async def test_a_proposal_the_hub_could_not_keep_is_refused_and_nothing_is_written(db, blob_store, tmp_path, action, proposal, artifact):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    before = await feed(db)

    with pytest.raises(suggestions.InvalidSuggestion):
        await propose(db, artifact_id if artifact == "recorded" else new_id(), action, proposal)

    assert await feed(db) == before


async def test_accepting_a_create_item_makes_the_item_and_an_evidence_relation_per_anchor(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id, evidence=[page_at(pdf), page_at(pdf, 3), SPOKEN])

    assert await decide(db, device, accepting(suggestion)) == {"result": "applied"}

    changes = await feed(db)
    [(item_id, item)] = rows(changes, "item").items()
    assert validate("common/uuid", item_id) == []
    assert item == {"id": item_id, "schema_version": 1, **ASSIGNMENT, "rev": item["rev"]}
    assert rows(changes, "relation") == {
        f"item:{item_id}|evidence|blob:{pdf}": {
            "id": f"item:{item_id}|evidence|blob:{pdf}", "from": f"item:{item_id}", "type": "evidence", "to": f"blob:{pdf}",
            "locators": [page_at(pdf), page_at(pdf, 3)],
        },
        f"item:{item_id}|evidence|capture:{CAPTURE}": {
            "id": f"item:{item_id}|evidence|capture:{CAPTURE}", "from": f"item:{item_id}", "type": "evidence", "to": f"capture:{CAPTURE}",
            "locators": [SPOKEN],
        },
    }
    assert rows(changes, "suggestion")[suggestion["id"]] == {**suggestion, "state": "accepted"}


async def test_a_suggestion_without_evidence_is_accepted_without_relations(db, device, blob_store, tmp_path):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)

    assert await decide(db, device, accepting(await propose(db, artifact_id))) == {"result": "applied"}

    changes = await feed(db)
    assert len(rows(changes, "item")) == 1
    assert rows(changes, "relation") == {}


async def test_edits_replace_what_a_create_item_proposed_and_null_leaves_an_optional_column_out(db, device, blob_store, tmp_path):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id)
    edits = {"title": "과제 2 (조별)", "fields.due_text": "3월 17일까지", "starts_at": None}

    assert await decide(db, device, accepting(suggestion, edits)) == {"result": "applied"}

    [item] = rows(await feed(db), "item").values()
    assert item["title"] == "과제 2 (조별)"
    assert item["fields"] == {"course_id": COURSE, "due_text": "3월 17일까지"}
    assert "starts_at" not in item


async def test_accepting_a_patch_item_patches_the_item_with_edits_in_place_of_the_proposed_to(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    proposal = due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z",
                            title={"from": create["data"]["title"], "to": "과제 3"})
    suggestion = await propose(db, artifact_id, "patch_item", proposal, [page_at(pdf)])

    result = await decide(db, device, accepting(suggestion, {"due_at": "2026-03-24T14:59:00.000Z"}))

    assert result == {"result": "applied", "applied_fields": ["due_at", "title"]}
    changes = await feed(db)
    item = rows(changes, "item")[item_id]
    assert (item["due_at"], item["title"]) == ("2026-03-24T14:59:00.000Z", "과제 3")
    assert rows(changes, "relation")[f"item:{item_id}|evidence|blob:{pdf}"]["locators"] == [page_at(pdf)]
    assert await state_of(db, suggestion) == "accepted"


async def test_evidence_a_relation_already_has_is_joined_not_repeated(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    earlier = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"),
                            [page_at(pdf, 2)])
    later = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-23T14:59:00.000Z", "2026-03-30T14:59:00.000Z"),
                          [page_at(pdf, 3), page_at(pdf, 2)], OTHER_FINGERPRINT)

    for suggestion in (earlier, later):
        assert (await decide(db, device, accepting(suggestion)))["result"] == "applied"

    relation = rows(await feed(db), "relation")[f"item:{item_id}|evidence|blob:{pdf}"]
    assert relation["locators"] == [page_at(pdf, 2), page_at(pdf, 3)]


async def test_evidence_a_relation_already_holds_leaves_it_as_it_is(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    relation_id = f"item:{item_id}|evidence|blob:{pdf}"
    earlier = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"),
                            [page_at(pdf)])
    await decide(db, device, accepting(earlier))
    later = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-23T14:59:00.000Z", "2026-03-30T14:59:00.000Z"),
                          [page_at(pdf)], OTHER_FINGERPRINT)
    before = [row for row in await feed(db) if row["id"] == relation_id]

    assert (await decide(db, device, accepting(later)))["result"] == "applied"

    assert [row for row in await feed(db) if row["id"] == relation_id] == before


async def test_a_patch_item_whose_item_changed_since_is_a_conflict_and_the_suggestion_is_accepted(db, device, blob_store, tmp_path):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    suggestion = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"))
    moved = {"mutation_id": new_id(), "op": "patch", "entity": "item", "id": item_id,
             "set": {"due_at": {"from": "2026-03-16T14:59:00.000Z", "to": "2026-03-20T14:59:00.000Z"}}}
    await apply(db, device, moved)

    result = await decide(db, device, accepting(suggestion))

    assert result == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["due_at"]}
    assert rows(await feed(db), "item")[item_id]["due_at"] == "2026-03-20T14:59:00.000Z"
    assert await state_of(db, suggestion) == "accepted"


async def test_a_patch_item_that_lost_every_key_adds_no_evidence(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    suggestion = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"),
                               [page_at(pdf)])
    moved = {"mutation_id": new_id(), "op": "patch", "entity": "item", "id": item_id,
             "set": {"due_at": {"from": "2026-03-16T14:59:00.000Z", "to": "2026-03-20T14:59:00.000Z"}}}
    await apply(db, device, moved)

    assert await decide(db, device, accepting(suggestion)) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["due_at"]}

    assert rows(await feed(db), "relation") == {}
    assert await state_of(db, suggestion) == "accepted"


async def test_a_patch_item_that_won_some_keys_adds_its_evidence(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    proposal = due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z", title={"from": create["data"]["title"], "to": "과제 3"})
    suggestion = await propose(db, artifact_id, "patch_item", proposal, [page_at(pdf)])
    moved = {"mutation_id": new_id(), "op": "patch", "entity": "item", "id": item_id,
             "set": {"due_at": {"from": "2026-03-16T14:59:00.000Z", "to": "2026-03-20T14:59:00.000Z"}}}
    await apply(db, device, moved)

    assert await decide(db, device, accepting(suggestion)) == {"result": "conflict", "applied_fields": ["title"], "conflicted_fields": ["due_at"]}

    assert rows(await feed(db), "relation")[f"item:{item_id}|evidence|blob:{pdf}"]["locators"] == [page_at(pdf)]


async def test_a_patch_item_on_a_deleted_item_is_rejected_as_deleted_and_stays_open(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create, {"mutation_id": new_id(), "op": "delete", "entity": "item", "id": item_id})
    suggestion = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"),
                               [page_at(pdf)])
    before = await feed(db)

    assert await decide(db, device, accepting(suggestion)) == {"result": "rejected", "reason": "deleted"}

    assert await feed(db) == before


async def test_accepting_an_evidence_relation_puts_the_evidence_anchored_at_its_to_in_its_locators(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item()
    await apply(db, device, create)
    relation = {"id": f"item:{item_id}|evidence|blob:{pdf}", "from": f"item:{item_id}", "type": "evidence", "to": f"blob:{pdf}"}
    suggestion = await propose(db, artifact_id, "add_relation", relation, [page_at(pdf), SPOKEN])

    assert await decide(db, device, accepting(suggestion)) == {"result": "applied"}

    assert rows(await feed(db), "relation") == {relation["id"]: {**relation, "locators": [page_at(pdf)]}}


async def test_accepting_another_known_relation_type_creates_it_without_locators(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    newer = await store_bytes(blob_store, tmp_path, b"%PDF synthetic slides, revised")
    relation = {"id": f"blob:{newer}|supersedes|blob:{pdf}", "from": f"blob:{newer}", "type": "supersedes", "to": f"blob:{pdf}"}
    suggestion = await propose(db, artifact_id, "add_relation", relation, [{"v": 1, "kind": "document", "blob": pdf}])

    assert await decide(db, device, accepting(suggestion)) == {"result": "applied"}

    assert rows(await feed(db), "relation") == {relation["id"]: relation}


async def test_accepting_the_contract_supersedes_example_leaves_its_evidence_on_the_suggestion(db, device, blob_store, tmp_path):
    example = contract_fixture("valid", "suggestion/suggestion", "add_supersedes")
    for content in (b"foo", b"bar"):
        await store_bytes(blob_store, tmp_path, content)
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id, "add_relation", example["proposed"], example["evidence"], example["fingerprint"])

    assert await decide(db, device, accepting(suggestion)) == {"result": "applied"}

    changes = await feed(db)
    assert rows(changes, "relation") == {example["proposed"]["id"]: example["proposed"]}
    assert rows(changes, "suggestion")[suggestion["id"]]["evidence"] == example["evidence"]


@pytest.mark.parametrize(("missing", "relation_type"), [(f"blob:{'f' * 64}", "supersedes"), (f"item:{COURSE}", "evidence")])
async def test_a_relation_whose_end_the_hub_lacks_is_rejected_with_nothing_written(db, device, blob_store, tmp_path, missing, relation_type):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    relation = {"id": f"{missing}|{relation_type}|blob:{pdf}", "from": missing, "type": relation_type, "to": f"blob:{pdf}"}
    suggestion = await propose(db, artifact_id, "add_relation", relation, [page_at(pdf)])
    before = await feed(db)

    assert await decide(db, device, accepting(suggestion)) == {"result": "rejected", "reason": "link_target_missing"}

    assert await feed(db) == before


@pytest.mark.parametrize("action", ["create_item", "patch_item"])
async def test_evidence_on_something_the_hub_no_longer_holds_rejects_the_accept_with_nothing_written(db, device, blob_store, tmp_path, action):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    proposal = ASSIGNMENT if action == "create_item" else due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z")
    purged = new_id()
    purged_ref = EntityRef(Entity.CAPTURE, purged)
    await db.write(lambda tx: store.write(tx, purged_ref, {"id": purged, "device_id": new_id(), "created_at": "2026-03-02T00:30:00.000Z",
                                                           "origin": {"type": "manual"}}))
    suggestion = await propose(db, artifact_id, action, proposal, [page_at(pdf), {**SPOKEN, "capture": purged}])
    await db.write(lambda tx: store.write(tx, purged_ref, None))
    before = await feed(db)

    assert await decide(db, device, accepting(suggestion)) == {"result": "rejected", "reason": "link_target_missing"}

    assert await feed(db) == before


EDITS_THE_SUGGESTION_CANNOT_TAKE = {
    "create_item: a column it did not propose": ("create_item", {"body": "본문"}),
    "create_item: a field it did not propose": ("create_item", {"fields.submission": "온라인 제출"}),
    "create_item: a field value the kind does not take": ("create_item", {"fields.due_text": 316}),
    "create_item: a required field left out": ("create_item", {"fields.course_id": None}),
    "create_item: a date that does not follow all_day": ("create_item", {"due_at": {"date": "2026-03-16", "tz": "Asia/Seoul"}}),
    "patch_item: a key it did not propose": ("patch_item", {"title": "과제 3"}),
    "patch_item: a field value the kind does not take": ("patch_item", {"fields.due_text": 316}),
    "add_relation: any edit": ("add_relation", {"title": "과제 3"}),
}


@pytest.mark.parametrize(("action", "edits"), EDITS_THE_SUGGESTION_CANNOT_TAKE.values(), ids=EDITS_THE_SUGGESTION_CANNOT_TAKE.keys())
async def test_edits_the_suggestion_cannot_take_are_rejected_as_schema_and_change_nothing(db, device, blob_store, tmp_path, action, edits):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item("create_course")
    await apply(db, device, create)
    proposals = {
        "create_item": ASSIGNMENT,
        "patch_item": {"entity": "item", "id": item_id, "set": {"fields.room": {"from": "공학관 301", "to": "공학관 402"}}},
        "add_relation": {"id": f"item:{item_id}|about|capture:{CAPTURE}", "from": f"item:{item_id}", "type": "about", "to": f"capture:{CAPTURE}"},
    }
    if action == "patch_item":
        edits = {"fields.room" if key == "fields.due_text" else key: value for key, value in edits.items()}
    suggestion = await propose(db, artifact_id, action, proposals[action], [page_at(pdf)])
    mutation = accepting(suggestion, edits)
    assert validate("mutation/mutation", mutation) == []
    before = await feed(db)

    assert await decide(db, device, mutation) == {"result": "rejected", "reason": "schema"}

    assert await feed(db) == before


@pytest.mark.parametrize(
    ("action", "proposal"),
    [
        ("create_item", {"kind": "club_meeting", "title": "동아리 모임", "fields": {}}),
        ("add_relation", {"id": f"blob:{'e' * 64}|mentions|capture:{CAPTURE}", "from": f"blob:{'e' * 64}", "type": "mentions",
                          "to": f"capture:{CAPTURE}"}),
    ],
    ids=["item of a kind this hub has no fields for", "relation of a type clients cannot create"],
)
async def test_a_proposal_this_hub_cannot_write_is_rejected_as_schema_with_nothing_written(db, device, blob_store, tmp_path, action, proposal):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id, action, proposal, [page_at(pdf)])
    before = await feed(db)

    assert await decide(db, device, accepting(suggestion)) == {"result": "rejected", "reason": "schema"}

    assert await feed(db) == before


async def test_dismissing_records_the_decision_and_writes_no_user_data(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id, evidence=[page_at(pdf)])

    assert await decide(db, device, dismissing(suggestion)) == {"result": "applied"}

    changes = await feed(db)
    assert rows(changes, "suggestion")[suggestion["id"]] == {**suggestion, "state": "dismissed"}
    assert (rows(changes, "item"), rows(changes, "relation")) == ({}, {})


@pytest.mark.parametrize("decision", [accepting, dismissing])
async def test_a_decision_on_a_suggestion_the_hub_lacks_is_rejected_as_deleted(db, device, decision):
    assert await decide(db, device, decision({"id": new_id()})) == {"result": "rejected", "reason": "deleted"}


@pytest.mark.parametrize(
    ("first", "then", "answer"),
    [
        (accepting, accepting, {"result": "conflict", "applied_fields": [], "conflicted_fields": ["state"]}),
        (accepting, dismissing, {"result": "conflict", "applied_fields": [], "conflicted_fields": ["state"]}),
        (dismissing, accepting, {"result": "conflict", "applied_fields": [], "conflicted_fields": ["state"]}),
        (dismissing, dismissing, {"result": "applied"}),
    ],
    ids=["accept after accept", "dismiss after accept", "accept after dismiss", "dismiss after dismiss"],
)
async def test_a_decision_on_a_suggestion_decided_before_changes_nothing(db, device, blob_store, tmp_path, first, then, answer):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id)
    await decide(db, device, first(suggestion))
    before = await feed(db)

    assert await decide(db, device, then(suggestion)) == answer

    assert await feed(db) == before


@pytest.mark.parametrize("decision", [accepting, dismissing])
async def test_a_superseded_suggestion_takes_no_decision(db, device, blob_store, tmp_path, decision):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    older = await propose(db, artifact_id)
    newer_id, _ = await grounds(db, blob_store, tmp_path, pipeline="suggest-2", recorded_at=LATER)
    await propose(db, newer_id)
    before = await feed(db)

    assert await decide(db, device, decision(older)) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["state"]}

    assert await feed(db) == before


async def test_a_suggestion_of_an_action_this_hub_does_not_know_is_dismissed_but_not_accepted(db, device):
    unknown = contract_fixture("forward", "suggestion/suggestion", "action_unknown")
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.SUGGESTION, unknown["id"]), unknown))

    assert await decide(db, device, accepting(unknown)) == {"result": "rejected", "reason": "schema"}
    assert await decide(db, device, dismissing(unknown)) == {"result": "applied"}

    assert await state_of(db, unknown) == "dismissed"
