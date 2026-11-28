"""Each reader runs under its own limits table, apart from identify's (the extractor issues' common decision): the
Hub checks what a reader returns, and hands its child, that reader's bounds and no other."""

from dataclasses import replace

from extract_harness import TEST_LIMITS, hub_with
from test_capture_commit import blob_store, device

from campuslife_hub.extract.fakes import fake
from campuslife_hub.extract.framework import IDENTIFY
from campuslife_hub.jobs import JobState, OutcomeCode

__all__ = ["blob_store", "device"]

NOTES = "1. 트리\n2. 그래프\n".encode()
PLAIN = {"text/plain": {}}


def reading_under(db, blob_store, device, tmp_path, reader, **bounds):
    """A Hub whose one reader has TEST_LIMITS with bounds, while identify keeps TEST_LIMITS."""
    return hub_with(db, blob_store, device, tmp_path, replace(reader, limits=replace(TEST_LIMITS, **bounds)), base=(), limits=None, identify_limits=TEST_LIMITS)


async def outcomes(hub, job_type):
    await hub.capture(("notes.txt", NOTES))
    await hub.settle()
    [identified] = await hub.jobs(IDENTIFY)
    [read] = await hub.jobs(job_type)
    return identified.state, (read.state, read.outcome_code)


async def test_an_extractor_reads_no_blob_larger_than_its_own_input_bound(db, device, blob_store, tmp_path):
    hub = reading_under(db, blob_store, device, tmp_path, fake("echo", PLAIN), input_max_bytes=len(NOTES) - 1)

    assert await outcomes(hub, "extract.fake_echo") == (JobState.SUCCEEDED, (JobState.FAILED, OutcomeCode.UNSAFE_INPUT))


async def test_an_extractors_body_is_checked_against_its_own_block_bound(db, device, blob_store, tmp_path):
    block = {"block_type": "paragraph", "text": "네 글자다", "refs": [{"kind": "document"}]}
    hub = reading_under(db, blob_store, device, tmp_path, fake("echo", {"text/plain": {"blocks": [block]}}), max_block_text_chars=4)

    assert await outcomes(hub, "extract.fake_echo") == (JobState.SUCCEEDED, (JobState.FAILED, OutcomeCode.UNSAFE_INPUT))
    assert await hub.artifacts("extraction") == []


async def test_a_converters_pdf_is_checked_against_its_own_rendition_bound(db, device, blob_store, tmp_path):
    converter = fake("pdf", PLAIN, extractor_id="fake_convert", kind="render")
    hub = reading_under(db, blob_store, device, tmp_path, converter, rendition_max_bytes=16)

    assert await outcomes(hub, "render.fake_convert") == (JobState.SUCCEEDED, (JobState.FAILED, OutcomeCode.UNSAFE_INPUT))


async def test_a_converters_child_is_handed_its_own_table(db, device, blob_store, tmp_path):
    converter = fake("pdf_of_limits", PLAIN, extractor_id="fake_convert", kind="render")
    hub = reading_under(db, blob_store, device, tmp_path, converter, zip_max_depth=5)

    assert await outcomes(hub, "render.fake_convert") == (JobState.SUCCEEDED, (JobState.SUCCEEDED, None))
    [rendition] = await hub.artifacts("rendition")
    _, handle = await hub.blobs.open_blob(rendition["content_blob"])
    with handle:
        assert repr(replace(TEST_LIMITS, zip_max_depth=5)).encode() in handle.read()
