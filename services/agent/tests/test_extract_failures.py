import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from extract_harness import hub_with
from test_capture_commit import blob_store, device

import malicious_files as make
from campuslife_hub.extract import child
from campuslife_hub.extract.fakes import fake
from campuslife_hub.extract.framework import IDENTIFY
from campuslife_hub.extract.limits import Limits
from campuslife_hub.extract.running import Answer, run_child
from campuslife_hub.jobs import JobState, OutcomeCode

__all__ = ["blob_store", "device"]

WINDOWS = sys.platform == "win32"
NOTES = b"lecture notes\n\nsecond paragraph"
FAKES = tuple(
    fake(name, {}) for name in ("echo", "crash", "hang", "hog", "flood", "garbage", "exit_silently", "lone_surrogate", "refuse", "modules", "plant", "zip_walk")
)
DOCUMENT = [{"kind": "document"}]


class OneAttempt:
    attempts = 1


def same_path(seen, expected):
    """Windows may give the same directory by its short name or in another case."""
    return os.path.normcase(os.path.realpath(seen)) == os.path.normcase(os.path.realpath(expected))


def block(text="x", refs=DOCUMENT, **more):
    return {"block_type": "paragraph", "text": text, "refs": refs, **more}


async def ran(hub, name, params=None, data=NOTES, retries=0):
    """The job of fake name on a Blob of data, after it ran."""
    job = await hub.enqueue(f"extract.fake_{name}", await hub.store(data), params)
    await hub.settle(retries=retries)
    return await hub.job(job.id)


@pytest.mark.parametrize(
    ("status", "state", "code"),
    [
        ("corrupt", JobState.FAILED, OutcomeCode.CORRUPT),
        ("password_required", JobState.FAILED, OutcomeCode.PASSWORD_REQUIRED),
        ("unsupported", JobState.FAILED, OutcomeCode.UNSUPPORTED),
        ("unsafe_input", JobState.FAILED, OutcomeCode.UNSAFE_INPUT),
        ("needs_conversion", JobState.BLOCKED, OutcomeCode.NEEDS_CONVERSION),
    ],
)
async def test_what_the_extractor_refuses_ends_the_job_with_its_outcome_and_no_artifact(db, device, blob_store, tmp_path, status, state, code):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)

    job = await ran(hub, "refuse", {"status": status})

    assert (job.state, job.outcome_code, job.attempts, job.error) == (state, code, 1, "the fake extractor refused")
    assert await hub.artifacts() == []


async def test_a_partial_result_succeeds_partial_with_its_artifact(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)

    job = await ran(hub, "echo", {"blocks": [block("read")], "warnings": ["limit_reached"], "partial": True})

    assert (job.state, job.outcome_code) == (JobState.SUCCEEDED, OutcomeCode.PARTIAL)
    [artifact] = await hub.artifacts("extraction")
    body, _ = await hub.body(artifact)
    assert ([each["text"] for each in body["blocks"]], body["warnings"]) == (["read"], [{"code": "limit_reached"}])


@pytest.mark.parametrize(
    ("name", "limits", "error"),
    [
        ("hang", Limits(timeout_s=2.0), "ran past"),
        ("hog", Limits(mem_bytes=256 << 20), "memory"),
        ("flood", Limits(stdout_max_bytes=1 << 20), "printed more"),
    ],
)
async def test_a_child_past_a_limit_fails_with_unsafe_input_and_is_not_tried_again(db, device, blob_store, tmp_path, name, limits, error):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES, limits=limits)

    job = await ran(hub, name, retries=1)

    assert (job.state, job.outcome_code, job.attempts) == (JobState.FAILED, OutcomeCode.UNSAFE_INPUT, 1)
    assert error in job.error


@pytest.mark.parametrize("name", ["crash", "exit_silently"])
async def test_a_child_that_ends_without_a_result_is_tried_once_more_and_then_fails(db, device, blob_store, tmp_path, name):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)

    first = await ran(hub, name)
    assert (first.state, first.attempts) == (JobState.RETRY_WAIT, 1)
    await hub.settle(retries=1)

    job = await hub.job(first.id)
    assert (job.state, job.outcome_code, job.attempts) == (JobState.FAILED, None, 2)
    assert "without a result" in job.error


async def test_a_result_outside_the_protocol_fails_the_job(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)

    job = await ran(hub, "garbage")

    assert (job.state, job.outcome_code, job.attempts) == (JobState.FAILED, None, 1)
    assert "not the protocol's" in job.error


BLOB_REF = [{"kind": "document", "blob": "ab" * 32}]


@pytest.mark.parametrize(
    ("blocks", "error"),
    [
        ([block(refs=BLOB_REF)], "names blob"),
        ([block(refs=[{"kind": "document", "v": 1}])], "names v"),
        ([block(refs=[{"kind": "media_time", "capture": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "start_ms": 0, "end_ms": 1}])], "names capture"),
        ([block(id="ab" * 32)], "names its id"),
        ([block(refs=[])], "breaks artifact/extraction"),
        ([block(refs=[{"kind": "doc_path", "block_index": 0}])], "breaks artifact/extraction"),
        ([block(refs=["document"])], "not an object"),
        ([block(text=7)], "breaks artifact/extraction"),
        ([block() | {"refs": "document"}], "no refs list"),
        (["paragraph"], "not an object"),
        ([block(block_type="Heading")], "breaks artifact/extraction"),
    ],
)
async def test_a_result_that_breaks_the_contract_is_refused_and_nothing_is_recorded(db, device, blob_store, tmp_path, blocks, error):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)

    job = await ran(hub, "echo", {"blocks": blocks})

    assert (job.state, job.outcome_code) == (JobState.FAILED, None)
    assert "breaks the contract" in job.error and error in job.error
    assert await hub.artifacts() == []


async def test_a_result_holding_a_lone_surrogate_is_refused_once_with_its_reason(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)

    job = await ran(hub, "lone_surrogate")

    assert (job.state, job.outcome_code, job.attempts) == (JobState.FAILED, None, 1)
    assert "breaks the contract" in job.error and "lone surrogate" in job.error
    assert await hub.artifacts() == []


@pytest.mark.parametrize(
    ("limits", "blocks"),
    [
        (Limits(max_blocks=2), [block()] * 3),
        (Limits(max_block_text_chars=4), [block("12345")]),
        (Limits(body_max_bytes=600), [block("x" * 100)] * 3),
        (Limits(max_text_chars=5), [block("abc")] * 2),
    ],
    ids=["blocks", "block text", "body bytes", "total text"],
)
async def test_a_result_past_a_size_limit_fails_with_unsafe_input(db, device, blob_store, tmp_path, limits, blocks):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES, limits=limits)

    job = await ran(hub, "echo", {"blocks": blocks})

    assert (job.state, job.outcome_code) == (JobState.FAILED, OutcomeCode.UNSAFE_INPUT)
    assert await hub.artifacts() == []


async def test_a_result_at_the_size_limits_is_recorded(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES, limits=Limits(max_blocks=2, max_block_text_chars=4, max_text_chars=8))

    job = await ran(hub, "echo", {"blocks": [block("1234")] * 2})

    assert job.state is JobState.SUCCEEDED


async def test_a_blob_of_exactly_the_input_limit_is_read(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES, limits=Limits(timeout_s=30.0, input_max_bytes=len(NOTES)))

    job = await ran(hub, "echo", {"blocks": []})

    assert job.state is JobState.SUCCEEDED


async def test_a_blob_past_the_input_limit_is_not_handed_to_a_child(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES, limits=Limits(input_max_bytes=len(NOTES) - 1))

    job = await ran(hub, "crash")

    assert (job.state, job.outcome_code) == (JobState.FAILED, OutcomeCode.UNSAFE_INPUT)


async def test_strings_are_stored_nfc(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)
    decomposed = "가"

    await ran(hub, "echo", {"blocks": [block(decomposed, refs=[{"kind": "doc_path", "heading_path": [decomposed], "block_index": 0}], payload={decomposed: decomposed})]})

    [artifact] = await hub.artifacts("extraction")
    body, _ = await hub.body(artifact)
    assert (body["blocks"][0]["text"], body["blocks"][0]["refs"][0]["heading_path"]) == ("가", ["가"])
    assert body["blocks"][0]["payload"] == {"가": "가"}


async def test_one_failing_extractor_stops_neither_the_hub_nor_the_other_files(db, device, blob_store, tmp_path):
    limits = Limits(timeout_s=3.0, mem_bytes=256 << 20)
    failing = [fake(name, {media: {}}) for name, media in (("crash", "text/csv"), ("hang", "text/tab-separated-values"), ("hog", "application/json"))]
    hub = hub_with(db, blob_store, device, tmp_path, *failing, limits=limits)
    capture = await hub.capture(("a.csv", b"a,b\n"), ("b.tsv", b"a\tb\n"), ("c.json", b"{}"), ("notes.txt", NOTES))

    await hub.settle(retries=1)

    outcomes = {job.job_type: (job.state, job.outcome_code) for job in await hub.jobs() if job.job_type.startswith("extract.")}
    assert outcomes == {
        "extract.fake_crash": (JobState.FAILED, None),
        "extract.fake_hang": (JobState.FAILED, OutcomeCode.UNSAFE_INPUT),
        "extract.fake_hog": (JobState.FAILED, OutcomeCode.UNSAFE_INPUT),
        "extract.text": (JobState.SUCCEEDED, None),
    }
    assert len(await hub.artifacts("extraction")) == 1
    assert len(capture["attachments"]) == 4


async def test_the_child_runs_in_its_work_directory_with_only_the_env_it_is_given_and_none_of_the_hub(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)

    job = await ran(hub, "modules")

    [artifact] = await hub.artifacts("extraction")
    body, _ = await hub.body(artifact)
    loaded, where = body["blocks"][0]["text"].split("\n"), body["blocks"][1]["text"].split("\n")
    hub_modules = {name for name in loaded if name.startswith("campuslife_hub")}
    assert hub_modules <= {
        "campuslife_hub", "campuslife_hub.extract", "campuslife_hub.extract.child", "campuslife_hub.extract.sniff",
        "campuslife_hub.extract.formats", "campuslife_hub.extract.limits", "campuslife_hub.extract.protocol",
        "campuslife_hub.extract.safe_zip", "campuslife_hub.extract.extracted", "campuslife_hub.extract.fakes",
        "campuslife_hub.extract.extractors", "campuslife_hub.extract.text",
    }
    assert not {"sqlite3", "socket", "ssl", "http.client", "urllib.request", "campuslife_contracts"} & set(loaded)
    assert same_path(where[0], tmp_path / "work" / job.id)
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"} if WINDOWS else {"TMPDIR"}
    assert set(where[1:]) - {"__PYVENV_LAUNCHER__", "PYTHONHASHSEED", "LC_CTYPE"} <= allowed
    assert ({"TEMP", "TMP"} if WINDOWS else {"TMPDIR"}) <= set(where[1:])
    assert not (tmp_path / "work" / job.id).exists()


async def test_the_child_starts_in_an_empty_work_directory_whatever_a_former_run_left(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)
    job = await hub.enqueue("extract.fake_modules", await hub.store(NOTES))
    left = tmp_path / "work" / job.id
    left.mkdir(parents=True)
    (left / "left.txt").write_bytes(b"from a run the Hub did not finish")

    await hub.settle()

    [artifact] = await hub.artifacts("extraction")
    body, _ = await hub.body(artifact)
    assert body["blocks"][2]["text"] == "held:"


async def test_the_hub_stages_the_body_outside_the_childs_work_directory(db, device, blob_store, tmp_path):
    hub = hub_with(db, blob_store, device, tmp_path, *FAKES)

    job = await ran(hub, "plant")

    assert job.state is JobState.SUCCEEDED
    [artifact] = await hub.artifacts("extraction")
    body, _ = await hub.body(artifact)
    assert [block["text"] for block in body["blocks"]] == ["planted"]


async def test_the_child_ignores_modules_planted_in_its_work_directory(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "json.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    source = tmp_path / "notes.txt"
    source.write_bytes(NOTES)

    judged = await run_child(OneAttempt(), {"op": "identify", "path": str(source)}, Limits(timeout_s=30.0), work)

    assert isinstance(judged, Answer) and judged.result["media_type"] == "text/plain"


def test_the_child_runs_no_entry_point_outside_the_extractor_package(tmp_path):
    request = {"op": "extract", "path": str(tmp_path / "notes.txt"), "entry": "os:getcwd", "params": {}, "limits": Limits().to_json()}

    with pytest.raises(ValueError, match="an entry point lives in campuslife_hub.extract."):
        child.respond(request)


async def test_the_event_loop_keeps_running_while_the_hub_builds_a_body_whatever_its_block_count(db, device, blob_store, tmp_path):
    """Whether the loop stalls does not depend on the block count, since the body is built in a worker thread: 5,000
    blocks, some 4 s of the Hub's work, show it without spending the 16 s of the default max_blocks."""
    limits = Limits(max_blocks=5_000)
    hub = hub_with(db, blob_store, device, tmp_path, limits=limits)
    gaps = []

    async def tick():
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.05)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticking = asyncio.create_task(tick())
    try:
        await hub.capture(("many.txt", b"a\n\n" * (limits.max_blocks + 1)))
        await hub.settle()
    finally:
        ticking.cancel()

    [job] = await hub.jobs("extract.text")
    assert (job.state, job.outcome_code) == (JobState.SUCCEEDED, OutcomeCode.PARTIAL)
    [artifact] = await hub.artifacts("extraction")
    assert len((await hub.body(artifact))[0]["blocks"]) == limits.max_blocks
    assert max(gaps) < 1.0


@pytest.mark.parametrize(
    ("make_file", "code"),
    [
        (make.traversal, OutcomeCode.UNSAFE_INPUT),
        (make.absolute, OutcomeCode.UNSAFE_INPUT),
        (make.symlink, OutcomeCode.UNSAFE_INPUT),
        (make.bomb, OutcomeCode.UNSAFE_INPUT),
        (make.encrypted_member, OutcomeCode.PASSWORD_REQUIRED),
        (make.encrypted_ooxml, OutcomeCode.PASSWORD_REQUIRED),
    ],
)
async def test_identify_ends_a_dangerous_file_with_its_outcome_and_plans_nothing(db, device, blob_store, tmp_path, make_file, code):
    hub = hub_with(db, blob_store, device, tmp_path, fake("zip_walk", {"application/zip": {}}))
    capture = await hub.capture(("lecture.docx", make_file(tmp_path / "input").read_bytes()))

    await hub.settle()

    [identify] = await hub.jobs(IDENTIFY)
    assert (identify.state, identify.outcome_code) == (JobState.FAILED, code)
    assert [job.job_type for job in await hub.jobs()] == ["capture.ingest", IDENTIFY]
    assert "detected_mime" not in await hub.attachment(capture["attachments"][0]["id"])
    assert await hub.media(capture["attachments"][0]["blob_sha256"]) is None


@pytest.mark.parametrize(("depth", "state", "code"), [(2, JobState.SUCCEEDED, None), (3, JobState.FAILED, OutcomeCode.UNSAFE_INPUT)])
async def test_an_extractor_reads_nested_archives_through_the_gate_up_to_the_depth_of_the_table(db, device, blob_store, tmp_path, depth, state, code):
    hub = hub_with(db, blob_store, device, tmp_path, fake("zip_walk", {"application/zip": {}}), limits=Limits(zip_max_depth=2))
    await hub.capture(("bundle.zip", make.nested(tmp_path / "input", depth).read_bytes()))

    await hub.settle()

    [walked] = await hub.jobs("extract.fake_zip_walk")
    assert (walked.state, walked.outcome_code) == (state, code)


NO_CONSOLE_CHILD = """
import asyncio, json, sys
from pathlib import Path
sys.stdin = sys.stdout = sys.stderr = None
from campuslife_hub.extract.limits import Limits
from campuslife_hub.extract.running import Answer, run_child

class Job:
    attempts = 1

judged = asyncio.run(run_child(Job(), {{"op": "identify", "path": {path!r}}}, Limits(), Path({work!r})))
Path({out!r}).write_text(json.dumps(judged.result if isinstance(judged, Answer) else repr(judged)), encoding="utf-8")
"""


def test_a_hub_without_console_streams_still_reads_its_childs_result(tmp_path):
    """As the logon task runs the Hub: through pythonw on Windows, with every standard stream None."""
    windowless = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(windowless) if WINDOWS and windowless.exists() else sys.executable
    source, out = tmp_path / "notes.txt", tmp_path / "out.json"
    source.write_bytes(NOTES)
    script = NO_CONSOLE_CHILD.format(path=str(source), work=str(tmp_path / "work"), out=str(out))

    finished = subprocess.run([interpreter, "-c", script], capture_output=True, timeout=120)

    assert finished.returncode == 0, finished.stderr
    assert out.read_text(encoding="utf-8") == '{"status": "ok", "media_type": "text/plain"}'
