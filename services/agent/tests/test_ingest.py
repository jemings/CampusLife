from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from conftest import contract_fixture

from campuslife_hub import capture_commit, jobs
from campuslife_hub.ingest import INGEST, INGEST_PIPELINE, ingest_spec
from campuslife_hub.job_runner import JobContext, JobRegistry, JobRunner
from campuslife_hub.jobs import JobSpec, JobState, Pipeline, Succeeded
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


async def stored_rows(db):
    """The rows the feed carries, without the revs a job sends its subject out again at."""
    return sorted((row["entity"], row["id"], row["deleted"], row.get("data")) for row in await feed(db))


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


async def ran_ingest(db, blob_store, registry):
    """Run the one job due, the ingest, and not the jobs it queues."""
    await JobRunner(JobContext(db, blob_store, registry)).run_one()


def planning(job_type, seen=None):
    async def plan(view, context):
        if seen is not None:
            seen.append(view)
        return [JobSpec(job_type, f"attachment:{attachment['id']}", (attachment["blob_sha256"],)) for attachment in view.attachments]

    return plan


def failing_planner(error):
    async def plan(view, context):
        raise error

    return plan


async def succeeding(job, context):
    return Succeeded()


def handled(*job_types):
    registry = JobRegistry()
    for job_type in job_types:
        registry.handle(job_type, Pipeline(f"{job_type}-1"), succeeding)
    return registry


async def committed(db, device, blob_store, tmp_path, files=(b"slides",)):
    capture = await text_capture(blob_store, tmp_path, files[0])
    for content in files[1:]:
        capture["attachments"].append({"id": new_id(), "blob_sha256": await store_bytes(blob_store, tmp_path, content), "role": "file", "cloud_policy": "local_only"})
    await apply(db, device, commit(capture))
    return capture


async def queued(db, job_type):
    return [each for each in await all_jobs(db) if each.job_type == job_type]


async def test_planners_see_the_capture_and_its_attachments_and_their_jobs_are_queued(db, device, blob_store, tmp_path):
    registry, seen = handled("extract.text", "transcribe"), []
    registry.plan("extract", planning("extract.text", seen))
    registry.plan("speech", planning("transcribe"))
    capture = await committed(db, device, blob_store, tmp_path, files=(b"slides", b"audio"))

    await ran_ingest(db, blob_store, registry)

    [view] = seen
    assert view.capture["id"] == capture["id"]
    assert [attachment["id"] for attachment in view.attachments] == [attachment["id"] for attachment in capture["attachments"]]
    for job_type in ("extract.text", "transcribe"):
        planned = await queued(db, job_type)
        assert [(each.subject_ref, each.inputs, each.state, each.pipeline) for each in planned] == [
            (f"attachment:{attachment['id']}", (attachment["blob_sha256"],), JobState.PENDING, Pipeline(f"{job_type}-1"))
            for attachment in capture["attachments"]
        ]
    [ingest] = await ingest_of(db, capture)
    assert ingest.state is JobState.SUCCEEDED


async def test_a_failing_planner_keeps_out_only_its_own_jobs_and_the_ingest_runs_again(db, device, blob_store, tmp_path):
    registry = handled("extract.text", "transcribe")
    registry.plan("extract", planning("extract.text"))
    broken = {"error": RuntimeError("no model")}

    async def speech(view, context):
        if broken["error"] is not None:
            raise broken["error"]
        return await planning("transcribe")(view, context)

    registry.plan("speech", speech)
    capture = await committed(db, device, blob_store, tmp_path)
    after_commit = await stored_rows(db)
    clock = {"now": datetime.now(UTC)}
    runner = JobRunner(JobContext(db, blob_store, registry), clock=lambda: clock["now"])

    await runner.run_one()

    [ingest] = await ingest_of(db, capture)
    assert (ingest.state, ingest.error) == (JobState.RETRY_WAIT, "PlanningFailed: speech: RuntimeError: no model")
    assert len(await queued(db, "extract.text")) == 1
    assert await queued(db, "transcribe") == []
    assert await stored_rows(db) == after_commit

    broken["error"] = None
    clock["now"] = datetime.fromisoformat(ingest.next_attempt_at)
    await runner.run_due()

    assert (await ingest_of(db, capture))[0].state is JobState.SUCCEEDED
    assert [len(await queued(db, job_type)) for job_type in ("extract.text", "transcribe")] == [1, 1]


async def test_a_planner_that_proposes_a_job_type_without_a_handler_counts_as_failing(db, device, blob_store, tmp_path):
    registry = handled("extract.text")
    registry.plan("extract", planning("extract.text"))
    registry.plan("summary", planning("summarize"))
    capture = await committed(db, device, blob_store, tmp_path)

    await ran_ingest(db, blob_store, registry)

    [ingest] = await ingest_of(db, capture)
    assert (ingest.state, ingest.error) == (JobState.RETRY_WAIT, "PlanningFailed: summary: UnknownJobType: no handler for job type summarize")
    assert len(await queued(db, "extract.text")) == 1


@pytest.mark.parametrize(
    "unstorable",
    [
        {"params": {"recorded_on": date(2026, 3, 2)}},
        {"subject_ref": None},
        {"inputs": ("slides.pdf",)},
        {"subject_ref": "capture:x\ud800"},
        {"params": {"n": 10**5000}},
    ],
    ids=["param not JSON", "no subject", "input not a sha256", "subject not UTF-8", "param past what JSON text holds"],
)
async def test_a_planner_that_proposes_a_job_that_cannot_be_stored_keeps_out_only_its_own_jobs(db, device, blob_store, tmp_path, unstorable):
    registry = handled("extract.text", "transcribe")
    registry.plan("extract", planning("extract.text"))

    async def speech(view, context):
        [planned] = await planning("transcribe")(view, context)
        return [replace(planned, **unstorable)]

    registry.plan("speech", speech)
    capture = await committed(db, device, blob_store, tmp_path)

    await ran_ingest(db, blob_store, registry)

    [ingest] = await ingest_of(db, capture)
    assert (ingest.state, ingest.error.split(":")[:2]) == (JobState.RETRY_WAIT, ["PlanningFailed", " speech"])
    assert len(await queued(db, "extract.text")) == 1
    assert await queued(db, "transcribe") == []


async def test_without_planners_the_ingest_succeeds_and_queues_nothing(db, device, blob_store, tmp_path):
    capture = await committed(db, device, blob_store, tmp_path)

    await ran_ingest(db, blob_store, JobRegistry())

    assert [(each.job_type, each.state) for each in await all_jobs(db)] == [(INGEST, JobState.SUCCEEDED)]
    assert (await ingest_of(db, capture))[0].attempts == 1


async def test_processing_changes_no_capture_or_attachment_row(db, device, blob_store, tmp_path):
    registry = handled("extract.text")
    registry.plan("extract", planning("extract.text"))
    await committed(db, device, blob_store, tmp_path)
    after_commit = await stored_rows(db)

    await ran_ingest(db, blob_store, registry)

    assert await stored_rows(db) == after_commit


async def test_ingesting_a_capture_the_hub_lacks_fails(db, blob_store):
    registry = JobRegistry()
    missing = {"id": new_id(), "attachments": []}
    [ingest] = await db.write(lambda tx: [registry.enqueue(tx, ingest_spec(missing))])

    await ran_ingest(db, blob_store, registry)

    failed = await db.read(lambda snap: jobs.get(snap, ingest.id))
    assert (failed.state, failed.error) == (JobState.FAILED, f"capture {missing['id']} is not in the Hub")


def test_a_planner_name_is_registered_once():
    registry = JobRegistry()
    registry.plan("extract", planning("extract.text"))

    with pytest.raises(ValueError):
        registry.plan("extract", planning("extract.text"))
