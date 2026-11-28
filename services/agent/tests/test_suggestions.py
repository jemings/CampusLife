import asyncio
import json
import unicodedata
from datetime import UTC, datetime, timedelta

import pytest
from campuslife_contracts import contract_root, validate
from conftest import contract_fixture
from test_capture_commit import apply, blob_store, device, feed, new_id, store_bytes
from test_uploads import tablet

from campuslife_hub import artifacts, backup, jobs, mutations, store, suggestions
from campuslife_hub.core import open_core
from campuslife_hub.jobs import JobSpec, Pipeline
from campuslife_hub.store import Entity, EntityRef

__all__ = ["blob_store", "device", "tablet"]

COURSE = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
CAPTURE = "0f8fad5b-d9cb-469f-a165-70867728950e"
FINGERPRINT = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
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


async def propose(db, artifact_id, action="create_item", proposal=ASSIGNMENT, evidence=()):
    return await db.write(lambda tx: suggestions.propose(tx, artifact_id, action, proposal, list(evidence)))


def accepting(suggestion, edits=None):
    mutation = {"mutation_id": new_id(), "op": "suggestion.accept", "suggestion_id": suggestion["id"]}
    return mutation if edits is None else {**mutation, "edits": edits}


def dismissing(suggestion):
    return {"mutation_id": new_id(), "op": "suggestion.dismiss", "suggestion_id": suggestion["id"]}


async def decide(db, device, mutation):
    [result] = await apply(db, device, mutation)
    return {name: value for name, value in result.items() if name != "mutation_id"}


def decided(result):
    """What compare-and-set decided, without the ConflictRecords it named."""
    return {name: value for name, value in result.items() if name != "conflict_record_ids"}


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


def test_the_hub_has_a_fingerprint_a_write_and_an_accept_for_each_action_the_contract_knows():
    known = json.loads((contract_root() / "schemas" / "suggestion" / "suggestion.json").read_text(encoding="utf-8"))["$defs"]["known"]

    assert set(suggestions.ACTIONS) == set(known["properties"]["action"]["enum"])


PDF = "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"
PINNED_FINGERPRINTS = {
    "create_item": (ASSIGNMENT, [page_at(PDF), SPOKEN], "d6d41e3414af511b70098fbc4cc38f0932c9ee14bf934b688f3c50760352d5c0"),
    "patch_item": (due_at_patch(COURSE, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"), [page_at(PDF)],
                   "d4f54be5a9fa13adf47e212fea5a12cece9f748ae1ff99322cb3ce093a33ff00"),
    "add_relation": ({"id": f"item:{COURSE}|about|capture:{CAPTURE}", "from": f"item:{COURSE}", "type": "about", "to": f"capture:{CAPTURE}"},
                     [], "d03f9463544c13c57180e7284dced0f4776a133e5030991488935ad655b25452"),
}
"""#9 decision 6, worked by hand: sha256 of the compact JSON of [action, kind, sorted anchors, due_at or starts_at, title
NFC casefolded without spaces or punctuation], [action, item id, sorted [key, to]] or [action, relation id]. A Hub
that computed another value would reopen every fingerprint the user decided."""


@pytest.mark.parametrize("action", PINNED_FINGERPRINTS)
def test_the_fingerprint_of_each_action_is_the_pinned_value(action):
    proposed, evidence, expected = PINNED_FINGERPRINTS[action]
    assert suggestions.fingerprint(action, proposed, evidence) == expected


CREATE_ITEM_KEY = PINNED_FINGERPRINTS["create_item"][2]
SAME_PROPOSAL_WORDED_OTHERWISE = {
    "title in NFD": ({**ASSIGNMENT, "title": unicodedata.normalize("NFD", "과제 2")}, [page_at(PDF), SPOKEN]),
    "title in other spacing and punctuation": ({**ASSIGNMENT, "title": " 과제-2. "}, [page_at(PDF), SPOKEN]),
    "evidence in another order": (ASSIGNMENT, [SPOKEN, page_at(PDF)]),
    "other places on the same anchors": (ASSIGNMENT, [page_at(PDF, 5), page_at(PDF, 9), {**SPOKEN, "start_ms": 0}]),
    "title over two lines": ({**ASSIGNMENT, "title": "과제\n2"}, [page_at(PDF), SPOKEN]),
    "title split by a tab": ({**ASSIGNMENT, "title": "과제\t2"}, [page_at(PDF), SPOKEN]),
    "title with format characters": ({**ASSIGNMENT, "title": "\ufeff과제\u200b 2\u200d"}, [page_at(PDF), SPOKEN]),
    "another start beside the same due date": ({**ASSIGNMENT, "starts_at": "2026-03-10T00:00:00.000Z"}, [page_at(PDF), SPOKEN]),
    "other columns and fields": ({**ASSIGNMENT, "body": "조별 과제", "fields": {"course_id": COURSE}}, [page_at(PDF), SPOKEN]),
}


@pytest.mark.parametrize(("proposed", "evidence"), SAME_PROPOSAL_WORDED_OTHERWISE.values(), ids=SAME_PROPOSAL_WORDED_OTHERWISE.keys())
def test_a_create_item_worded_otherwise_keeps_its_fingerprint(proposed, evidence):
    assert suggestions.fingerprint("create_item", proposed, evidence) == CREATE_ITEM_KEY


def test_a_create_item_title_in_other_case_keeps_its_fingerprint():
    evidence = [page_at(PDF)]

    assert suggestions.fingerprint("create_item", {**ASSIGNMENT, "title": "Lab Report 2"}, evidence) == suggestions.fingerprint(
        "create_item", {**ASSIGNMENT, "title": "LAB REPORT 2"}, evidence)


def test_without_a_due_date_the_start_is_the_date_in_the_fingerprint():
    undated = {key: value for key, value in ASSIGNMENT.items() if key != "due_at"}

    assert suggestions.fingerprint("create_item", undated, []) != suggestions.fingerprint(
        "create_item", {**undated, "starts_at": "2026-03-10T00:00:00.000Z"}, [])


@pytest.mark.parametrize(("title", "other"), [("C# lab", "C lab"), ("Week 3.1", "Week 31")])
def test_decision_6_accepts_that_titles_apart_only_by_punctuation_share_a_fingerprint(title, other):
    """The cost decision 6 took on: dropping punctuation joins these. #8 measures how often it bites."""
    assert suggestions.fingerprint("create_item", {**ASSIGNMENT, "title": title}, []) == suggestions.fingerprint(
        "create_item", {**ASSIGNMENT, "title": other}, [])


ANOTHER_PROPOSAL = {
    "another kind": ({**ASSIGNMENT, "kind": "exam", "fields": {"course_id": COURSE}}, [page_at(PDF), SPOKEN]),
    "another due date": ({**ASSIGNMENT, "due_at": "2026-03-23T14:59:00.000Z"}, [page_at(PDF), SPOKEN]),
    "no due date, so the start": ({key: value for key, value in ASSIGNMENT.items() if key != "due_at"}, [page_at(PDF), SPOKEN]),
    "another title": ({**ASSIGNMENT, "title": "과제 3"}, [page_at(PDF), SPOKEN]),
    "another anchor": (ASSIGNMENT, [page_at("e" * 64), SPOKEN]),
    "one anchor fewer": (ASSIGNMENT, [page_at(PDF)]),
}


@pytest.mark.parametrize(("proposed", "evidence"), ANOTHER_PROPOSAL.values(), ids=ANOTHER_PROPOSAL.keys())
def test_a_create_item_that_differs_in_a_key_part_has_another_fingerprint(proposed, evidence):
    assert suggestions.fingerprint("create_item", proposed, evidence) != CREATE_ITEM_KEY


def test_a_patch_item_fingerprint_follows_its_keys_and_values_not_their_from_or_order():
    proposed, evidence, expected = PINNED_FINGERPRINTS["patch_item"]
    reordered = {**proposed, "set": {"title": {"from": "a", "to": "b"}, **proposed["set"]}}

    assert suggestions.fingerprint("patch_item", {**proposed, "set": {"due_at": {"from": None, "to": "2026-03-23T14:59:00.000Z"}}}, []) == expected
    assert suggestions.fingerprint("patch_item", reordered, evidence) == suggestions.fingerprint(
        "patch_item", {**proposed, "set": {**proposed["set"], "title": {"from": "x", "to": "b"}}}, evidence)
    assert suggestions.fingerprint("patch_item", due_at_patch(COURSE, None, "2026-03-30T14:59:00.000Z"), evidence) != expected


async def test_a_proposal_is_an_open_suggestion_in_the_feed(db, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)

    suggestion = await propose(db, artifact_id, evidence=[page_at(pdf), SPOKEN])

    assert validate("suggestion/suggestion", suggestion) == []
    assert {name: value for name, value in suggestion.items() if name != "id"} == {
        "source_artifact_id": artifact_id, "action": "create_item", "proposed": ASSIGNMENT,
        "evidence": [page_at(pdf), SPOKEN], "fingerprint": suggestions.fingerprint("create_item", ASSIGNMENT, [page_at(pdf), SPOKEN]),
        "state": "open",
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
    other = await propose(db, artifact_id, proposal={**ASSIGNMENT, "title": "과제 3"})
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


@pytest.mark.parametrize("action", ["create_item", "patch_item", "add_relation"])
@pytest.mark.parametrize("anchor", ["blob", "capture"])
async def test_a_proposal_whose_evidence_rests_on_what_the_hub_does_not_hold_is_refused(db, blob_store, tmp_path, anchor, action):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id = new_id()
    proposals = {
        "create_item": ASSIGNMENT,
        "patch_item": due_at_patch(item_id, None, ASSIGNMENT["due_at"]),
        "add_relation": {"id": f"item:{item_id}|evidence|blob:{pdf}", "from": f"item:{item_id}", "type": "evidence", "to": f"blob:{pdf}"},
    }
    missing = {"blob": {"v": 1, "kind": "document", "blob": "e" * 64}, "capture": {**SPOKEN, "capture": new_id()}}[anchor]
    before = await feed(db)

    with pytest.raises(suggestions.InvalidSuggestion, match="does not hold"):
        await propose(db, artifact_id, action, proposals[action], evidence=[page_at(pdf), missing])

    assert await feed(db) == before


@pytest.mark.parametrize("line_break", ["\r\n", "\r"])
async def test_proposed_text_is_kept_with_lf_line_ends_and_accepted_as_it_is(db, device, blob_store, tmp_path, line_break):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item()
    await apply(db, device, create)
    title = f"제목{line_break}둘째 줄"
    created = await propose(db, artifact_id, proposal={**ASSIGNMENT, "title": title, "body": f"1주차{line_break}2주차"})
    patch_set = {"body": {"from_hash": None, "to": f"a{line_break}b"}, "title": {"from": create["data"]["title"], "to": title}}
    patching = await propose(db, artifact_id, "patch_item", {"entity": "item", "id": item_id, "set": patch_set})

    results = [await decide(db, device, accepting(each)) for each in (created, patching)]

    assert (created["proposed"]["body"], patching["proposed"]["set"]["body"]["to"]) == ("1주차\n2주차", "a\nb")
    assert (created["proposed"]["title"], patching["proposed"]["set"]["title"]["to"]) == (title, title)
    assert results == [{"result": "applied"}, {"result": "applied", "applied_fields": ["body", "title"]}]
    items = rows(await feed(db), "item").values()
    assert sorted((item["body"], item["title"]) for item in items) == [("1주차\n2주차", title), ("a\nb", title)]


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
    assert rows(changes, "suggestion")[suggestion["id"]] == {**suggestion, "state": "accepted", "created_item_id": item_id}


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


async def test_only_accepting_a_create_item_records_the_item_it_made(db, device, blob_store, tmp_path):
    """#9 decision 5: another device finds the Item a Suggestion became by created_item_id."""
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    relation = {"id": f"item:{item_id}|about|capture:{CAPTURE}", "from": f"item:{item_id}", "type": "about", "to": f"capture:{CAPTURE}"}
    made = await propose(db, artifact_id)
    patching = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"))
    relating = await propose(db, artifact_id, "add_relation", relation, [page_at(pdf)])

    for suggestion in (made, patching, relating):
        assert (await decide(db, device, accepting(suggestion)))["result"] == "applied"

    changes = await feed(db)
    decided = rows(changes, "suggestion")
    [new_item] = [each for each in rows(changes, "item") if each != item_id]
    assert decided[made["id"]]["created_item_id"] == new_item
    assert "created_item_id" not in decided[patching["id"]] and "created_item_id" not in decided[relating["id"]]


@pytest.mark.parametrize("outcome", ["rejected", "dismissed"])
async def test_a_create_item_not_accepted_records_no_item(db, device, blob_store, tmp_path, outcome):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id)
    mutation = accepting(suggestion, {"fields.course_id": None}) if outcome == "rejected" else dismissing(suggestion)
    await decide(db, device, mutation)

    assert "created_item_id" not in rows(await feed(db), "suggestion")[suggestion["id"]]


async def test_evidence_a_relation_already_has_is_joined_not_repeated(db, device, blob_store, tmp_path):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    earlier = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"),
                            [page_at(pdf, 2)])
    later = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-23T14:59:00.000Z", "2026-03-30T14:59:00.000Z"),
                          [page_at(pdf, 3), page_at(pdf, 2)])

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
                          [page_at(pdf)])
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
    decision = accepting(suggestion)

    result = await decide(db, device, decision)

    assert result == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["due_at"], "conflict_record_ids": [decision["mutation_id"]]}
    changes = await feed(db)
    assert rows(changes, "item")[item_id]["due_at"] == "2026-03-20T14:59:00.000Z"
    assert rows(changes, "conflict_record") == {
        decision["mutation_id"]: {"id": decision["mutation_id"], "entity_ref": f"item:{item_id}", "set": suggestion["proposed"]["set"]},
    }
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

    assert decided(await decide(db, device, accepting(suggestion))) == {"result": "conflict", "applied_fields": [], "conflicted_fields": ["due_at"]}

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

    assert decided(await decide(db, device, accepting(suggestion))) == {"result": "conflict", "applied_fields": ["title"], "conflicted_fields": ["due_at"]}

    assert rows(await feed(db), "relation")[f"item:{item_id}|evidence|blob:{pdf}"]["locators"] == [page_at(pdf)]


async def test_a_patch_item_on_a_deleted_item_is_rejected_as_deleted_and_stays_open_with_no_record(db, device, blob_store, tmp_path):
    """E3: the open Suggestion keeps what it proposes, so no ConflictRecord keeps it again, however often it is accepted."""
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create, {"mutation_id": new_id(), "op": "delete", "entity": "item", "id": item_id})
    suggestion = await propose(db, artifact_id, "patch_item", due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z"),
                               [page_at(pdf)])
    before = await feed(db)

    assert await decide(db, device, accepting(suggestion)) == {"result": "rejected", "reason": "deleted"}
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
    suggestion = await propose(db, artifact_id, "add_relation", example["proposed"], example["evidence"])

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


@pytest.mark.parametrize(
    ("edits", "written"),
    [({"body": "본문"}, {"body": "본문"}), ({"fields.submission": "온라인 제출"}, {"fields": {**ASSIGNMENT["fields"], "submission": "온라인 제출"}})],
    ids=["a column it did not propose", "a field of its kind it did not propose"],
)
async def test_edits_add_to_a_create_item_what_it_did_not_propose(db, device, blob_store, tmp_path, edits, written):
    """#9 decision 3: the user fills in what the model left out, and the Item is judged as a client's create."""
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id)

    assert await decide(db, device, accepting(suggestion, edits)) == {"result": "applied"}

    [item] = rows(await feed(db), "item").values()
    assert item == {"id": item["id"], "schema_version": 1, **ASSIGNMENT, **written, "rev": item["rev"]}


EDITS_THE_SUGGESTION_CANNOT_TAKE = {
    "create_item: a field its kind does not define": ("create_item", {"fields.room": "공학관 301"}),
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


NOT_ACCEPTABLE_AS_PROPOSED = {
    "item of a kind this hub has no fields for": ("create_item", {"kind": "club_meeting", "title": "동아리 모임", "fields": {}}),
    "field its kind does not define": ("create_item", {**ASSIGNMENT, "fields": {**ASSIGNMENT["fields"], "submission_url": "https://lms.example.invalid/a/2"}}),
    "relation of a type clients cannot create": (
        "add_relation", {"id": f"blob:{'e' * 64}|mentions|capture:{CAPTURE}", "from": f"blob:{'e' * 64}", "type": "mentions", "to": f"capture:{CAPTURE}"}),
}
"""Each fits the open Suggestion shape the Hub sends, but accepting it as proposed could only answer rejected{schema}
(#9 decision 12)."""


@pytest.mark.parametrize(("action", "proposal"), NOT_ACCEPTABLE_AS_PROPOSED.values(), ids=NOT_ACCEPTABLE_AS_PROPOSED.keys())
async def test_a_proposal_not_acceptable_as_proposed_is_refused_and_nothing_is_written(db, blob_store, tmp_path, action, proposal):
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    assert validate("suggestion/suggestion", {"id": new_id(), "source_artifact_id": artifact_id, "action": action, "proposed": proposal,
                                              "evidence": [page_at(pdf)], "fingerprint": FINGERPRINT, "state": "open"}) == []
    before = await feed(db)

    with pytest.raises(suggestions.InvalidSuggestion, match="cannot be accepted as proposed"):
        await propose(db, artifact_id, action, proposal, [page_at(pdf)])

    assert await feed(db) == before


@pytest.mark.parametrize(("action", "proposal"), NOT_ACCEPTABLE_AS_PROPOSED.values(), ids=NOT_ACCEPTABLE_AS_PROPOSED.keys())
async def test_a_stored_suggestion_not_acceptable_as_proposed_is_rejected_as_schema_with_nothing_written(db, device, action, proposal):
    """A row an earlier Hub stored before propose refused such proposals."""
    kept = {"id": new_id(), "source_artifact_id": new_id(), "action": action, "proposed": proposal, "evidence": [],
            "fingerprint": FINGERPRINT, "state": "open"}
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.SUGGESTION, kept["id"]), kept))
    before = await feed(db)

    assert await decide(db, device, accepting(kept)) == {"result": "rejected", "reason": "schema"}

    assert await feed(db) == before


async def test_a_patch_item_setting_a_field_its_items_kind_does_not_define_is_refused_and_nothing_is_written(db, device, blob_store, tmp_path):
    """The kind of an Item never changes (decision 4), so neither does the answer of accepting such a patch."""
    artifact_id, pdf = await grounds(db, blob_store, tmp_path)
    task_id, create = created_item()
    await apply(db, device, create)
    before = await feed(db)

    with pytest.raises(suggestions.InvalidSuggestion, match="cannot be accepted as proposed"):
        await propose(db, artifact_id, "patch_item", {"entity": "item", "id": task_id, "set": {"fields.course_id": {"from": None, "to": COURSE}}})

    assert await feed(db) == before


async def test_a_patch_item_leaving_out_a_field_its_items_kind_does_not_define_is_proposed_and_applied(db, device, blob_store, tmp_path):
    """Setting such a field to null writes nothing to it, so the patch can be accepted (#9 decision 12)."""
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    task_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    proposal = due_at_patch(task_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z", **{"fields.room": {"from": None, "to": None}})

    suggestion = await propose(db, artifact_id, "patch_item", proposal)

    assert (await decide(db, device, accepting(suggestion)))["result"] == "applied"
    assert rows(await feed(db), "item")[task_id]["due_at"] == "2026-03-23T14:59:00.000Z"


async def gone_for_good(db, blob_store, tmp_path, device):
    """An Item purged away (its tombstone) and a Blob a purge removed: neither can come back."""
    item_id, create = created_item()
    await apply(db, device, create)
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.ITEM, item_id), None))
    purged = "d" * 64
    await db.write(lambda tx: tx.execute("INSERT INTO purge_journal (id, target, registered_at) VALUES (?, ?, ?)",
                                         (new_id(), json.dumps({"blobs": [purged]}), "2026-03-02T00:00:00.000Z")))
    return f"item:{item_id}", f"blob:{purged}"


def relation(source, relation_type, target):
    return {"id": f"{source}|{relation_type}|{target}", "from": source, "type": relation_type, "to": target}


@pytest.mark.parametrize("target", ["patch_item on a purged Item", "relation from a purged Item", "relation to a purged Item",
                                    "relation to a purged Blob"])
async def test_a_proposal_on_what_is_gone_for_good_is_refused_and_nothing_is_written(db, device, blob_store, tmp_path, target):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    item, blob = await gone_for_good(db, blob_store, tmp_path, device)
    action, proposal = {
        "patch_item on a purged Item": ("patch_item", due_at_patch(item.removeprefix("item:"), None, "2026-03-23T14:59:00.000Z")),
        "relation from a purged Item": ("add_relation", relation(item, "about", f"capture:{CAPTURE}")),
        "relation to a purged Item": ("add_relation", relation(f"capture:{CAPTURE}", "about", item)),
        "relation to a purged Blob": ("add_relation", relation(f"capture:{CAPTURE}", "about", blob)),
    }[target]
    before = await feed(db)

    with pytest.raises(suggestions.InvalidSuggestion, match="gone for good"):
        await propose(db, artifact_id, action, proposal)

    assert await feed(db) == before


async def test_a_relation_to_what_the_hub_does_not_hold_yet_is_proposed(db, device, blob_store, tmp_path):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)

    suggestion = await propose(db, artifact_id, "add_relation", relation(f"capture:{CAPTURE}", "about", f"item:{new_id()}"))

    assert suggestion["state"] == "open"


async def test_a_patch_item_on_an_item_the_hub_does_not_hold_is_proposed_and_its_accept_answers_deleted(db, device, blob_store, tmp_path):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id, "patch_item", due_at_patch(new_id(), None, "2026-03-23T14:59:00.000Z"))
    before = await feed(db)

    assert await decide(db, device, accepting(suggestion)) == {"result": "rejected", "reason": "deleted"}

    assert await feed(db) == before
    assert await db.read(lambda snap: snap.execute("SELECT count(*) FROM conflict_records").fetchone()[0]) == 0


async def test_a_patch_item_change_with_members_a_patch_does_not_take_is_accepted_without_them(db, device, blob_store, tmp_path):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    item_id, create = created_item(due_at="2026-03-16T14:59:00.000Z")
    await apply(db, device, create)
    proposal = due_at_patch(item_id, "2026-03-16T14:59:00.000Z", "2026-03-23T14:59:00.000Z")
    proposal["set"]["due_at"]["confidence"] = 0.8

    suggestion = await propose(db, artifact_id, "patch_item", proposal)

    assert await decide(db, device, accepting(suggestion)) == {"result": "applied", "applied_fields": ["due_at"]}
    assert rows(await feed(db), "item")[item_id]["due_at"] == "2026-03-23T14:59:00.000Z"


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


@pytest.mark.parametrize("second", [accepting, dismissing])
async def test_two_devices_deciding_at_once_leave_one_decision_and_the_later_gets_a_state_conflict(db, device, tablet, blob_store, tmp_path, second):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id)

    phone_results, tablet_results = await asyncio.gather(
        mutations.apply(db, [accepting(suggestion)], device), mutations.apply(db, [second(suggestion)], tablet))

    answers = sorted(result["result"] for result in (*phone_results, *tablet_results))
    assert answers == ["applied", "conflict"]
    [lost] = [result for result in (*phone_results, *tablet_results) if result["result"] == "conflict"]
    assert (lost["applied_fields"], lost["conflicted_fields"]) == ([], ["state"])
    changes = await feed(db)
    decided = rows(changes, "suggestion")[suggestion["id"]]
    assert len(rows(changes, "item")) == (1 if decided["state"] == "accepted" else 0)


@pytest.mark.parametrize("decision", [accepting, dismissing])
async def test_a_decision_sent_again_gets_the_first_answer_as_a_duplicate_and_changes_nothing(db, device, blob_store, tmp_path, decision):
    artifact_id, _ = await grounds(db, blob_store, tmp_path)
    suggestion = await propose(db, artifact_id)
    mutation = decision(suggestion)
    first = await decide(db, device, mutation)
    before = await feed(db)

    assert await decide(db, device, mutation) == {"result": "duplicate", "original": {"mutation_id": mutation["mutation_id"], **first}}

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


async def test_an_accept_resent_to_a_hub_restored_from_before_it_makes_the_same_item(tmp_path):
    """D24 decision 5a: the accept names the Item it makes, so after a restore the client's kept Mutations, resent
    in order, find the Item their later patches name."""
    drive = tmp_path / "drive"
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        artifact_id, _ = await grounds(core.db, core.blobs, tmp_path)
        suggestion = await propose(core.db, artifact_id)
        await backup.back_up(core, drive)
        accept = accepting(suggestion)
        await mutations.apply(core.db, [accept], core.hub_device)
        [item_id] = rows(await feed(core.db), "item")
        retitle = {"mutation_id": new_id(), "op": "patch", "entity": "item", "id": item_id, "set": {"title": {"from": "과제 2", "to": "과제 2 제출"}}}
        await mutations.apply(core.db, [retitle], core.hub_device)
    await backup.restore(drive, tmp_path / "restored", sync_roots=[])

    async with open_core(tmp_path / "restored", create=False, sync_roots=[]) as core:
        results = await mutations.apply(core.db, [accept, retitle], core.hub_device)
        items = rows(await feed(core.db), "item")

    assert [result["result"] for result in results] == ["applied", "applied"]
    assert {identifier: item["title"] for identifier, item in items.items()} == {item_id: "과제 2 제출"}
