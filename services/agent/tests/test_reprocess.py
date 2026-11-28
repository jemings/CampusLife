import hashlib

import pytest
from extract_harness import hub_with
from test_capture_commit import blob_store, device

from campuslife_hub import ingest, jobs, purge
from campuslife_hub.extract import framework
from campuslife_hub.extract.extractors import TEXT
from campuslife_hub.extract.fakes import fake
from campuslife_hub.extract.framework import IDENTIFY, Files
from campuslife_hub.ingest import INGEST, REPROCESS, NoSuchCapture
from campuslife_hub.jobs import JobState
from campuslife_hub.purge import PurgeHooks

__all__ = ["blob_store", "device"]

NOTES = "# 1장\n트리는 계층 구조입니다.\n\n둘째 문단".encode()
OTHER = "다른 노트".encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


async def reprocess(hub, capture_ids=None):
    return await hub.db.write(lambda tx: ingest.reprocess(tx, capture_ids, now=hub.clock()))


async def stored_blobs(hub):
    return await hub.db.read(lambda snap: snap.execute("SELECT sha256, size FROM blobs ORDER BY sha256").fetchall())


async def test_a_reprocess_identifies_and_extracts_again_and_keeps_the_original_and_the_block_ids(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("노트.md", NOTES))
    await hub.settle()
    [before] = await hub.artifacts("extraction")
    original = blob_store.path_of(sha(NOTES)).read_bytes()

    done = await reprocess(hub, [capture["id"]])
    await hub.settle()

    assert done.captures == (capture["id"],)
    nonce = {REPROCESS: done.nonce}
    assert [(job.params, job.state) for job in await hub.jobs(INGEST)] == [
        ({"capture_id": capture["id"]}, JobState.SUCCEEDED), ({"capture_id": capture["id"], **nonce}, JobState.SUCCEEDED),
    ]
    assert [(job.params, job.state) for job in await hub.jobs(IDENTIFY)] == [({}, JobState.SUCCEEDED), (nonce, JobState.SUCCEEDED)]
    assert [(job.params, job.state) for job in await hub.jobs("extract.text")] == [
        ({"syntax": "markdown"}, JobState.SUCCEEDED), ({"syntax": "markdown", **nonce}, JobState.SUCCEEDED),
    ]
    first, again = await hub.artifacts("extraction")
    assert first["id"] == before["id"]
    assert again["id"] != first["id"]
    assert again["content_blob"] == first["content_blob"]
    body, _ = await hub.body(again)
    assert body["extractor"]["params"] == {"syntax": "markdown"}
    assert [block["id"] for block in body["blocks"]] == [block["id"] for block in (await hub.body(first))[0]["blocks"]]
    assert blob_store.path_of(sha(NOTES)).read_bytes() == original
    assert len(await hub.artifacts("identify")) == 2


async def test_a_reprocess_hands_the_extractor_its_params_without_the_nonce(db, device, blob_store, tmp_path):
    seen = fake("show_params", {"text/plain": {"held": True}})
    hub = hub_with(db, blob_store, device, tmp_path, seen, base=())
    capture = await hub.capture(("a.txt", OTHER))
    await hub.settle()

    await reprocess(hub, [capture["id"]])
    await hub.settle()

    for extraction in await hub.artifacts("extraction"):
        body, _ = await hub.body(extraction)
        assert body["extractor"]["params"] == {"held": True}
        assert body["blocks"][0]["text"] == '{"held": true}'


async def test_a_reprocess_of_every_capture_reads_a_blob_they_share_once(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    one = await hub.capture(("a.md", NOTES))
    two = await hub.capture(("b.md", NOTES), ("c.txt", OTHER))
    await hub.settle()

    done = await reprocess(hub)
    await hub.settle()

    assert done.captures == (one["id"], two["id"])
    again = [job for job in await hub.jobs() if job.params.get(REPROCESS) == done.nonce]
    assert sorted((job.job_type, job.subject_ref) for job in again) == sorted([
        (INGEST, f"capture:{one['id']}"), (INGEST, f"capture:{two['id']}"),
        (IDENTIFY, f"blob:{sha(NOTES)}"), (IDENTIFY, f"blob:{sha(OTHER)}"),
        ("extract.text", f"blob:{sha(NOTES)}"), ("extract.text", f"blob:{sha(OTHER)}"),
    ])
    assert {job.state for job in again} == {JobState.SUCCEEDED}


async def test_a_reprocess_planned_again_queues_nothing_new(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()
    done = await reprocess(hub, [capture["id"]])
    await hub.settle()
    queued = len(await hub.jobs())

    attachment_id = capture["attachments"][0]["id"]
    planned = await db.write(lambda tx: hub.files.plan(tx, attachment_id, done.nonce))
    await db.write(lambda tx: [hub.registry.enqueue(tx, spec) for spec in planned])

    assert [spec.job_type for spec in planned] == ["extract.text"]
    assert len(await hub.jobs()) == queued


async def test_a_reprocess_waits_for_its_own_identify_before_extracting(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()
    done = await reprocess(hub, [capture["id"]])

    await hub.runner.run_one()

    attachment_id = capture["attachments"][0]["id"]
    planned = await db.write(lambda tx: hub.files.plan(tx, attachment_id, done.nonce))
    assert planned == [framework.identify_spec(sha(NOTES), done.nonce)]
    assert [job.state for job in await hub.jobs("extract.text")] == [JobState.SUCCEEDED]


async def test_a_reprocess_runs_again_a_job_that_failed_for_good(db, device, blob_store, tmp_path):
    crashing = fake("crash", {"text/plain": {}})
    hub = hub_with(db, blob_store, device, tmp_path, crashing, base=())
    capture = await hub.capture(("a.txt", OTHER))
    await hub.settle(retries=2)
    [failed] = await hub.jobs("extract.fake_crash")
    assert failed.state is JobState.FAILED

    done = await reprocess(hub, [capture["id"]])
    await hub.runner.run_due()

    first, again = await hub.jobs("extract.fake_crash")
    assert first.state is JobState.FAILED
    assert again.params == {REPROCESS: done.nonce}
    assert again.attempts == 1


async def test_a_reprocess_of_a_capture_the_hub_does_not_hold_queues_nothing(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()
    queued = len(await hub.jobs())

    with pytest.raises(NoSuchCapture):
        await reprocess(hub, [capture["id"], "8c1f2b4e-0000-4000-8000-000000000000"])

    assert len(await hub.jobs()) == queued


async def test_a_reprocess_of_no_captures_queues_nothing(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)

    done = await reprocess(hub)

    assert done.captures == ()
    assert await hub.jobs() == []


async def test_each_reprocess_has_its_own_nonce(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()

    first = await reprocess(hub, [capture["id"]])
    await hub.settle()
    second = await reprocess(hub, [capture["id"]])
    await hub.settle()

    assert first.nonce != second.nonce
    assert len(await hub.jobs("extract.text")) == 3
    assert len(await hub.artifacts("extraction")) == 3


async def test_a_purge_releases_the_reprocessed_jobs_and_the_file_imported_again_is_processed(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()
    done = await reprocess(hub, [capture["id"]])
    await hub.settle()

    await purge.purge_blob(db, blob_store, sha(NOTES), hooks=PurgeHooks())

    assert all(jobs.is_released(job) for job in await hub.jobs() if job.params.get(REPROCESS) == done.nonce)
    await hub.capture(("a.md", NOTES))
    await hub.settle()
    assert len(await hub.artifacts("extraction")) == 1
    assert (await hub.jobs("extract.text"))[-1].state is JobState.SUCCEEDED


async def test_a_reprocess_leaves_photos_and_recordings_to_their_own_pipelines(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.jpg", b"\xff\xd8\xff\xe0 photo"), role="photo")
    await hub.settle()

    await reprocess(hub, [capture["id"]])
    await hub.settle()

    assert [job.job_type for job in await hub.jobs()] == [INGEST, INGEST]


def test_an_extractor_that_takes_the_reprocess_param_is_refused():
    taking = fake("echo", {"text/plain": {REPROCESS: "mine"}})

    with pytest.raises(ValueError, match=REPROCESS):
        Files((TEXT, taking))
