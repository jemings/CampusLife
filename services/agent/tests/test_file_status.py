"""What the file pipeline tells the local UI about each Attachment (S13)."""

import pytest
from extract_harness import TEST_LIMITS, hub_with
from test_capture_commit import blob_store, device

from campuslife_hub import ingest
from campuslife_hub.extract.fakes import fake
from campuslife_hub.extract.framework import IDENTIFY, RULES_CHANGED, can_read
from campuslife_hub.extract.limits import Limits
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


async def test_after_the_rules_change_the_last_reading_is_shown_with_the_change_and_nothing_is_queued(db, device, blob_store, tmp_path):
    refusing = fake("refuse", {"text/plain": {"status": "corrupt"}})
    hub = hub_with(db, blob_store, device, tmp_path, refusing)
    capture = await hub.capture(("a.md", NOTES), ("b.txt", NOTES))
    await hub.settle()
    queued = len(await hub.jobs())
    changed = hub_with(db, blob_store, device, tmp_path, refusing, limits=Limits(**{**TEST_LIMITS.to_json(), "rendition_max_bytes": 1 << 20}))

    markdown, plain = [await status(changed, attachment) for attachment in capture["attachments"]]
    await changed.settle()

    assert (markdown.detected_mime, markdown.facts) == ("text/markdown", (RULES_CHANGED,))
    assert kinds(markdown) == [(IDENTIFY, {}, JobState.SUCCEEDED, None), ("extract.text", {"syntax": "markdown"}, JobState.SUCCEEDED, None)]
    assert plain.detected_mime == "text/plain"
    assert [(job.job_type, job.state, job.outcome_code) for job in plain.jobs][-1] == ("extract.fake_refuse", JobState.FAILED, OutcomeCode.CORRUPT)
    assert RULES_CHANGED in plain.facts
    assert len(await changed.jobs()) == queued


async def test_a_file_read_under_the_current_rules_has_no_rules_fact(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.md", NOTES))
    await hub.settle()

    assert RULES_CHANGED not in (await status(hub, capture["attachments"][0])).facts


async def test_a_file_an_extractor_added_since_could_read_says_so_and_nothing_is_queued(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("a.csv", b"a,b\n1,2\n"))
    await hub.settle()
    queued = len(await hub.jobs())
    upgraded = hub_with(db, blob_store, device, tmp_path, fake("echo", {"text/csv": {}}))

    found = await status(upgraded, capture["attachments"][0])
    await upgraded.settle()

    assert upgraded.files.identify_pipeline == hub.files.identify_pipeline
    assert found.facts == (can_read("extract.fake_echo"),)
    assert kinds(found) == [(IDENTIFY, {}, JobState.SUCCEEDED, None)]
    assert len(await upgraded.jobs()) == queued


async def test_a_file_read_by_an_older_extractor_version_says_the_new_one_could_read_it(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, fake("echo", {"text/plain": {}}), base=())
    capture = await hub.capture(("a.txt", b"plain"))
    await hub.settle()
    upgraded = hub_with(db, blob_store, device, tmp_path, fake("echo", {"text/plain": {}}, version="2"), base=())

    found = await status(upgraded, capture["attachments"][0])

    assert found.facts == (can_read("extract.fake_echo"),)
    assert [(job.job_type, job.state) for job in found.jobs] == [(IDENTIFY, JobState.SUCCEEDED), ("extract.fake_echo", JobState.SUCCEEDED)]


async def test_a_reader_whose_job_under_the_current_rules_failed_has_no_reader_fact(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, fake("refuse", {"text/plain": {"status": "corrupt"}}), base=())
    capture = await hub.capture(("a.txt", b"plain"))
    await hub.settle()

    found = await status(hub, capture["attachments"][0])

    assert found.facts == ()
    assert found.jobs[-1].state is JobState.FAILED


async def test_after_a_reprocess_under_the_new_version_the_reader_fact_goes(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, fake("echo", {"text/plain": {}}), base=())
    capture = await hub.capture(("a.txt", b"plain"))
    await hub.settle()
    upgraded = hub_with(db, blob_store, device, tmp_path, fake("echo", {"text/plain": {}}, version="2"), base=())

    await db.write(lambda tx: ingest.reprocess(tx, [capture["id"]]))
    await upgraded.settle()

    assert (await status(upgraded, capture["attachments"][0])).facts == ()
