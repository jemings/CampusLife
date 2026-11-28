import hashlib
from datetime import timedelta

import pytest
from extract_harness import TEST_LIMITS, fresh_hub, hub_with, valid_body
from test_capture_commit import blob_store, device, feed

from campuslife_hub import jobs, purge
from campuslife_hub.core import open_core
from campuslife_hub.extract import bodies, framework, sniff
from campuslife_hub.extract.extractors import TEXT, Extractor
from campuslife_hub.extract.framework import IDENTIFY
from campuslife_hub.extract.limits import Limits
from campuslife_hub.ingest import INGEST
from campuslife_hub.jobs import Failed, JobState, NotRetryable, Pipeline
from campuslife_hub.purge import PurgeHooks

__all__ = ["blob_store", "device"]

NOTES = "# 1장\n트리는 계층 구조입니다.\n\n둘째 문단".encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


async def change_seq(db):
    (seq,) = await db.read(lambda snap: snap.execute("SELECT change_seq FROM hub_state").fetchone())
    return seq


async def test_a_text_file_is_identified_then_extracted_into_an_extraction_artifact(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("노트.md", NOTES))

    await hub.settle()

    blob = sha(NOTES)
    assert [(job.job_type, job.state, job.outcome_code) for job in await hub.jobs()] == [
        (INGEST, JobState.SUCCEEDED, None), (IDENTIFY, JobState.SUCCEEDED, None), ("extract.text", JobState.SUCCEEDED, None),
    ]
    identify_job, extract_job = (await hub.jobs(IDENTIFY))[0], (await hub.jobs("extract.text"))[0]
    assert (identify_job.subject_ref, identify_job.inputs, identify_job.params) == (f"blob:{blob}", (blob,), {})
    assert (extract_job.subject_ref, extract_job.inputs, extract_job.params) == (f"blob:{blob}", (blob,), {"syntax": "markdown"})
    assert extract_job.pipeline == Pipeline(f"text-{TEXT.version}+L{TEST_LIMITS.hash8()}")
    assert identify_job.pipeline == Pipeline(f"identify-{sniff.VERSION}+L{TEST_LIMITS.hash8()}")
    [identified] = await hub.artifacts("identify")
    identify_body, _ = await hub.body(identified)
    assert identify_body == {"v": 1, "blob": blob, "identifier": {"id": "identify", "version": sniff.VERSION}, "media_type": "text/plain", "blocks": []}
    [extraction] = await hub.artifacts("extraction")
    assert (extraction["subject_ref"], extraction["input_hashes"], extraction["pipeline_version"]) == (f"blob:{blob}", [blob], extract_job.pipeline.version)
    body, stored = await hub.body(extraction)
    assert valid_body("artifact/extraction", body)
    assert stored == bodies.canonical_json(body)
    assert [(block["block_type"], block["text"], block["refs"]) for block in body["blocks"]] == [
        ("heading", "1장", [{"v": 1, "kind": "doc_path", "blob": blob, "heading_path": [], "block_index": 0}]),
        ("paragraph", "트리는 계층 구조입니다.", [{"v": 1, "kind": "doc_path", "blob": blob, "heading_path": ["1장"], "block_index": 0}]),
        ("paragraph", "둘째 문단", [{"v": 1, "kind": "doc_path", "blob": blob, "heading_path": ["1장"], "block_index": 1}]),
    ]
    assert [block["id"] for block in body["blocks"]] == [bodies.block_id(blob, {"id": "text", "version": TEXT.version, "params": {"syntax": "markdown"}}, ordinal) for ordinal in range(3)]
    assert (await hub.media(blob)).media_type == "text/plain"
    attachment = await hub.attachment(capture["attachments"][0]["id"])
    assert attachment["detected_mime"] == "text/markdown"
    [sent] = [change["data"] for change in await feed(db) if change["entity"] == "attachment"]
    assert sent["detected_mime"] == "text/markdown"


async def test_a_blob_under_two_names_is_identified_once_and_extracted_once_per_reading(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.txt", NOTES), ("b.txt", NOTES), ("c.md", NOTES), ("d.markdown", NOTES))

    await hub.settle()

    assert len(await hub.jobs(IDENTIFY)) == 1
    assert sorted(job.params["syntax"] for job in await hub.jobs("extract.text")) == ["markdown", "plain"]
    extractions = await hub.artifacts("extraction")
    assert len(extractions) == 2
    assert len({artifact["content_blob"] for artifact in extractions}) == 2
    plain, markdown = [(await hub.body(artifact))[0] for artifact in extractions]
    assert {block["id"] for block in plain["blocks"]}.isdisjoint(block["id"] for block in markdown["blocks"])
    assert sorted(body["extractor"]["params"]["syntax"] for body in (plain, markdown)) == ["markdown", "plain"]


async def test_a_later_capture_of_an_identified_blob_is_planned_without_identifying_it_again(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.txt", NOTES))
    await hub.settle()
    [identify] = await hub.jobs(IDENTIFY)

    later = await hub.capture(("same notes.md", NOTES))
    await hub.settle()

    assert await hub.jobs(IDENTIFY) == [await hub.job(identify.id)]
    assert (await hub.job(identify.id)).attempts == 1
    assert sorted(job.params["syntax"] for job in await hub.jobs("extract.text")) == ["markdown", "plain"]
    assert (await hub.attachment(later["attachments"][0]["id"]))["detected_mime"] == "text/markdown"


async def test_a_capture_that_comes_while_its_blob_waits_for_identify_is_planned_when_identify_ends(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.txt", NOTES))
    await hub.runner.run_one()
    assert [job.state for job in await hub.jobs(IDENTIFY)] == [JobState.PENDING]

    second = await hub.capture(("b.md", NOTES))
    await hub.settle()

    assert len(await hub.jobs(IDENTIFY)) == 1
    assert sorted(job.params["syntax"] for job in await hub.jobs("extract.text")) == ["markdown", "plain"]
    assert (await hub.attachment(second["attachments"][0]["id"]))["detected_mime"] == "text/markdown"


async def test_planning_again_queues_nothing_new(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()
    before = await hub.jobs()

    planned = await db.write(lambda tx: hub.files.plan(tx, capture["attachments"][0]["id"]))
    await db.write(lambda tx: [hub.registry.enqueue(tx, spec) for spec in planned])

    assert await hub.jobs() == before


async def test_planning_an_attachment_it_already_planned_writes_nothing(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()
    before = await change_seq(db)

    await db.write(lambda tx: hub.files.plan(tx, capture["attachments"][0]["id"]))

    assert await change_seq(db) == before


async def test_a_name_the_bytes_disagree_with_does_not_choose_the_reader(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    pdf = b"%PDF-1.7\n%%EOF\n"
    capture = await hub.capture(("notes.md", pdf), ("slides.pdf", NOTES))

    await hub.settle()

    assert [job.params for job in await hub.jobs("extract.text")] == [{"syntax": "plain"}]
    detected = [(await hub.attachment(attachment["id"]))["detected_mime"] for attachment in capture["attachments"]]
    assert detected == ["application/pdf", "text/plain"]


async def test_a_type_no_extractor_reads_is_identified_and_nothing_more(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("grades.csv", b"name,score\nkim,90\n"))

    await hub.settle()

    assert [(job.job_type, job.state) for job in await hub.jobs()] == [(INGEST, JobState.SUCCEEDED), (IDENTIFY, JobState.SUCCEEDED)]
    assert (await hub.attachment(capture["attachments"][0]["id"]))["detected_mime"] == "text/csv"


@pytest.mark.parametrize("role", ["photo", "audio_segment"])
async def test_photos_and_recordings_are_left_to_their_own_pipelines(db, device, blob_store, tmp_path, role):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.txt", NOTES), role=role)

    await hub.settle()

    assert [job.job_type for job in await hub.jobs()] == [INGEST]
    assert "detected_mime" not in await hub.attachment(capture["attachments"][0]["id"])


async def test_the_same_blob_and_extractor_version_give_the_same_body_and_block_ids_in_another_hub(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.md", NOTES))
    await hub.settle()
    [first] = await hub.artifacts("extraction")

    async with fresh_hub(tmp_path / "other") as other:
        await other.capture(("renamed.md", NOTES))
        await other.settle()
        [second] = await other.artifacts("extraction")

    assert first["id"] != second["id"]
    assert first["content_blob"] == second["content_blob"]


async def test_a_new_extractor_version_makes_a_new_artifact_and_leaves_the_old_one(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.md", NOTES))
    await hub.settle()
    [old] = await hub.artifacts("extraction")
    newer = Extractor("text", "2", TEXT.entry, TEXT.accepts)
    upgraded = hub_with(db, blob_store, device, tmp_path, newer, base=())

    await upgraded.capture(("again.md", NOTES))
    await upgraded.settle()

    old_body, _ = await hub.body(old)
    extractions = await hub.artifacts("extraction")
    assert [artifact["id"] for artifact in extractions][0] == old["id"]
    assert [artifact["pipeline_version"] for artifact in extractions] == [f"text-1+L{TEST_LIMITS.hash8()}", f"text-2+L{TEST_LIMITS.hash8()}"]
    new_body, _ = await hub.body(extractions[1])
    assert [block["text"] for block in new_body["blocks"]] == [block["text"] for block in old_body["blocks"]]
    assert not {block["id"] for block in new_body["blocks"]} & {block["id"] for block in old_body["blocks"]}


async def test_a_purge_of_the_original_takes_its_identify_and_extraction_artifacts_and_its_media_type(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.md", NOTES), ("b.txt", NOTES))
    await hub.settle()
    made = await hub.artifacts()
    assert sorted(artifact["artifact_type"] for artifact in made) == ["extraction", "extraction", "identify"]

    purged = await purge.purge_blob(db, blob_store, sha(NOTES), hooks=PurgeHooks())

    assert sorted(purged.target.artifacts) == sorted(artifact["id"] for artifact in made)
    assert set(purged.target.blobs) == {sha(NOTES), *(artifact["content_blob"] for artifact in made)}
    assert await hub.artifacts() == []
    assert await hub.media(sha(NOTES)) is None


async def test_a_file_imported_again_after_its_purge_is_identified_and_extracted_again(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.md", NOTES))
    await hub.settle()
    await purge.purge_blob(db, blob_store, sha(NOTES), hooks=PurgeHooks())

    again = await hub.capture(("a.md", NOTES))
    await hub.settle()

    assert (await hub.media(sha(NOTES))).media_type == "text/plain"
    assert (await hub.attachment(again["attachments"][0]["id"]))["detected_mime"] == "text/markdown"
    assert len(await hub.artifacts("extraction")) == 1
    assert [job.state for job in await hub.jobs(IDENTIFY)] == [JobState.SUCCEEDED, JobState.SUCCEEDED]
    assert [job.state for job in await hub.jobs("extract.text")] == [JobState.SUCCEEDED, JobState.SUCCEEDED]


async def test_a_file_purged_before_its_jobs_ran_is_processed_when_imported_again(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.md", NOTES))
    await hub.runner.run_one()
    [waiting] = await hub.jobs(IDENTIFY)
    await purge.purge_blob(db, blob_store, sha(NOTES), hooks=PurgeHooks())
    assert (await hub.job(waiting.id)).state is JobState.CANCELLED

    await hub.capture(("a.md", NOTES))
    await hub.settle()

    assert (await hub.media(sha(NOTES))).media_type == "text/plain"
    assert len(await hub.artifacts("extraction")) == 1


async def failing_identify_once(hub, monkeypatch):
    """The file's identify fails once, then the file is purged; the failed job, released."""
    real, calls = framework.run_child, []

    async def flaky(job, request, limits, work):
        calls.append(job.id)
        return Failed("a transient failure") if len(calls) == 1 else await real(job, request, limits, work)

    monkeypatch.setattr(framework, "run_child", flaky)
    await hub.capture(("a.txt", NOTES))
    await hub.settle()
    [failed] = await hub.jobs(IDENTIFY)
    assert failed.state is JobState.FAILED
    await purge.purge_blob(hub.db, hub.blobs, sha(NOTES), hooks=PurgeHooks())
    return failed


async def test_a_file_whose_identify_failed_is_identified_when_imported_again_after_its_purge(db, device, blob_store, tmp_path, monkeypatch):
    hub = hub_with(db, blob_store, device, tmp_path)
    await failing_identify_once(hub, monkeypatch)

    await hub.capture(("a.txt", NOTES))
    await hub.settle()

    assert (await hub.media(sha(NOTES))).media_type == "text/plain"
    assert len(await hub.artifacts("extraction")) == 1


async def test_a_failed_job_whose_blob_went_is_not_retried(db, device, blob_store, tmp_path, monkeypatch):
    hub = hub_with(db, blob_store, device, tmp_path)
    failed = await failing_identify_once(hub, monkeypatch)

    with pytest.raises(NotRetryable):
        await db.write(lambda tx: jobs.retry(tx, failed.id, now=hub.clock()))

    assert (await hub.job(failed.id)).state is JobState.FAILED


async def test_a_blob_that_goes_releases_every_finished_job_that_read_it_whatever_its_subject(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()

    await purge.purge_blob(db, blob_store, sha(NOTES), hooks=PurgeHooks())

    [ingest] = await hub.jobs(INGEST)
    assert (ingest.subject_ref, ingest.job_key) == (f"capture:{capture['id']}", f"{jobs.RELEASED}{ingest.id}")
    assert [job.job_key for job in await hub.jobs()] == [f"{jobs.RELEASED}{job.id}" for job in await hub.jobs()]


async def test_a_blob_stored_again_after_the_collection_took_it_is_identified_again(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    blob = await hub.store(NOTES)
    first = await hub.enqueue(IDENTIFY, blob)
    await hub.settle()
    await blob_store.collect_garbage(hub.clock() + timedelta(days=30))

    assert await hub.store(NOTES) == blob
    second = await hub.enqueue(IDENTIFY, blob)
    await hub.settle()

    assert second.id != first.id
    assert (await hub.job(second.id)).state is JobState.SUCCEEDED
    assert (await hub.media(blob)).media_type == "text/plain"


async def test_a_job_that_waits_for_a_blob_keeps_its_key_when_the_blob_goes(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    blob = await hub.store(NOTES)
    waiting = await hub.enqueue(IDENTIFY, blob)
    await blob_store.collect_garbage(hub.clock() + timedelta(days=30))

    assert await blob_store.open_blob(blob) is None
    assert (await hub.enqueue(IDENTIFY, blob)).id == waiting.id


async def test_the_media_type_of_a_blob_goes_when_the_collection_of_unreferenced_blobs_takes_it(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    blob = await hub.store(NOTES)
    await hub.enqueue(IDENTIFY, blob)
    await hub.settle()
    assert (await hub.media(blob)).media_type == "text/plain"

    await blob_store.collect_garbage(hub.clock() + timedelta(days=30))

    assert await hub.media(blob) is None


async def test_the_work_directories_a_stopped_hub_left_are_removed_when_the_hub_opens(tmp_path):
    root = tmp_path / "root"
    async with open_core(root, create=True, sync_roots=[]) as core:
        left = core.layout.work / "a-job-that-never-ran-again"
    left.mkdir(parents=True)
    (left / "body.json").write_bytes(b"{}")

    async with open_core(root, create=False, sync_roots=[]):
        assert not core.layout.work.exists()


async def test_a_blob_identified_under_another_identify_pipeline_is_identified_again_when_next_planned(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.txt", NOTES))
    await hub.settle()
    stricter = hub_with(db, blob_store, device, tmp_path, limits=Limits(timeout_s=TEST_LIMITS.timeout_s + 1))

    await stricter.capture(("b.txt", NOTES))
    await stricter.settle()

    assert [job.pipeline.version for job in await hub.jobs(IDENTIFY)] == [hub.files.identify_pipeline.version, stricter.files.identify_pipeline.version]
    assert (await hub.media(sha(NOTES))).pipeline_version == stricter.files.identify_pipeline.version
    assert len(await hub.artifacts("identify")) == 2


async def test_an_extractor_reads_its_own_limits_in_the_child_and_identify_its_own(db, device, blob_store, tmp_path):
    """The common decision: each extractor carries its limits, a type of its own with bounds of its format included."""
    from dataclasses import replace

    from campuslife_hub.extract.fakes import PagedLimits, fake
    from campuslife_hub.extract.limits import IDENTIFY_LIMITS

    paged = replace(fake("limits_seen", {"text/plain": {}}), limits=PagedLimits(max_pages=7, timeout_s=29.0))
    hub = hub_with(db, blob_store, device, tmp_path, paged, base=(), limits=None)

    await hub.capture(("a.txt", NOTES))
    await hub.settle()

    [extraction] = await hub.artifacts("extraction")
    [block] = (await hub.body(extraction))[0]["blocks"]
    assert block["text"] == f"PagedLimits {PagedLimits(max_pages=7, timeout_s=29.0)!r}"
    [identify_job] = await hub.jobs(IDENTIFY)
    [extract_job] = await hub.jobs("extract.fake_limits_seen")
    assert identify_job.pipeline.version.endswith(f"+L{IDENTIFY_LIMITS.hash8()}")
    assert extract_job.pipeline.version == f"fake_limits_seen-1+L{paged.limits.hash8()}"
