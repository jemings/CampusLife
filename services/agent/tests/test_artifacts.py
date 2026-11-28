import re
from datetime import UTC, datetime

import pytest
from campuslife_contracts import validate
from test_blobs import blob_store, source

from campuslife_hub import artifacts, changes, jobs
from campuslife_hub.jobs import JobSpec, Pipeline

__all__ = ["blob_store"]

NOW = datetime(2026, 3, 2, 1, 10, tzinfo=UTC)
PDF = "a" * 64
SLIDES = "b" * 64
SUBJECT = "attachment:0f8fad5b-d9cb-469f-a165-70867728950e"
SUMMARIZE = Pipeline("summary-1", "local", "small-model", "3")


async def stored(blob_store, tmp_path, content):
    return (await blob_store.put_file(source(tmp_path, f"{content.hex()}.out", content))).sha256


async def queued(db, inputs=(PDF, SLIDES, PDF), pipeline=SUMMARIZE, job_type="summarize"):
    return await db.write(lambda tx: jobs.enqueue(tx, JobSpec(job_type, SUBJECT, inputs, {}), pipeline, now=NOW))


async def record(db, job, artifact_type, content_blob):
    return await db.write(lambda tx: artifacts.record(tx, job, artifact_type, content_blob, now=NOW))


async def artifact_rows(db):
    body = await db.read(lambda snap: changes.page(snap, 0, 500))
    assert validate("sync/changes_page", body) == []
    return [(row["id"], row["rev"], row["data"]) for row in body["changes"] if row["entity"] == "artifact"]


async def test_an_artifact_records_the_job_that_made_it_and_goes_out_in_the_feed(db, blob_store, tmp_path):
    job = await queued(db)
    content = await stored(blob_store, tmp_path, b"summary")

    artifact = await record(db, job, "summary", content)

    assert validate("artifact/artifact", artifact) == []
    assert re.fullmatch("[0-9a-f-]{36}", artifact["id"])
    assert {name: value for name, value in artifact.items() if name != "id"} == {
        "artifact_type": "summary", "subject_ref": SUBJECT, "job_key": job.job_key,
        "provider": "local", "model": "small-model", "prompt_version": "3", "pipeline_version": "summary-1",
        "input_hashes": [PDF, SLIDES], "content_blob": content, "created_at": "2026-03-02T01:10:00.000Z",
    }
    assert [(row_id, data) for row_id, _, data in await artifact_rows(db)] == [(artifact["id"], artifact)]


async def test_a_job_that_used_no_model_records_no_model_columns(db, blob_store, tmp_path):
    job = await queued(db, inputs=(PDF,), pipeline=Pipeline("extract-1"), job_type="extract.text")

    artifact = await record(db, job, "text", await stored(blob_store, tmp_path, b"text"))

    assert {"provider", "model", "prompt_version"}.isdisjoint(artifact)
    assert artifact["pipeline_version"] == "extract-1"


async def test_a_job_that_reads_no_blob_records_no_input_hashes(db, blob_store, tmp_path):
    job = await queued(db, inputs=(), pipeline=Pipeline("outline-1"), job_type="outline")

    artifact = await record(db, job, "outline", await stored(blob_store, tmp_path, b"outline"))

    assert artifact["input_hashes"] == []
    assert [data for _, _, data in await artifact_rows(db)] == [artifact]


async def test_the_same_job_recording_its_type_again_gets_the_first_artifact_back(db, blob_store, tmp_path):
    job = await queued(db)
    first = await record(db, job, "summary", await stored(blob_store, tmp_path, b"first run"))
    feed = await artifact_rows(db)

    again = await record(db, job, "summary", await stored(blob_store, tmp_path, b"second run"))

    assert again == first
    assert await artifact_rows(db) == feed
    assert await db.read(lambda snap: artifacts.made_by(snap, job.job_key, "summary")) == first


async def test_another_type_from_the_same_job_or_the_same_type_from_another_job_is_another_artifact(db, blob_store, tmp_path):
    job, rerun = await queued(db), await queued(db, pipeline=Pipeline("summary-2", "local", "small-model", "3"))
    content = await stored(blob_store, tmp_path, b"summary")

    made = [await record(db, job, "summary", content), await record(db, job, "outline", content), await record(db, rerun, "summary", content)]

    assert len({artifact["id"] for artifact in made}) == 3
    assert [artifact["job_key"] for artifact in made] == [job.job_key, job.job_key, rerun.job_key]
    assert await db.read(lambda snap: artifacts.made_by(snap, job.job_key, "slides")) is None


@pytest.mark.parametrize(
    ("artifact_type", "content", "inputs"),
    [("Summary", b"x", (PDF,)), ("summary", None, (PDF,)), ("web_snapshot", b"<html>", ())],
    ids=["type outside the contract", "content not stored", "web snapshot without what was fetched"],
)
async def test_an_artifact_the_contract_or_the_blob_store_cannot_hold_is_refused_and_nothing_is_written(
    db, blob_store, tmp_path, artifact_type, content, inputs
):
    job = await queued(db, inputs=inputs)
    content_blob = "c" * 64 if content is None else await stored(blob_store, tmp_path, content)

    with pytest.raises(artifacts.InvalidArtifact):
        await record(db, job, artifact_type, content_blob)

    assert await artifact_rows(db) == []
