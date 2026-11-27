"""What the file pipeline tells the local UI about each Attachment (S13)."""

import pytest
from extract_harness import hub_with
from test_capture_commit import blob_store, device

from campuslife_hub import ingest
from campuslife_hub.extract.fakes import fake
from campuslife_hub.extract.framework import IDENTIFY
from campuslife_hub.ingest import REPROCESS
from campuslife_hub.jobs import JobState, OutcomeCode

__all__ = ["blob_store", "device"]

NOTES = "# 1장\n트리는 계층 구조입니다.".encode()


async def status(hub, attachment):
    return await hub.db.read(lambda snap: hub.files.status(snap, attachment["id"]))


def kinds(found):
    return [(job.job_type, job.params, job.state, job.outcome_code) for job in found.jobs]


async def test_a_file_waiting_for_identify_shows_its_identify_job(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.runner.run_one()

    found = await status(hub, capture["attachments"][0])

    assert (found.detected_mime, found.facts) == (None, ())
    assert kinds(found) == [(IDENTIFY, {}, JobState.PENDING, None)]


async def test_a_file_not_yet_planned_shows_no_job(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))

    found = await status(hub, capture["attachments"][0])

    assert (found.detected_mime, found.facts, found.jobs) == (None, (), ())


async def test_a_processed_file_shows_its_media_type_and_each_job_it_needed(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES), ("b.txt", NOTES))
    await hub.settle()

    markdown, plain = [await status(hub, attachment) for attachment in capture["attachments"]]

    assert (markdown.detected_mime, markdown.facts) == ("text/markdown", ())
    assert kinds(markdown) == [(IDENTIFY, {}, JobState.SUCCEEDED, None), ("extract.text", {"syntax": "markdown"}, JobState.SUCCEEDED, None)]
    assert plain.detected_mime == "text/plain"
    assert kinds(plain) == [(IDENTIFY, {}, JobState.SUCCEEDED, None), ("extract.text", {"syntax": "plain"}, JobState.SUCCEEDED, None)]


async def test_a_name_the_bytes_disagree_with_is_said(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("slides.pdf", NOTES))
    await hub.settle()

    found = await status(hub, capture["attachments"][0])

    assert found.detected_mime == "text/plain"
    assert found.facts == ("its name or declared type says another kind of file than its bytes, which are text/plain",)
    assert kinds(found)[1][:2] == ("extract.text", {"syntax": "plain"})


async def test_a_type_no_extractor_reads_is_said(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("grades.csv", b"name,score\nkim,90\n"))
    await hub.settle()

    found = await status(hub, capture["attachments"][0])

    assert found.facts == ("no extractor reads text/csv",)
    assert kinds(found) == [(IDENTIFY, {}, JobState.SUCCEEDED, None)]


async def test_a_failed_extract_shows_its_outcome_and_error(db, device, blob_store, tmp_path):
    refusing = fake("refuse", {"text/plain": {"status": "corrupt"}})
    hub = hub_with(db, blob_store, device, tmp_path, refusing, base=())
    capture = await hub.capture(("a.txt", NOTES))
    await hub.settle()

    found = await status(hub, capture["attachments"][0])

    [_, failed] = found.jobs
    assert (failed.job_type, failed.state, failed.outcome_code, failed.error) == (
        "extract.fake_refuse", JobState.FAILED, OutcomeCode.CORRUPT, "the fake extractor refused",
    )


async def test_after_a_reprocess_the_reprocessed_jobs_are_shown(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()
    done = await db.write(lambda tx: ingest.reprocess(tx, [capture["id"]]))
    await hub.settle()

    found = await status(hub, capture["attachments"][0])

    assert [job.params for job in found.jobs] == [{REPROCESS: done.nonce}, {"syntax": "markdown", REPROCESS: done.nonce}]


@pytest.mark.parametrize("role", ["photo", "audio_segment"])
async def test_photos_and_recordings_have_no_file_status(db, device, blob_store, tmp_path, role):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.txt", NOTES), role=role)

    assert await status(hub, capture["attachments"][0]) is None


async def test_the_file_pipeline_reports_its_status_through_the_registry(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)

    assert [(name, reporter.__self__) for name, reporter in hub.registry.statuses] == [("files", hub.files)]
