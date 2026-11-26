import pytest
from conftest import contract_fixture

from campuslife_hub import capture_commit, jobs
from campuslife_hub.ingest import INGEST, INGEST_PIPELINE
from campuslife_hub.jobs import JobState
from test_capture_commit import apply, blob_store, commit, device, feed, new_id, store_bytes, text_capture

__all__ = ["blob_store", "device"]


async def all_jobs(db):
    return await db.read(lambda snap: [jobs.get(snap, row[0]) for row in snap.execute("SELECT id FROM processing_jobs ORDER BY rowid")])


async def ingest_of(db, capture):
    return await db.read(lambda snap: jobs.of_subject(snap, f"capture:{capture['id']}"))


async def test_a_commit_registers_one_pending_capture_ingest_job_for_its_capture(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    second = await store_bytes(blob_store, tmp_path, b"photo")
    capture["attachments"] += [
        {"id": new_id(), "blob_sha256": second, "role": "photo", "cloud_policy": "local_only"},
        {"id": new_id(), "blob_sha256": second, "role": "photo", "cloud_policy": "local_only"},
    ]

    await apply(db, device, commit(capture))

    [ingest] = await all_jobs(db)
    assert (ingest.job_type, ingest.subject_ref, ingest.state, ingest.pipeline) == (INGEST, f"capture:{capture['id']}", JobState.PENDING, INGEST_PIPELINE)
    assert ingest.inputs == (capture["attachments"][0]["blob_sha256"], second)
    assert ingest.params == {"capture_id": capture["id"]}


async def test_a_capture_without_attachments_gets_its_ingest_job_too(db, device):
    capture = contract_fixture("valid", "capture/commit", "text_only")

    assert (await apply(db, device, commit(capture)))[0]["result"] == "applied"

    [ingest] = await ingest_of(db, capture)
    assert ingest.inputs == ()


async def test_two_captures_of_the_same_file_get_a_job_each(db, device, blob_store, tmp_path):
    first = await text_capture(blob_store, tmp_path)
    second = await text_capture(blob_store, tmp_path)

    await apply(db, device, commit(first), commit(second))

    assert [len(await ingest_of(db, capture)) for capture in (first, second)] == [1, 1]


async def test_a_resent_or_repeated_commit_registers_nothing_more(db, device, blob_store, tmp_path):
    capture = await text_capture(blob_store, tmp_path)
    first = commit(capture)

    await apply(db, device, first, first, commit(capture))

    assert len(await all_jobs(db)) == 1


async def test_a_commit_that_is_rejected_or_waits_for_blobs_registers_no_job(db, device, blob_store, tmp_path):
    waiting = await text_capture(blob_store, tmp_path)
    waiting["attachments"][0]["blob_sha256"] = "cd" * 32
    unlinked = await text_capture(blob_store, tmp_path)
    unlinked["links"] = [{"type": "about", "to": "item:7c9e6679-7425-40de-944b-e07fc1f90ae7"}]

    results = await apply(db, device, commit(unlinked), commit(waiting))

    assert [result["result"] for result in results] == ["rejected", "retry"]
    assert await all_jobs(db) == []


async def test_the_ingest_job_is_registered_in_the_commit_transaction(db, device, blob_store, tmp_path, monkeypatch):
    capture = await text_capture(blob_store, tmp_path)
    mutation = commit(capture)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("the queue refused")

    monkeypatch.setattr(capture_commit.jobs, "enqueue", refuse)
    with pytest.raises(RuntimeError):
        await apply(db, device, mutation)
    assert await feed(db) == []
    monkeypatch.undo()

    assert (await apply(db, device, mutation))[0]["result"] == "applied"
    assert len(await ingest_of(db, capture)) == 1
