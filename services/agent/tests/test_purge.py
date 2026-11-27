import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass

import pytest
from test_capture_commit import apply, blob_store, commit, device, feed, new_id, store_bytes

from campuslife_hub import artifacts, jobs, purge
from campuslife_hub.artifacts import InvalidArtifact
from campuslife_hub.blobs import has_blob
from campuslife_hub.core import open_core
from campuslife_hub.ingest import INGEST
from campuslife_hub.job_runner import JobContext, JobRegistry, JobRunner
from campuslife_hub.jobs import JobSpec, JobState, Pipeline
from campuslife_hub.local_import import import_files
from campuslife_hub.purge import PurgeHooks, PurgeRefused

__all__ = ["blob_store", "device"]

COURSE = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
CRASH = 71


@dataclass
class Lecture:
    """A Capture of a PDF and a recording, what jobs made from each, and what the user did with it."""

    pdf: str
    audio: str
    pdf_attachment: str
    audio_attachment: str
    capture: str
    transcript: dict
    summary: dict
    audio_notes: dict
    suggestion: dict
    kept_suggestion: dict
    item: str
    note: str
    kept_note: str


def page_at(pdf, page=2):
    return {"v": 1, "kind": "pdf_page", "blob": pdf, "page": page}


async def made(db, blob_store, tmp_path, job_type, subject, inputs, content):
    job = await db.write(lambda tx: jobs.enqueue(tx, JobSpec(job_type, subject, tuple(inputs), {}), Pipeline(f"{job_type}-1")))
    content_blob = await store_bytes(blob_store, tmp_path, content)
    return await db.write(lambda tx: artifacts.record(tx, job, job_type.replace(".", "_"), content_blob))


def note(forked_from, title="정리"):
    note_id = new_id()
    data = {"kind": "note", "title": title, "body": "스택과 큐\n", "fields": {"forked_from_artifact": forked_from}}
    return note_id, {"mutation_id": new_id(), "op": "create", "entity": "item", "id": note_id, "data": data}


async def lecture(db, device, blob_store, tmp_path) -> Lecture:
    course = {"kind": "course", "title": "자료구조", "fields": {}}
    await apply(db, device, {"mutation_id": new_id(), "op": "create", "entity": "item", "id": COURSE, "data": course})
    pdf = await store_bytes(blob_store, tmp_path, b"%PDF synthetic slides")
    audio = await store_bytes(blob_store, tmp_path, b"synthetic recording")
    pdf_attachment, audio_attachment = new_id(), new_id()
    capture = {
        "id": new_id(),
        "created_at": "2026-03-02T00:30:00.000Z",
        "origin": {"type": "import"},
        "links": [{"type": "about", "to": f"item:{COURSE}"}],
        "attachments": [
            {"id": pdf_attachment, "blob_sha256": pdf, "role": "file", "original_filename": "3주차.pdf", "cloud_policy": "local_only"},
            {"id": audio_attachment, "blob_sha256": audio, "role": "audio_segment", "seq": 0, "cloud_policy": "local_only"},
        ],
    }
    assert (await apply(db, device, commit(capture)))[0]["result"] == "applied"
    transcript = await made(db, blob_store, tmp_path, "ocr", f"attachment:{pdf_attachment}", [pdf], b"slide text")
    summary = await made(db, blob_store, tmp_path, "summarize", f"artifact:{transcript['id']}", [transcript["content_blob"]], b"summary")
    audio_notes = await made(db, blob_store, tmp_path, "transcribe", f"attachment:{audio_attachment}", [audio], b"spoken words")
    assignment = {"kind": "task", "title": "과제 2", "fields": {"progress": "inbox"}}
    suggestion = await db.write(lambda tx: suggestions_propose(tx, summary["id"], assignment, [page_at(pdf)], "a"))
    kept_suggestion = await db.write(lambda tx: suggestions_propose(tx, audio_notes["id"], {**assignment, "title": "복습"}, [], "b"))
    assert (await apply(db, device, {"mutation_id": new_id(), "op": "suggestion.accept", "suggestion_id": suggestion["id"]}))[0]["result"] == "applied"
    [item] = [row["id"] for row in await feed(db) if row["entity"] == "item" and row["data"]["title"] == "과제 2"]
    note_id, create_note = note(summary["id"])
    kept_note_id, create_kept_note = note(audio_notes["id"], "녹음 정리")
    about_summary = {"mutation_id": new_id(), "op": "create", "entity": "relation", "id": f"item:{note_id}|about|artifact:{summary['id']}",
                     "data": {"from": f"item:{note_id}", "type": "about", "to": f"artifact:{summary['id']}"}}
    about_suggestion = {"mutation_id": new_id(), "op": "create", "entity": "relation", "id": f"item:{COURSE}|about|suggestion:{suggestion['id']}",
                        "data": {"from": f"item:{COURSE}", "type": "about", "to": f"suggestion:{suggestion['id']}"}}
    results = await apply(db, device, create_note, create_kept_note, about_summary, about_suggestion)
    assert [result["result"] for result in results] == ["applied"] * 4
    return Lecture(pdf, audio, pdf_attachment, audio_attachment, capture["id"], transcript, summary, audio_notes, suggestion,
                   kept_suggestion, item, note_id, kept_note_id)


def suggestions_propose(tx, artifact_id, proposed, evidence, fingerprint):
    from campuslife_hub import suggestions

    return suggestions.propose(tx, artifact_id, "create_item", proposed, evidence, fingerprint * 64)


def live(changes, entity):
    return {row["id"]: row["data"] for row in changes if row["entity"] == entity and not row["deleted"]}


def tombstones(changes):
    return {(row["entity"], row["id"]) for row in changes if row["deleted"]}


def mentions(row, *names):
    text = json.dumps(row)
    return any(name in text for name in names)


async def held(db, sha):
    return await db.read(lambda snap: has_blob(snap, sha))


async def test_purging_a_blob_removes_its_attachments_what_was_made_from_it_and_every_relation_at_them(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    doomed_blobs = (lecture_.pdf, lecture_.transcript["content_blob"], lecture_.summary["content_blob"])

    purged = await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())

    changes = await feed(db)
    gone = (lecture_.pdf_attachment, lecture_.transcript["id"], lecture_.summary["id"], lecture_.suggestion["id"], *doomed_blobs)
    assert [row for entity in ("attachment", "artifact", "suggestion", "relation") for row in live(changes, entity).values() if mentions(row, *gone)] == []
    assert {("attachment", lecture_.pdf_attachment), ("artifact", lecture_.transcript["id"]), ("artifact", lecture_.summary["id"]),
            ("suggestion", lecture_.suggestion["id"])} <= tombstones(changes)
    assert set(live(changes, "attachment")) == {lecture_.audio_attachment}
    assert set(live(changes, "artifact")) == {lecture_.audio_notes["id"]}
    assert set(live(changes, "suggestion")) == {lecture_.kept_suggestion["id"]}
    assert {lecture_.item, lecture_.note, lecture_.kept_note, COURSE} == set(live(changes, "item"))
    assert set(live(changes, "relation")) == {f"attachment:{lecture_.audio_attachment}|about|item:{COURSE}"}
    assert [await held(db, sha) for sha in doomed_blobs] == [False, False, False]
    assert [blob_store.path_of(sha).exists() for sha in doomed_blobs] == [False, False, False]
    assert all([await held(db, sha) for sha in (lecture_.audio, lecture_.audio_notes["content_blob"])])
    assert purged.finished
    assert purged.target.blobs == tuple(sorted(doomed_blobs))


async def test_a_note_forked_from_a_purged_artifact_stays_and_is_marked(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    before = live(await feed(db), "item")

    purged = await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())

    items = live(await feed(db), "item")
    marked = items[lecture_.note]
    assert {name: value for name, value in marked.items() if name not in ("evidence_purged_at", "rev")} == {
        name: value for name, value in before[lecture_.note].items() if name != "rev"
    }
    assert marked["fields"]["forked_from_artifact"] == lecture_.summary["id"]
    assert "evidence_purged_at" in marked
    assert "evidence_purged_at" not in items[lecture_.kept_note]
    assert purged.marked == (lecture_.note,)


async def test_a_soft_deleted_fork_note_is_marked_too(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    await apply(db, device, {"mutation_id": new_id(), "op": "delete", "entity": "item", "id": lecture_.note})

    await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())

    marked = live(await feed(db), "item")[lecture_.note]
    assert {"deleted_at", "evidence_purged_at"} <= marked.keys()


async def test_a_fork_note_created_after_the_purge_of_its_artifact_arrives_marked(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())
    late_id, create_late = note(lecture_.summary["id"], "오프라인 정리")
    kept_id, create_kept = note(lecture_.audio_notes["id"], "오프라인 녹음 정리")

    results = await apply(db, device, create_late, create_kept)

    assert [result["result"] for result in results] == ["applied", "applied"]
    [(purged_at,)] = await db.read(lambda snap: snap.execute("SELECT registered_at FROM purge_journal").fetchall())
    items = live(await feed(db), "item")
    assert items[late_id]["evidence_purged_at"] == purged_at
    assert "evidence_purged_at" not in items[kept_id]


async def test_fork_notes_the_user_chose_go_with_the_purge(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    stale = {"from_hash": None, "to": "다른 장치에서 쓴 글"}
    await apply(db, device, {"mutation_id": new_id(), "op": "patch", "entity": "item", "id": lecture_.note, "set": {"body": stale}})

    purged = await purge.purge_blob(db, blob_store, lecture_.pdf, delete_notes=[lecture_.note], hooks=PurgeHooks())

    changes = await feed(db)
    assert ("item", lecture_.note) in tombstones(changes)
    assert [row for row in live(changes, "relation").values() if mentions(row, lecture_.note)] == []
    assert purged.target.items == (lecture_.note,)
    assert purged.marked == ()
    assert await db.read(lambda snap: snap.execute("SELECT count(*) FROM conflict_stash WHERE entity_id = ?", (lecture_.note,)).fetchone()) == (0,)


async def test_a_note_the_purge_does_not_reach_cannot_be_chosen_and_nothing_is_purged(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    before = await feed(db)

    with pytest.raises(PurgeRefused):
        await purge.purge_blob(db, blob_store, lecture_.pdf, delete_notes=[lecture_.kept_note], hooks=PurgeHooks())

    assert await feed(db) == before
    assert blob_store.path_of(lecture_.pdf).exists()


async def test_a_blob_the_hub_does_not_hold_is_refused(db, blob_store):
    with pytest.raises(PurgeRefused):
        await purge.purge_blob(db, blob_store, "e" * 64, hooks=PurgeHooks())


async def test_the_plan_shows_what_a_purge_removes_and_the_notes_it_would_mark(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    before = await feed(db)

    plan = await db.read(lambda snap: purge.plan(snap, lecture_.pdf))

    assert plan.target.attachments == (lecture_.pdf_attachment,)
    assert set(plan.target.artifacts) == {lecture_.transcript["id"], lecture_.summary["id"]}
    assert plan.target.suggestions == (lecture_.suggestion["id"],)
    assert len(plan.target.relations) == 4
    assert [(note_.id, note_.title, note_.deleted) for note_ in plan.fork_notes] == [(lecture_.note, "정리", False)]
    assert plan.filenames == ("3주차.pdf",)
    assert await feed(db) == before


@pytest.mark.parametrize("holder", ["attachment", "artifact"])
async def test_a_derived_blob_something_kept_also_names_stays(db, device, blob_store, tmp_path, holder):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    shared = lecture_.transcript["content_blob"]
    if holder == "attachment":
        same_bytes = {"id": new_id(), "created_at": "2026-03-03T00:30:00.000Z", "origin": {"type": "import"}, "links": [],
                      "attachments": [{"id": new_id(), "blob_sha256": shared, "role": "file", "cloud_policy": "local_only"}]}
        await apply(db, device, commit(same_bytes))
        kept = ("attachment", same_bytes["attachments"][0]["id"])
    else:
        same_output = await made(db, blob_store, tmp_path, "ocr.audio", f"attachment:{lecture_.audio_attachment}", [lecture_.audio], b"slide text")
        assert same_output["content_blob"] == shared
        kept = ("artifact", same_output["id"])

    purged = await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())

    assert shared not in purged.target.blobs
    assert await held(db, shared) and blob_store.path_of(shared).exists()
    assert kept[1] in live(await feed(db), kept[0])
    assert lecture_.transcript["id"] in purged.target.artifacts


async def test_an_artifact_made_from_bytes_a_kept_artifact_also_holds_stays(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    same_output = await made(db, blob_store, tmp_path, "transcribe.alt", f"attachment:{lecture_.audio_attachment}", [lecture_.audio], b"slide text")
    assert same_output["content_blob"] == lecture_.transcript["content_blob"]

    purged = await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())

    changes = await feed(db)
    assert purged.target.artifacts == (lecture_.transcript["id"],)
    assert purged.target.blobs == (lecture_.pdf,)
    assert {lecture_.summary["id"], same_output["id"]} <= set(live(changes, "artifact"))
    assert lecture_.suggestion["id"] in live(changes, "suggestion")
    assert purged.marked == ()
    assert await held(db, lecture_.summary["content_blob"])


async def test_purging_an_artifacts_content_removes_that_artifact_and_what_was_made_from_it(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)

    purged = await purge.purge_blob(db, blob_store, lecture_.transcript["content_blob"], hooks=PurgeHooks())

    assert purged.target.artifacts == tuple(sorted([lecture_.transcript["id"], lecture_.summary["id"]]))
    assert purged.target.attachments == ()
    assert await held(db, lecture_.pdf)


async def test_purging_cancels_the_jobs_still_to_run_on_what_it_removes(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    [ingest] = await db.read(lambda snap: jobs.of_subject(snap, f"capture:{lecture_.capture}"))

    async def queued(job_type, subject, *inputs):
        return await db.write(lambda tx: jobs.enqueue(tx, JobSpec(job_type, subject, inputs, {"n": 2}), Pipeline(f"{job_type}-2")))

    on_both = await queued("summarize", f"artifact:{lecture_.transcript['id']}", lecture_.transcript["content_blob"])
    on_attachment = await queued("thumbnail", f"attachment:{lecture_.pdf_attachment}", lecture_.audio)
    on_artifact = await queued("translate", f"artifact:{lecture_.transcript['id']}", lecture_.audio)
    reading = await queued("align.slides", f"capture:{lecture_.capture}", lecture_.pdf, lecture_.audio)
    other = await queued("summarize", f"artifact:{lecture_.audio_notes['id']}", lecture_.audio_notes["content_blob"])

    purged = await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())

    states = await db.read(lambda snap: {job.id: jobs.get(snap, job.id).state for job in (on_both, on_attachment, on_artifact, reading, other, ingest)})
    assert states == {on_both.id: JobState.CANCELLED, on_attachment.id: JobState.CANCELLED, on_artifact.id: JobState.CANCELLED,
                      reading.id: JobState.CANCELLED, other.id: JobState.PENDING, ingest.id: JobState.PENDING}
    cancelled = await db.read(lambda snap: {job_id for (job_id,) in snap.execute("SELECT id FROM processing_jobs WHERE state = 'cancelled'")})
    assert set(purged.cancelled) == cancelled


def two_files(pdf, audio):
    return {"id": new_id(), "created_at": "2026-03-02T00:30:00.000Z", "origin": {"type": "import"}, "links": [],
            "attachments": [{"id": new_id(), "blob_sha256": pdf, "role": "file", "cloud_policy": "local_only"},
                            {"id": new_id(), "blob_sha256": audio, "role": "audio_segment", "seq": 0, "cloud_policy": "local_only"}]}


async def test_purging_one_file_of_a_capture_leaves_its_ingest_to_plan_the_file_left(db, device, blob_store, tmp_path):
    pdf = await store_bytes(blob_store, tmp_path, b"%PDF to purge")
    audio = await store_bytes(blob_store, tmp_path, b"recording that stays")
    capture = two_files(pdf, audio)
    await apply(db, device, commit(capture))
    [ingest] = await db.read(lambda snap: jobs.of_subject(snap, f"capture:{capture['id']}"))

    await purge.purge_blob(db, blob_store, pdf, hooks=PurgeHooks())

    assert await db.read(lambda snap: jobs.get(snap, ingest.id).state) is JobState.PENDING
    registry, seen = JobRegistry(), []

    async def planner(view, context):
        seen.append([attachment["blob_sha256"] for attachment in view.attachments])
        return []

    registry.plan("files", planner)
    await JobRunner(JobContext(db, blob_store, registry)).run_one()
    assert seen == [[audio]]
    assert await db.read(lambda snap: jobs.get(snap, ingest.id).state) is JobState.SUCCEEDED


async def test_purging_the_last_file_of_a_capture_cancels_its_ingest(db, device, blob_store, tmp_path):
    pdf = await store_bytes(blob_store, tmp_path, b"%PDF to purge")
    capture = two_files(pdf, pdf)
    await apply(db, device, commit(capture))
    [ingest] = await db.read(lambda snap: jobs.of_subject(snap, f"capture:{capture['id']}"))

    purged = await purge.purge_blob(db, blob_store, pdf, hooks=PurgeHooks())

    assert ingest.job_type == INGEST
    assert await db.read(lambda snap: jobs.get(snap, ingest.id).state) is JobState.CANCELLED
    assert purged.cancelled == (ingest.id,)


async def test_a_job_cancelled_by_a_purge_records_no_artifact(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    running = await db.write(lambda tx: jobs.enqueue(tx, JobSpec("ocr", f"attachment:{lecture_.pdf_attachment}", (lecture_.pdf,), {"n": 2}), Pipeline("ocr-2")))
    content = await store_bytes(blob_store, tmp_path, b"late slide text")

    await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())

    with pytest.raises(InvalidArtifact):
        await db.write(lambda tx: artifacts.record(tx, running, "ocr", content))


async def test_hooks_run_once_after_the_commit_with_the_target(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    hooks, seen = PurgeHooks(), []
    hooks.register("index", lambda target: seen.append((target, blob_store.path_of(lecture_.pdf).exists())))

    purged = await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=hooks)
    again = await purge.finish_unfinished(db, blob_store, hooks)

    assert seen == [(purged.target, True)]
    assert again == []


async def test_a_purge_whose_hook_failed_runs_that_hook_again_when_the_core_opens(tmp_path):
    root = tmp_path / "root"
    seen = {"index": [], "thumbnails": []}

    def failing(target):
        raise OSError("index locked")

    first = PurgeHooks()
    first.register("index", failing)
    first.register("thumbnails", seen["thumbnails"].append)
    async with open_core(root, create=True, sync_roots=[], purge_hooks=first) as core:
        await import_files(core, [synthetic(tmp_path, b"%PDF to purge")])
        sha = await core.db.read(lambda snap: snap.execute("SELECT blob_sha256 FROM attachments").fetchone()[0])
        purged = await purge.purge_blob(core.db, core.blobs, sha, hooks=first)
        assert (purged.finished, purged.failed_hooks) == (False, ("index",))
        assert not core.blobs.path_of(sha).exists()
        assert live(await feed(core.db), "attachment") == {}

    second = PurgeHooks()
    second.register("index", seen["index"].append)
    second.register("thumbnails", seen["thumbnails"].append)
    async with open_core(root, create=False, sync_roots=[], purge_hooks=second) as core:
        assert seen == {"index": [purged.target], "thumbnails": [purged.target]}
        assert await purge.finish_unfinished(core.db, core.blobs, second) == []


def synthetic(tmp_path, content):
    path = tmp_path / "inbox" / f"{new_id()}.pdf"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


CRASHING_PURGE = f"""
import asyncio, os, sys
from pathlib import Path
from campuslife_hub import purge
from campuslife_hub.core import open_core

async def crash(*args, **kwargs):
    os._exit({CRASH})

async def main(root, sha):
    purge._finish = crash
    async with open_core(Path(root), create=False, sync_roots=[]) as core:
        await purge.purge_blob(core.db, core.blobs, sha, hooks=purge.PurgeHooks())

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


async def test_after_a_crash_between_the_commit_and_the_file_removal_the_reopened_hub_finishes_the_purge(tmp_path):
    root = tmp_path / "root"
    async with open_core(root, create=True, sync_roots=[]) as core:
        await import_files(core, [synthetic(tmp_path, b"%PDF to purge")])
        sha = await core.db.read(lambda snap: snap.execute("SELECT blob_sha256 FROM attachments").fetchone()[0])

    crashed = subprocess.run([sys.executable, "-c", CRASHING_PURGE, str(root), sha], capture_output=True, timeout=120)
    assert crashed.returncode == CRASH, crashed.stderr.decode(errors="replace")

    seen = []
    hooks = PurgeHooks()
    hooks.register("index", seen.append)
    async with open_core(root, create=False, sync_roots=[], purge_hooks=hooks) as core:
        assert [target.blobs for target in seen] == [(sha,)]
        assert not core.blobs.path_of(sha).exists()
        assert live(await feed(core.db), "attachment") == {}


async def test_a_hook_name_is_registered_once():
    hooks = PurgeHooks()
    hooks.register("index", lambda target: None)

    with pytest.raises(ValueError):
        hooks.register("index", lambda target: None)


async def test_the_purge_target_survives_the_journal(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)
    purged = await purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks())

    [(target,)] = await db.read(lambda snap: snap.execute("SELECT target FROM purge_journal").fetchall())

    assert purge.PurgeTarget.from_json(target) == purged.target


async def test_discarding_a_blob_removes_its_file_only_once_no_row_names_it(db, blob_store, tmp_path):
    sha = await store_bytes(blob_store, tmp_path, b"stored again after a purge")

    assert await blob_store.discard(sha) == []
    assert blob_store.path_of(sha).exists()

    await db.write(lambda tx: tx.execute("DELETE FROM blobs WHERE sha256 = ?", (sha,)))
    assert await blob_store.discard(sha) == []
    assert not blob_store.path_of(sha).exists()
    assert await blob_store.discard(sha) == []


async def test_a_purge_leaves_the_files_while_a_backup_holds_blob_removals(db, device, blob_store, tmp_path):
    lecture_ = await lecture(db, device, blob_store, tmp_path)

    async with blob_store.removals:
        purging = asyncio.create_task(purge.purge_blob(db, blob_store, lecture_.pdf, hooks=PurgeHooks()))
        await asyncio.sleep(0.05)
        assert not purging.done()
        assert blob_store.path_of(lecture_.pdf).exists()
        assert not await held(db, lecture_.pdf)

    assert (await purging).finished
    assert not blob_store.path_of(lecture_.pdf).exists()


async def test_a_stored_blob_no_row_names_yet_is_purged_with_its_file(db, blob_store, tmp_path):
    sha = await store_bytes(blob_store, tmp_path, b"uploaded, never committed")

    purged = await purge.purge_blob(db, blob_store, sha, hooks=PurgeHooks())

    assert purged.target == purge.PurgeTarget(blobs=(sha,))
    assert purged.finished
    assert not await held(db, sha)
    assert not blob_store.path_of(sha).exists()
