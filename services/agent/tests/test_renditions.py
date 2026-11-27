"""The rendition seam (S12, D12 and the PR1 decision): a converter, kind render, writes a PDF the Hub keeps as it is,
as the content of a rendition Artifact on the original Blob, with no body and no Relation."""

import hashlib
import sys
from datetime import timedelta

import pytest
from extract_harness import TEST_LIMITS, hub_with
from test_capture_commit import blob_store, device

from campuslife_hub import ingest, purge
from campuslife_hub.extract.extractors import Extractor
from campuslife_hub.extract.fakes import fake
from campuslife_hub.jobs import JobState, OutcomeCode, Pipeline
from campuslife_hub.purge import PurgeHooks

__all__ = ["blob_store", "device"]

SLIDES = "1. 트리\n2. 그래프\n".encode()
PLAIN = {"text/plain": {}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def converter(name="pdf", *, version="1", accepts=PLAIN):
    return fake(name, accepts, version=version, extractor_id="fake_convert", kind="render")


def written(data, mark=""):
    return b"%PDF-1.7\n%" + mark.encode() + b"\n" + data + b"\n%%EOF\n"


async def relations(db):
    return await db.read(lambda snap: snap.execute("SELECT count(*) FROM relations").fetchone()[0])


async def test_a_converter_makes_a_rendition_artifact_whose_content_is_the_pdf(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, converter(), base=())
    await hub.capture(("slides.txt", SLIDES))

    await hub.settle()

    [job] = await hub.jobs("render.fake_convert")
    assert (job.state, job.subject_ref, job.pipeline) == (JobState.SUCCEEDED, f"blob:{sha(SLIDES)}", Pipeline(f"fake_convert-1+L{TEST_LIMITS.hash8()}"))
    [rendition] = await hub.artifacts("rendition")
    assert (rendition["subject_ref"], rendition["input_hashes"], rendition["pipeline_version"]) == (f"blob:{sha(SLIDES)}", [sha(SLIDES)], job.pipeline.version)
    assert rendition["content_blob"] == sha(written(SLIDES))
    assert blob_store.path_of(rendition["content_blob"]).read_bytes() == written(SLIDES)
    assert await relations(db) == 0
    assert not (tmp_path / "work" / job.id).exists()


async def test_a_new_converter_version_leaves_the_old_rendition_and_only_the_purge_takes_them(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, converter(accepts={"text/plain": {"mark": "v1"}}), base=())
    capture = await hub.capture(("slides.txt", SLIDES))
    await hub.settle()
    upgraded = hub_with(db, blob_store, device, tmp_path, converter(version="2", accepts={"text/plain": {"mark": "v2"}}), base=())

    await upgraded.db.write(lambda tx: ingest.reprocess(tx, [capture["id"]]))
    await upgraded.settle()
    await blob_store.collect_garbage(upgraded.clock() + timedelta(days=30))

    old, new = await hub.artifacts("rendition")
    assert [old["pipeline_version"], new["pipeline_version"]] == [f"fake_convert-{version}+L{TEST_LIMITS.hash8()}" for version in ("1", "2")]
    assert [old["content_blob"], new["content_blob"]] == [sha(written(SLIDES, "v1")), sha(written(SLIDES, "v2"))]
    assert all(blob_store.path_of(artifact["content_blob"]).is_file() for artifact in (old, new))

    purged = await purge.purge_blob(db, blob_store, sha(SLIDES), hooks=PurgeHooks())

    assert set(purged.target.artifacts) >= {old["id"], new["id"]}
    assert set(purged.target.blobs) >= {old["content_blob"], new["content_blob"]}
    assert await hub.artifacts("rendition") == []
    assert not blob_store.path_of(old["content_blob"]).exists()


async def test_a_converter_and_an_extractor_of_the_same_type_both_run(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, converter())
    await hub.capture(("slides.txt", SLIDES))

    await hub.settle()

    assert sorted(artifact["artifact_type"] for artifact in await hub.artifacts()) == ["extraction", "identify", "rendition"]


@pytest.mark.parametrize(("name", "said"), [("not_pdf", "is not a PDF"), ("no_pdf", "wrote no rendition.pdf")])
async def test_a_converter_that_writes_no_pdf_fails_its_job_and_records_nothing(db, device, blob_store, tmp_path, name, said):
    hub = hub_with(db, blob_store, device, tmp_path, converter(name), base=())
    await hub.capture(("slides.txt", SLIDES))

    await hub.settle()

    [job] = await hub.jobs("render.fake_convert")
    assert (job.state, job.outcome_code) == (JobState.FAILED, None)
    assert said in job.error
    assert await hub.artifacts("rendition") == []


@pytest.mark.skipif(sys.platform == "win32", reason="a symbolic link needs a privilege on Windows")
async def test_a_converter_that_leaves_a_link_for_the_pdf_fails_its_job(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, converter("pdf_link"), base=())
    await hub.capture(("slides.txt", SLIDES))

    await hub.settle()

    [job] = await hub.jobs("render.fake_convert")
    assert job.state is JobState.FAILED
    assert "wrote no rendition.pdf" in job.error
    assert await hub.artifacts("rendition") == []


async def test_a_rendition_larger_than_the_limit_fails_its_job_as_unsafe(db, device, blob_store, tmp_path):
    limits = TEST_LIMITS.__class__(**{**TEST_LIMITS.to_json(), "rendition_max_bytes": len(written(SLIDES)) - 1})
    hub = hub_with(db, blob_store, device, tmp_path, converter(), base=(), limits=limits)
    await hub.capture(("slides.txt", SLIDES))

    await hub.settle()

    [job] = await hub.jobs("render.fake_convert")
    assert (job.state, job.outcome_code) == (JobState.FAILED, OutcomeCode.UNSAFE_INPUT)
    assert await hub.artifacts("rendition") == []


async def test_a_rendition_as_large_as_the_limit_is_kept(db, device, blob_store, tmp_path):
    limits = TEST_LIMITS.__class__(**{**TEST_LIMITS.to_json(), "rendition_max_bytes": len(written(SLIDES))})
    hub = hub_with(db, blob_store, device, tmp_path, converter(), base=(), limits=limits)
    await hub.capture(("slides.txt", SLIDES))

    await hub.settle()

    assert len(await hub.artifacts("rendition")) == 1


async def test_a_converter_that_needs_a_conversion_blocks_its_job(db, device, blob_store, tmp_path):
    refusing = converter("refuse_render", accepts={"text/plain": {"status": "needs_conversion"}})
    hub = hub_with(db, blob_store, device, tmp_path, refusing, base=())
    await hub.capture(("slides.txt", SLIDES))

    await hub.settle()

    [job] = await hub.jobs("render.fake_convert")
    assert (job.state, job.outcome_code) == (JobState.BLOCKED, OutcomeCode.NEEDS_CONVERSION)


def test_a_descriptor_of_another_kind_is_refused():
    with pytest.raises(ValueError, match="kind"):
        Extractor("convert", "1", "campuslife_hub.extract.fakes:pdf", PLAIN, "transcribe")


def test_a_converter_runs_as_a_render_job():
    assert converter().job_type == "render.fake_convert"
