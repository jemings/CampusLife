"""A Hub that starts with readers it lacked queues them for the files it already holds, and nothing else (#6 PR2
decision, D26): a new extractor or converter, or one that reads a type it did not, is planned for each identified
file it reads that it never read; a reader whose version or limits changed, and a file identified under other
identify rules, wait for a reprocess (D26 decision 5)."""

import asyncio
from dataclasses import replace

import pytest
from extract_harness import TEST_LIMITS, hub_with
from test_capture_commit import blob_store, device

from campuslife_hub.extract import formats
from campuslife_hub.extract.extractors import TEXT
from campuslife_hub.extract.fakes import fake
from campuslife_hub.extract.framework import IDENTIFY, RULES_CHANGED, can_read
from campuslife_hub.ingest import INGEST
from campuslife_hub.jobs import JobState

__all__ = ["blob_store", "device"]

NOTES = "# 1장\n트리는 계층 구조입니다.".encode()
ECHO = fake("echo", {"text/plain": {}, "text/markdown": {}})
CONVERT = fake("pdf", {"text/plain": {}, "text/markdown": {}}, extractor_id="fake_convert", kind="render")


async def read_by(db, blob_store, device, tmp_path, *readers):
    """A Hub with readers that has read one Markdown note, and the Capture holding it."""
    hub = hub_with(db, blob_store, device, tmp_path, *readers, base=())
    await hub.runner.catch_up()
    capture = await hub.capture(("노트.md", NOTES))
    await hub.settle()
    return hub, capture


def kinds(found):
    return [(job.job_type, job.state) for job in found]


async def status(hub, attachment):
    return await hub.db.read(lambda snap: hub.files.status(snap, attachment["id"]))


@pytest.mark.parametrize(("added", "job_type", "artifact_type"), [(ECHO, "extract.fake_echo", "extraction"), (CONVERT, "render.fake_convert", "rendition")])
async def test_a_reader_the_hub_gained_reads_the_files_it_already_holds(db, device, blob_store, tmp_path, added, job_type, artifact_type):
    before, capture = await read_by(db, blob_store, device, tmp_path, TEXT)
    after = hub_with(db, blob_store, device, tmp_path, TEXT, added, base=())

    assert await after.runner.catch_up() == 1
    await after.settle()

    assert kinds(await after.jobs()) == [
        (INGEST, JobState.SUCCEEDED), (IDENTIFY, JobState.SUCCEEDED), ("extract.text", JobState.SUCCEEDED), (job_type, JobState.SUCCEEDED),
    ]
    assert len([artifact for artifact in await after.artifacts(artifact_type) if artifact["pipeline_version"].startswith(added.id)]) == 1
    assert (await status(after, capture["attachments"][0])).facts == ()


async def test_a_reader_that_reads_a_type_it_did_not_reads_the_files_of_that_type(db, device, blob_store, tmp_path):
    plain_only = fake("echo", {"text/plain": {}})
    before, _ = await read_by(db, blob_store, device, tmp_path, TEXT, plain_only)
    after = hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=())

    assert await after.runner.catch_up() == 1
    await after.settle()

    assert [job.state for job in await after.jobs("extract.fake_echo")] == [JobState.SUCCEEDED]


@pytest.mark.parametrize(
    ("changed", "limits"),
    [(replace(TEXT, version=str(int(TEXT.version) + 1)), TEST_LIMITS), (replace(TEXT, limits=replace(TEST_LIMITS, timeout_s=31.0)), None)],
    ids=["version", "limits"],
)
async def test_a_reader_whose_version_or_limits_changed_waits_for_a_reprocess_when_another_is_gained(db, device, blob_store, tmp_path, changed, limits):
    before, capture = await read_by(db, blob_store, device, tmp_path, TEXT)
    after = hub_with(db, blob_store, device, tmp_path, changed, ECHO, base=(), limits=limits, identify_limits=TEST_LIMITS)

    assert await after.runner.catch_up() == 1

    assert len(await after.jobs("extract.fake_echo")) == 1
    [job] = await after.jobs("extract.text")
    assert job.pipeline == before.registry.pipeline("extract.text") != after.registry.pipeline("extract.text")
    assert (await status(after, capture["attachments"][0])).facts == (can_read("extract.text"),)


async def test_a_file_identified_under_other_identify_rules_waits_for_a_reprocess(db, device, blob_store, tmp_path):
    before, capture = await read_by(db, blob_store, device, tmp_path, TEXT)
    after = hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=(), limits=replace(TEST_LIMITS, timeout_s=29.0))

    assert await after.runner.catch_up() == 0

    assert await after.jobs("extract.fake_echo") == []
    assert (await status(after, capture["attachments"][0])).facts == (RULES_CHANGED,)


async def test_a_file_whose_reader_failed_is_not_read_again(db, device, blob_store, tmp_path):
    refusing = fake("refuse", {"text/markdown": {"status": "corrupt"}})
    before, _ = await read_by(db, blob_store, device, tmp_path, TEXT, refusing)
    after = hub_with(db, blob_store, device, tmp_path, TEXT, refusing, ECHO, base=())

    assert await after.runner.catch_up() == 1

    assert len(await after.jobs("extract.fake_refuse")) == 1


async def test_rules_the_hub_started_with_before_are_not_looked_through_again(db, device, blob_store, tmp_path, monkeypatch):
    before, _ = await read_by(db, blob_store, device, tmp_path, TEXT)
    after = hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=())
    assert await after.runner.catch_up() == 1

    def looked(*args):
        raise AssertionError("the Hub looked through its files under readers it started with before")

    monkeypatch.setattr(type(after.files), "plan", looked)
    again = hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=())

    assert await again.runner.catch_up() == 0


async def test_a_hub_that_goes_back_to_readers_it_had_queues_nothing_new(db, device, blob_store, tmp_path):
    before, _ = await read_by(db, blob_store, device, tmp_path, TEXT)
    await hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=()).runner.catch_up()
    queued = len(await before.jobs())

    assert await before.runner.catch_up() == 0
    assert len(await before.jobs()) == queued


async def test_a_file_of_a_role_the_planner_skips_is_not_read(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, TEXT, base=())
    await hub.runner.catch_up()
    await hub.capture(("a.md", NOTES), role="photo")
    await hub.settle()
    after = hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=())

    assert await after.runner.catch_up() == 0
    assert await after.jobs() == (await hub.jobs())


async def test_the_runner_catches_up_when_it_starts(db, device, blob_store, tmp_path):
    before, _ = await read_by(db, blob_store, device, tmp_path, TEXT)
    after = hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=())
    stop = asyncio.Event()

    async def read_then_stop():
        while not [job for job in await after.jobs("extract.fake_echo") if job.state is JobState.SUCCEEDED]:
            await asyncio.sleep(0.05)
        stop.set()

    async with asyncio.timeout(60):
        await asyncio.gather(after.runner.run_forever(stop, idle_s=0.05), read_then_stop())


async def test_a_catch_up_that_fails_leaves_the_jobs_running_and_runs_again_at_the_next_start(db, device, blob_store, tmp_path, monkeypatch):
    """T1: one file the catch-up cannot plan does not stop the Hub's processing, and the readers are not recorded."""
    await read_by(db, blob_store, device, tmp_path, TEXT)
    after = hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=())

    def broken(self, tx, registry):
        raise RuntimeError("a file the catch-up cannot plan")

    monkeypatch.setattr(type(after.files), "_catch_up", broken)
    await after.capture(("b.txt", b"another note"))
    stop = asyncio.Event()

    async def read_then_stop():
        while not [job for job in await after.jobs("extract.fake_echo") if job.state is JobState.SUCCEEDED]:
            await asyncio.sleep(0.05)
        stop.set()

    async with asyncio.timeout(60):
        await asyncio.gather(after.runner.run_forever(stop, idle_s=0.05), read_then_stop())
    monkeypatch.undo()

    assert await hub_with(db, blob_store, device, tmp_path, TEXT, ECHO, base=()).runner.catch_up() == 1


@pytest.mark.parametrize(("kind", "job_type"), [("extract", "extract.fake_echo"), ("render", "render.fake_convert")])
async def test_a_reader_that_reads_another_type_of_bytes_it_read_reads_them_as_that_type(db, device, blob_store, tmp_path, kind, job_type):
    """T2: the same Blob as a .txt and a .md; a reader that read it as plain text and now reads Markdown too reads it
    as Markdown, with Markdown's params."""
    name = "pdf" if kind == "render" else "echo"
    plain = fake(name, {"text/plain": {"as": "plain"}}, extractor_id="fake_convert" if kind == "render" else None, kind=kind)
    both = fake(name, {"text/plain": {"as": "plain"}, "text/markdown": {"as": "markdown"}}, extractor_id=plain.id, kind=kind)
    hub = hub_with(db, blob_store, device, tmp_path, TEXT, plain, base=())
    await hub.runner.catch_up()
    await hub.capture(("a.txt", NOTES), ("a.md", NOTES))
    await hub.settle()
    after = hub_with(db, blob_store, device, tmp_path, TEXT, both, base=())

    assert await after.runner.catch_up() == 1

    assert [job.params for job in await after.jobs(job_type)] == [{"as": "plain"}, {"as": "markdown"}]


async def test_a_type_the_format_table_gained_is_read_by_the_reader_of_it(db, device, blob_store, tmp_path, monkeypatch):
    """The readers stay; the format table now names a type by the file's extension, so refine picks a reader it did
    not before."""
    notes = fake("echo", {"text/x-notes": {}})
    hub, _ = await read_by(db, blob_store, device, tmp_path, TEXT, notes)
    await hub.capture(("a.notes", b"plain words"))
    await hub.settle()
    assert await hub.jobs("extract.fake_echo") == []
    gained = formats.Format("text/x-notes", formats.TEXT, (".notes",))
    monkeypatch.setattr(formats, "FORMATS", (*formats.FORMATS, gained))
    monkeypatch.setattr(formats, "BY_MEDIA_TYPE", {**formats.BY_MEDIA_TYPE, gained.media_type: gained})
    monkeypatch.setattr(formats, "BY_EXTENSION", {**formats.BY_EXTENSION, ".notes": gained})

    assert await hub_with(db, blob_store, device, tmp_path, TEXT, notes, base=()).runner.catch_up() == 1


async def test_a_reader_whose_params_for_a_type_changed_is_not_queued_again(db, device, blob_store, tmp_path):
    """C1: params changed for a type the reader already read is a change of what it reads, which waits for a
    reprocess (D26 decision 5), not a reading the Hub gained."""
    old = fake("echo", {"text/markdown": {"mode": "a"}})
    new = fake("echo", {"text/markdown": {"mode": "b"}})
    before = hub_with(db, blob_store, device, tmp_path, TEXT, old, base=())
    await before.runner.catch_up()
    await before.capture(("a.md", NOTES))
    await before.settle()
    after = hub_with(db, blob_store, device, tmp_path, TEXT, new, base=())

    assert await after.runner.catch_up() == 0


async def test_a_catch_up_that_fails_midway_queues_nothing_and_records_nothing(db, device, blob_store, tmp_path, monkeypatch):
    """C2: the catch-up is one transaction."""
    before = hub_with(db, blob_store, device, tmp_path, TEXT, base=())
    await before.runner.catch_up()
    await before.capture(("a.md", NOTES), ("b.md", b"# 2\nother"))
    await before.settle()
    queued = len(await before.jobs())
    echo = fake("echo", {"text/markdown": {}})
    after = hub_with(db, blob_store, device, tmp_path, TEXT, echo, base=())
    files = type(after.files)
    original, calls = files.plan, []

    def second_fails(self, tx, attachment_id, reprocess=None):
        calls.append(attachment_id)
        if len(calls) == 2:
            raise RuntimeError("midway")
        return original(self, tx, attachment_id, reprocess)

    monkeypatch.setattr(files, "plan", second_fails)
    with pytest.raises(RuntimeError, match="midway"):
        await after.runner.catch_up()

    assert len(await after.jobs()) == queued
    monkeypatch.undo()
    assert await hub_with(db, blob_store, device, tmp_path, TEXT, echo, base=()).runner.catch_up() == 2
