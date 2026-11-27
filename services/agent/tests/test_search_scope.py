"""Where a Blob sits, for the course and tag filters of search (#11 decision 4). Every text here is synthetic."""

from test_capture_commit import apply, blob_store, commit, device, new_id, store_bytes

from campuslife_hub import artifacts, jobs, store
from campuslife_hub.jobs import JobSpec, Pipeline
from campuslife_hub.search.scope import holders
from campuslife_hub.store import Entity, EntityRef

__all__ = ["blob_store", "device"]


def captured(capture_id, attachments):
    return {"id": capture_id, "created_at": "2026-03-02T00:30:00.000Z", "origin": {"type": "import"}, "links": [], "attachments": attachments}


def web_snapshot(capture_id, sha):
    """The Artifact #63 records for a link Capture; it has no input Blob, which the contract does not allow yet."""
    return {
        "id": new_id(), "artifact_type": "web_snapshot", "subject_ref": f"capture:{capture_id}", "job_key": new_id(),
        "pipeline_version": "web_snapshot-1", "input_hashes": [], "content_blob": sha, "created_at": "2026-03-02T00:31:00.000Z",
    }


def attached(sha):
    return {"id": new_id(), "blob_sha256": sha, "role": "file", "original_filename": "공지.txt", "cloud_policy": "local_only"}


async def test_a_blob_is_held_by_every_attachment_that_names_it(db, device, blob_store, tmp_path):
    sha = await store_bytes(blob_store, tmp_path, b"synthetic notice")
    first, second = attached(sha), attached(sha)
    await apply(db, device, commit(captured(new_id(), [first])), commit(captured(new_id(), [second])))

    held = await db.read(lambda snap: holders(snap, sha))

    assert held == {EntityRef(Entity.ATTACHMENT, first["id"]), EntityRef(Entity.ATTACHMENT, second["id"])}


async def test_a_snapshot_blob_is_held_by_the_capture_its_web_snapshot_artifact_is_about(db, device, blob_store, tmp_path):
    link = new_id()
    await apply(db, device, commit(captured(link, [])))
    snapshot = await store_bytes(blob_store, tmp_path, b"<html>synthetic notice page</html>")
    artifact = web_snapshot(link, snapshot)
    await db.write(lambda tx: store.write(tx, EntityRef(Entity.ARTIFACT, artifact["id"]), artifact))

    held = await db.read(lambda snap: holders(snap, snapshot))

    assert held == {EntityRef(Entity.CAPTURE, link)}


async def test_a_blob_only_another_artifact_type_holds_has_no_holder(db, device, blob_store, tmp_path):
    link = new_id()
    await apply(db, device, commit(captured(link, [])))
    content = await store_bytes(blob_store, tmp_path, b"synthetic summary")
    job = await db.write(lambda tx: jobs.enqueue(tx, JobSpec("summarize", f"capture:{link}", (content,), {}), Pipeline("summary-1")))
    await db.write(lambda tx: artifacts.record(tx, job, "summary", content))

    assert await db.read(lambda snap: holders(snap, content)) == set()
