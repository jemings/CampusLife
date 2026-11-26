import copy
import uuid

import pytest
from campuslife_contracts import Rule, RuleViolation, SchemaViolation, validate
from conftest import contract_fixture

from campuslife_hub import changes, mutations, store
from campuslife_hub.blobs import BlobStore
from campuslife_hub.devices import ensure_hub_device
from campuslife_hub.mutations import NoMutationId, Reason, UnsupportedOp, reason_for
from campuslife_hub.outcomes import Applied
from campuslife_hub.store import Entity, EntityRef

ITEM_A = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
ITEM_B = "16fd2706-8baf-433b-82eb-8c7fada847da"


def new_id():
    return str(uuid.uuid4())


def commit(capture, mutation_id=None):
    return {"mutation_id": mutation_id or new_id(), "op": "capture.commit", "capture": capture}


def course(item_id):
    return {"id": item_id, "kind": "course", "schema_version": 1, "title": "자료구조", "fields": {}, "rev": 0}


@pytest.fixture
def blob_store(tmp_path, db):
    return BlobStore(db, blobs_dir=tmp_path / "blobs", staging_dir=tmp_path / "staging")


@pytest.fixture
def device(db):
    import asyncio

    return asyncio.run(db.write(ensure_hub_device))


async def store_bytes(blob_store, tmp_path, content: bytes) -> str:
    path = tmp_path / f"src-{uuid.uuid4()}"
    path.write_bytes(content)
    return (await blob_store.put_file(path)).sha256


async def text_capture(blob_store, tmp_path, content=b"lecture notes"):
    sha = await store_bytes(blob_store, tmp_path, content)
    return {
        "id": new_id(),
        "created_at": "2026-03-02T00:30:00.000Z",
        "origin": {"type": "import"},
        "links": [],
        "attachments": [{"id": new_id(), "blob_sha256": sha, "role": "file", "original_filename": "노트.txt", "cloud_policy": "local_only"}],
    }


async def apply(db, device, *batch):
    results = await mutations.apply(db, list(batch), device)
    assert validate("mutation/batch_response", {"results": results}) == []
    return results


async def feed(db):
    body = await db.read(lambda snap: changes.page(snap, 0, 500))
    assert validate("sync/changes_page", body) == []
    return body["changes"]


async def test_a_commit_writes_the_capture_its_attachments_and_a_relation_per_attachment_and_link(db, device, blob_store, tmp_path):
    capture = contract_fixture("valid", "capture/commit", "recording_with_links")
    for attachment in capture["attachments"]:
        attachment["blob_sha256"] = await store_bytes(blob_store, tmp_path, attachment["id"].encode())
    for item_id in (ITEM_A, ITEM_B):
        await db.write(lambda tx, item_id=item_id: store.write(tx, EntityRef(Entity.ITEM, item_id), course(item_id)))
    mutation = commit(capture)

    assert await apply(db, device, mutation) == [{"mutation_id": mutation["mutation_id"], "result": "applied"}]

    rows = {(row["entity"], row["id"]): row["data"] for row in await feed(db)}
    expected_capture = {key: value for key, value in capture.items() if key not in ("links", "attachments")}
    assert rows[("capture", capture["id"])] == {**expected_capture, "device_id": device}
    for attachment in capture["attachments"]:
        assert rows[("attachment", attachment["id"])] == {**attachment, "capture_id": capture["id"]}
    relations = sorted(data["id"] for (entity, _), data in rows.items() if entity == "relation")
    assert relations == sorted(
        f"attachment:{attachment['id']}|about|item:{item_id}" for attachment in capture["attachments"] for item_id in (ITEM_A, ITEM_B)
    )


async def test_a_capture_without_attachments_links_from_the_capture(db, device):
    capture = contract_fixture("valid", "capture/commit", "text_only")
    capture["links"] = [{"type": "about", "to": f"item:{ITEM_A}"}]
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.ITEM, ITEM_A), course(ITEM_A)))

    assert (await apply(db, device, commit(capture)))[0]["result"] == "applied"
    relation_ids = [row["id"] for row in await feed(db) if row["entity"] == "relation"]
    assert relation_ids == [f"capture:{capture['id']}|about|item:{ITEM_A}"]


async def test_the_same_link_twice_makes_one_relation(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    capture["links"] = [{"type": "about", "to": f"item:{ITEM_A}"}] * 2
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.ITEM, ITEM_A), course(ITEM_A)))

    assert (await apply(db, device, commit(capture)))[0]["result"] == "applied"
    assert len([row for row in await feed(db) if row["entity"] == "relation"]) == 1


async def test_resending_a_mutation_answers_duplicate_with_the_first_result(db, device, blob_store, tmp_path):
    mutation = commit(await text_capture(blob_store, tmp_path))
    first = await apply(db, device, mutation)
    before = await feed(db)

    again = await apply(db, device, mutation)

    assert again == [{"mutation_id": mutation["mutation_id"], "result": "duplicate", "original": first[0]}]
    assert await feed(db) == before


async def test_the_same_capture_under_another_mutation_id_is_applied_without_writing(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    await apply(db, device, commit(capture))
    before = await feed(db)

    other = commit(copy.deepcopy(capture))
    assert await apply(db, device, other) == [{"mutation_id": other["mutation_id"], "result": "applied"}]
    assert await feed(db) == before


async def test_the_same_capture_id_with_other_content_is_rejected_as_id_reuse(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    await apply(db, device, commit(capture))
    before = await feed(db)

    changed = {**capture, "user_note": "다른 내용"}
    mutation = commit(changed)
    assert await apply(db, device, mutation) == [{"mutation_id": mutation["mutation_id"], "result": "rejected", "reason": "id_reuse"}]
    assert await feed(db) == before


async def test_an_attachment_id_already_used_by_another_capture_is_id_reuse(db, device, blob_store, tmp_path):
    first = await text_capture(blob_store, tmp_path)
    await apply(db, device, commit(first))
    second = await text_capture(blob_store, tmp_path, b"other")
    second["attachments"][0]["id"] = first["attachments"][0]["id"]

    result = (await apply(db, device, commit(second)))[0]
    assert (result["result"], result.get("reason")) == ("rejected", "id_reuse")


async def test_a_commit_naming_a_blob_the_hub_lacks_asks_for_a_retry_and_is_not_remembered(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    missing = "ab" * 32
    capture["attachments"].append({"id": new_id(), "blob_sha256": missing, "role": "photo", "cloud_policy": "local_only"})
    capture["attachments"].append({"id": new_id(), "blob_sha256": missing, "role": "photo", "cloud_policy": "local_only"})
    mutation = commit(capture)

    assert await apply(db, device, mutation) == [{"mutation_id": mutation["mutation_id"], "result": "retry", "missing_blobs": [missing]}]
    assert await feed(db) == []

    capture["attachments"][1]["blob_sha256"] = capture["attachments"][2]["blob_sha256"] = await store_bytes(blob_store, tmp_path, b"photo")
    assert (await apply(db, device, mutation))[0]["result"] == "applied"


async def test_mutations_after_a_retry_are_not_attempted(db, device, blob_store, tmp_path):
    waiting = await text_capture(blob_store, tmp_path)
    waiting["attachments"][0]["blob_sha256"] = "cd" * 32
    ready = commit(await text_capture(blob_store, tmp_path, b"ready"))

    results = await apply(db, device, commit(waiting), ready)

    assert [result["result"] for result in results] == ["retry", "not_attempted"]
    assert await feed(db) == []
    assert (await apply(db, device, ready))[0]["result"] == "applied"


async def test_a_link_to_an_item_the_hub_lacks_is_rejected_and_writes_nothing(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    capture["links"] = [{"type": "about", "to": f"item:{ITEM_A}"}]

    result = (await apply(db, device, commit(capture)))[0]
    assert (result["result"], result.get("reason")) == ("rejected", "link_target_missing")
    assert await feed(db) == []


async def test_a_link_to_a_deleted_item_is_rejected(db, device, blob_store, tmp_path):
    ref = EntityRef(Entity.ITEM, ITEM_A)
    await db.write(lambda tx: store.write(tx, ref, course(ITEM_A)))
    await db.write(lambda tx: store.write(tx, ref, None))
    capture = await text_capture(blob_store, tmp_path)
    capture["links"] = [{"type": "about", "to": f"item:{ITEM_A}"}]

    assert (await apply(db, device, commit(capture)))[0].get("reason") == "link_target_missing"


async def test_a_string_that_is_not_nfc_is_rejected_as_not_canonical(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    capture["user_note"] = "가"

    result = (await apply(db, device, commit(capture)))[0]
    assert (result["result"], result.get("reason")) == ("rejected", "not_canonical")
    assert await feed(db) == []


async def test_a_raw_file_name_keeps_its_received_form(db, device, blob_store, tmp_path):
    capture = contract_fixture("valid", "capture/commit", "shared_file_nfd_name")
    capture["attachments"][0]["blob_sha256"] = await store_bytes(blob_store, tmp_path, b"pdf bytes")

    assert (await apply(db, device, commit(capture)))[0]["result"] == "applied"
    attachment = next(row["data"] for row in await feed(db) if row["entity"] == "attachment")
    assert attachment["original_filename_raw"] == capture["attachments"][0]["original_filename_raw"]


async def test_a_commit_that_breaks_the_schema_is_rejected_and_its_resend_is_a_duplicate(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    del capture["created_at"]
    mutation = commit(capture)

    first = await apply(db, device, mutation)
    assert first == [{"mutation_id": mutation["mutation_id"], "result": "rejected", "reason": "schema"}]
    assert (await apply(db, device, mutation))[0] == {"mutation_id": mutation["mutation_id"], "result": "duplicate", "original": first[0]}


async def test_a_batch_with_a_mutation_lacking_a_usable_id_is_refused_before_anything_is_applied(db, device, blob_store, tmp_path):
    good = commit(await text_capture(blob_store, tmp_path))
    for broken in ({"op": "capture.commit"}, {"mutation_id": "not-a-uuid", "op": "capture.commit"}, "not an object"):
        with pytest.raises(NoMutationId):
            await mutations.apply(db, [good, broken], device)
    assert await feed(db) == []


async def test_every_mutation_after_a_retry_is_not_attempted(db, device, blob_store, tmp_path):
    waiting = await text_capture(blob_store, tmp_path)
    waiting["attachments"][0]["blob_sha256"] = "cd" * 32
    later = [commit(await text_capture(blob_store, tmp_path, f"later {index}".encode())) for index in range(2)]

    results = await apply(db, device, commit(waiting), *later)

    assert [result["result"] for result in results] == ["retry", "not_attempted", "not_attempted"]


async def test_a_rejected_mutation_does_not_stop_the_batch(db, device, blob_store, tmp_path):
    broken = await text_capture(blob_store, tmp_path)
    del broken["created_at"]
    ready = commit(await text_capture(blob_store, tmp_path, b"ready"))

    results = await apply(db, device, commit(broken), ready)

    assert [result["result"] for result in results] == ["rejected", "applied"]


async def test_a_batch_with_an_op_this_hub_does_not_apply_yet_is_refused_before_anything_is_written(db, device, blob_store, tmp_path):
    good = commit(await text_capture(blob_store, tmp_path))
    create = {"mutation_id": new_id(), "op": "create", "entity": "item", "id": ITEM_A, "data": {"kind": "note", "title": "메모", "fields": {}}}

    with pytest.raises(UnsupportedOp):
        await mutations.apply(db, [good, create], device)

    assert await db.read(lambda snap: snap.execute("SELECT count(*) FROM applied_mutations").fetchone()[0]) == 0
    assert await feed(db) == []
    assert (await apply(db, device, good))[0]["result"] == "applied"


async def test_an_op_outside_the_contract_is_a_schema_rejection_not_a_refused_batch(db, device):
    mutation = {"mutation_id": new_id(), "op": "layer.adopt"}
    assert await apply(db, device, mutation) == [{"mutation_id": mutation["mutation_id"], "result": "rejected", "reason": "schema"}]


async def test_an_op_that_is_not_a_string_is_a_schema_rejection_and_the_batch_goes_on(db, device, blob_store, tmp_path):
    malformed = [{"mutation_id": new_id(), "op": op} for op in (["x"], {"a": 1}, None, 5)]
    malformed.append({"mutation_id": new_id()})
    ready = commit(await text_capture(blob_store, tmp_path))

    results = await apply(db, device, *malformed, ready)

    assert results[:-1] == [{"mutation_id": mutation["mutation_id"], "result": "rejected", "reason": "schema"} for mutation in malformed]
    assert results[-1] == {"mutation_id": ready["mutation_id"], "result": "applied"}


async def test_an_unapplied_contract_op_refuses_the_batch_even_when_it_breaks_the_schema(db, device, blob_store, tmp_path):
    good = commit(await text_capture(blob_store, tmp_path))
    bare_create = {"mutation_id": new_id(), "op": "create"}

    with pytest.raises(UnsupportedOp):
        await mutations.apply(db, [good, bare_create], device)

    assert await db.read(lambda snap: snap.execute("SELECT count(*) FROM applied_mutations").fetchone()[0]) == 0
    assert await feed(db) == []


def test_a_mutation_breaking_both_owner_and_the_schema_is_a_schema_rejection():
    patch = {
        "mutation_id": new_id(), "op": "patch", "entity": "item", "id": ITEM_A,
        "set": {"rev": {"from": 0, "to": 1}}, "unknown_member": 1,
    }
    violations = validate("mutation/mutation", patch)
    assert {type(violation) for violation in violations} == {RuleViolation, SchemaViolation}
    assert any(isinstance(violation, RuleViolation) and violation.rule == Rule.OWNER for violation in violations)
    assert reason_for(violations) == Reason.SCHEMA


def test_contract_rules_map_to_reasons_with_schema_as_the_default():
    nfc = RuleViolation("/capture/user_note", Rule.NFC)
    owner = RuleViolation("/set/rev", Rule.OWNER)
    set_type = RuleViolation("/set/title", Rule.SET_TYPE)
    missing = SchemaViolation("/capture/created_at", "required")
    assert reason_for([nfc]) == Reason.NOT_CANONICAL
    assert reason_for([owner]) == Reason.OWNER
    assert reason_for([set_type]) == Reason.SCHEMA
    assert reason_for([RuleViolation("/x", Rule.CALENDAR), RuleViolation("/y", Rule.RELATION_SELF)]) == Reason.SCHEMA
    assert reason_for([nfc, missing]) == Reason.SCHEMA
    assert reason_for([nfc, owner]) == Reason.OWNER
    assert reason_for([owner, missing]) == Reason.SCHEMA


async def test_two_attachments_of_one_commit_with_the_same_id_are_rejected(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    capture["attachments"].append({**capture["attachments"][0], "original_filename": "다른 이름.txt"})

    result = (await apply(db, device, commit(capture)))[0]
    assert (result["result"], result.get("reason")) == ("rejected", "schema")
    assert await feed(db) == []


async def test_an_op_is_unapplied_only_while_no_handler_is_registered_for_it(db, device, monkeypatch):
    create = contract_fixture("valid", "mutation/mutation", "create_course")
    with pytest.raises(UnsupportedOp):
        await mutations.apply(db, [create], device)

    monkeypatch.setitem(mutations.HANDLERS, "create", lambda tx, mutation, device: Applied())

    assert await apply(db, device, create) == [{"mutation_id": create["mutation_id"], "result": "applied"}]
