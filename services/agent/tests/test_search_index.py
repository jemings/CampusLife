"""The search index: a cache beside the Hub database that follows its changes feed (D27, #11 decisions 2, 3, 5, 6).

Every text here is synthetic.
"""

import asyncio
import json
import random
import sqlite3
import threading
import time
import unicodedata

import pytest
from extract_harness import hub_with
from test_capture_commit import apply, blob_store, commit, device, new_id, store_bytes
from test_purge import database_file_bytes, default_secure_delete_off

from campuslife_hub import artifacts, jobs, purge, store
from campuslife_hub.core import open_core
from campuslife_hub.data_root import Layout
from campuslife_hub.db import Database
from campuslife_hub.extract.extractors import TEXT, Extractor
from campuslife_hub.extract.limits import Limits
from campuslife_hub.jobs import JobSpec, Pipeline
from campuslife_hub.purge import PurgeHooks
from campuslife_hub.search import index as search_index
from campuslife_hub.search.index import SearchIndex, WalKept
from campuslife_hub.store import Entity, EntityRef

__all__ = ["blob_store", "default_secure_delete_off", "device"]

NOTES = "# 트리\n이진탐색트리는 정렬된 순서를 지킵니다.\n\n균형 잡힌 트리".encode()


@pytest.fixture
def index(tmp_path):
    made = SearchIndex(Layout(tmp_path).search_index)
    yield made
    made.close()


def create_item(item_id, title, body=None, kind="note"):
    data = {"kind": kind, "title": title, "fields": {}} | ({"body": body} if body is not None else {})
    return {"mutation_id": new_id(), "op": "create", "entity": "item", "id": item_id, "data": data}


def set_body(item_id, body):
    return {"mutation_id": new_id(), "op": "patch", "entity": "item", "id": item_id, "set": {"body": {"from_hash": None, "to": body}}}


def item_op(op, item_id):
    return {"mutation_id": new_id(), "op": op, "entity": "item", "id": item_id}


async def sources(index, query):
    return [hit.source for hit in await index.find(query)]


async def extraction(db, blob_store, tmp_path, blocks, *, subject_bytes=b"synthetic original"):
    """An extraction Artifact of blocks [(text, refs)] about a Blob of subject_bytes, written as the extractor's job would."""
    sha = await store_bytes(blob_store, tmp_path, subject_bytes)
    body = {"v": 1, "blocks": [{"id": f"b{i}", "block_type": "paragraph", "text": text, "refs": refs} for i, (text, refs) in enumerate(blocks)]}
    content = await store_bytes(blob_store, tmp_path, json.dumps(body, ensure_ascii=False).encode("utf-8", "surrogatepass"))
    job = await db.write(lambda tx: jobs.enqueue(tx, JobSpec("extract.text", f"blob:{sha}", (sha,), {"n": new_id()}), Pipeline("text-1")))
    return sha, await db.write(lambda tx: artifacts.record(tx, job, "extraction", content))


async def test_an_item_is_found_by_its_title_and_body_once_the_index_catches_up(db, device, blob_store, index):
    note = new_id()
    await apply(db, device, create_item(note, "자료구조 정리", "이진탐색트리를 복습합니다"))

    assert await index.catch_up(db, blob_store) > 0

    assert await sources(index, "자료구조") == [f"item:{note}"]
    assert await sources(index, "탐색트리") == [f"item:{note}"]
    assert await index.catch_up(db, blob_store) == 0


async def test_catching_up_ends_before_the_next_batch_once_stop_is_set(db, device, blob_store, index):
    await apply(db, device, create_item(new_id(), "자료구조"))
    stop = asyncio.Event()
    stop.set()

    assert await index.catch_up(db, blob_store, stop=stop) == 0

    assert await index.documents() == []


async def test_an_extraction_is_found_block_by_block_with_its_locators_and_an_attachment_by_its_file_name(db, device, blob_store, tmp_path, index):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("자료구조노트.md", NOTES))
    await hub.settle()

    await index.catch_up(db, blob_store)

    [extracted] = await hub.artifacts("extraction")
    body, _ = await hub.body(extracted)
    [hit] = await index.find("정렬된")
    assert (hit.source, hit.part, hit.text) == (extracted["subject_ref"], "block", "이진탐색트리는 정렬된 순서를 지킵니다.")
    assert (hit.block_id, hit.refs) == (body["blocks"][1]["id"], body["blocks"][1]["refs"])
    assert await sources(index, "노트") == [f"attachment:{capture['attachments'][0]['id']}"]


async def test_a_new_extraction_of_a_blob_replaces_the_blocks_of_the_one_before(db, device, blob_store, tmp_path, index):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("a.md", NOTES))
    await hub.settle()
    await index.catch_up(db, blob_store)
    newer = hub_with(db, blob_store, device, tmp_path, Extractor("text", "2", TEXT.entry, TEXT.accepts), base=())
    await newer.capture(("again.md", NOTES))
    await newer.settle()

    await index.catch_up(db, blob_store)

    old, new = await hub.artifacts("extraction")
    new_body, _ = await hub.body(new)
    blocks = [document for document in await index.documents() if document.part == "block"]
    assert [document.block_id for document in blocks] == [block["id"] for block in new_body["blocks"]]
    assert (await db.read(lambda snap: artifacts.current(snap, old["subject_ref"], "extraction")))["id"] == new["id"]


async def test_the_current_extraction_of_a_subject_is_not_another_type_recorded_after_it(db, blob_store, tmp_path, index):
    sha, made = await extraction(db, blob_store, tmp_path, [("정렬 알고리즘", [])])
    job = await db.write(lambda tx: jobs.enqueue(tx, JobSpec("summarize", f"blob:{sha}", (sha,), {}), Pipeline("summary-1")))
    await db.write(lambda tx: artifacts.record(tx, job, "summary", sha))

    assert (await db.read(lambda snap: artifacts.current(snap, f"blob:{sha}", "extraction")))["id"] == made["id"]
    await index.catch_up(db, blob_store)
    assert await sources(index, "정렬") == [f"blob:{sha}"]


async def test_a_soft_deleted_item_leaves_the_index_and_comes_back_when_restored(db, device, blob_store, index):
    note = new_id()
    await apply(db, device, create_item(note, "그래프 탐색"))
    await index.catch_up(db, blob_store)

    await apply(db, device, item_op("delete", note))
    await index.catch_up(db, blob_store)
    assert await sources(index, "그래프") == []

    await apply(db, device, item_op("restore", note))
    await index.catch_up(db, blob_store)
    assert await sources(index, "그래프") == [f"item:{note}"]


async def test_tombstones_on_the_feed_take_their_documents_without_the_purge_hook(db, device, blob_store, tmp_path, index):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture(("자료구조노트.md", NOTES))
    await hub.settle()
    await index.catch_up(db, blob_store)
    assert await index.documents() != []

    await purge.purge_blob(db, blob_store, (await hub.artifacts("extraction"))[0]["input_hashes"][0], hooks=PurgeHooks())
    await index.catch_up(db, blob_store)

    assert await index.documents() == []


async def test_a_changed_body_replaces_the_item_s_documents_and_a_touch_writes_nothing(db, device, blob_store, index):
    note = new_id()
    await apply(db, device, create_item(note, "스택"))
    await index.catch_up(db, blob_store)
    await apply(db, device, set_body(note, "후입선출 구조"))
    await index.catch_up(db, blob_store)
    held = await index.documents()
    assert [(document.part, document.text) for document in held] == [("title", "스택"), ("body", "후입선출 구조")]
    await apply(db, device, create_item(new_id(), "큐"))
    await index.catch_up(db, blob_store)
    await db.write(lambda tx: store.touch(tx, EntityRef(Entity.ITEM, note)))
    conn = sqlite3.connect(index._path)
    ids_before = conn.execute("SELECT id FROM docs ORDER BY id").fetchall()

    await index.catch_up(db, blob_store)

    assert conn.execute("SELECT id FROM docs ORDER BY id").fetchall() == ids_before
    conn.close()


async def test_entities_the_index_does_not_know_or_hold_are_passed_over(db, device, blob_store, tmp_path, index):
    def unknown_row(tx):
        (rev,) = tx.execute("UPDATE hub_state SET change_seq = change_seq + 1 RETURNING change_seq").fetchone()
        tx.execute("INSERT INTO sync_index (entity, id, rev, deleted) VALUES ('later_entity', ?, ?, 0)", (new_id(), rev))

    await db.write(unknown_row)
    await apply(db, device, {"mutation_id": new_id(), "op": "create", "entity": "tag", "id": new_id(), "data": {"name": "시험범위"}})
    sha = await store_bytes(blob_store, tmp_path, b"synthetic")
    job = await db.write(lambda tx: jobs.enqueue(tx, JobSpec("summarize", f"blob:{sha}", (sha,), {}), Pipeline("summary-1")))
    await db.write(lambda tx: artifacts.record(tx, job, "summary", sha))
    note = new_id()
    await apply(db, device, create_item(note, "큐 정리"))

    await index.catch_up(db, blob_store)

    assert [document.source for document in await index.documents()] == [f"item:{note}"]


async def scenario(db, device, blob_store, tmp_path, *indexes):
    """Changes of every kind the index follows, each index catching up after each step and forgetting each purge."""
    hub = hub_with(db, blob_store, device, tmp_path)
    kept, deleted, gone = new_id(), new_id(), new_id()
    hooks = PurgeHooks()
    for number, each in enumerate(indexes):
        hooks.register(f"search-{number}", each.forget)

    async def step(*ops):
        if ops:
            await apply(db, device, *ops)
        for each in indexes:
            await each.catch_up(db, blob_store)

    await step(create_item(kept, "자료구조"), create_item(deleted, "알고리즘", "정렬"), create_item(gone, "그래프"))
    await hub.capture(("a.md", NOTES), ("b.txt", "둘째 파일 문단".encode()))
    await hub.settle()
    await step(set_body(kept, "트리와 힙"), item_op("delete", deleted))
    newer = hub_with(db, blob_store, device, tmp_path, Extractor("text", "2", TEXT.entry, TEXT.accepts), base=())
    await newer.capture(("again.md", NOTES))
    await newer.settle()
    await step(item_op("restore", deleted), item_op("delete", gone))
    extractions = await hub.artifacts("extraction")
    [newer_extraction] = [made for made in extractions if made["pipeline_version"].startswith("text-2")]
    await purge.purge_blob(db, blob_store, newer_extraction["content_blob"], hooks=hooks)
    await step()
    [other] = [made for made in extractions if made["subject_ref"] != newer_extraction["subject_ref"]]
    await purge.purge_blob(db, blob_store, other["input_hashes"][0], hooks=hooks)
    await step()


async def test_an_index_built_step_by_step_holds_what_one_built_from_since_0_holds(db, device, blob_store, tmp_path, index):
    await scenario(db, device, blob_store, tmp_path, index)
    rebuilt = SearchIndex(tmp_path / "rebuilt" / "search.sqlite3")

    await rebuilt.catch_up(db, blob_store)

    held, built_once = await index.documents(), await rebuilt.documents()
    rebuilt.close()
    assert {document.source.partition(":")[0] for document in held} == {"item", "attachment", "blob"}
    assert held == built_once


def plant_stray(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("INSERT INTO docs (source, part, original, folded) VALUES ('item:stray', 'title', 'stray', 'stray')")
    conn.close()


async def built_with_a_stray(db, device, blob_store, index):
    note = new_id()
    await apply(db, device, create_item(note, "자료구조"))
    await index.catch_up(db, blob_store)
    index.close()
    plant_stray(index._path)
    return [f"item:{note}"]


async def held_sources(index):
    return [document.source for document in await index.documents()]


async def test_an_index_built_for_this_hub_and_version_is_kept(db, device, blob_store, index):
    expected = await built_with_a_stray(db, device, blob_store, index)

    await index.catch_up(db, blob_store)

    assert await held_sources(index) == [*expected, "item:stray"]


async def test_an_index_built_by_another_index_version_is_built_again(db, device, blob_store, index, monkeypatch):
    expected = await built_with_a_stray(db, device, blob_store, index)
    monkeypatch.setattr(search_index, "INDEX_VERSION", "0:other")

    assert await index.catch_up(db, blob_store) > 0

    assert await held_sources(index) == expected


@pytest.mark.parametrize("column", ["epoch", "hub_id"])
async def test_an_index_built_for_another_epoch_or_hub_is_built_again(db, device, blob_store, index, column):
    expected = await built_with_a_stray(db, device, blob_store, index)
    await db.write(lambda tx: tx.execute(f"UPDATE hub_state SET {column} = ?", (new_id(),)))

    await index.catch_up(db, blob_store)

    assert await held_sources(index) == expected


async def test_a_missing_index_file_is_built_again_from_since_0(db, device, blob_store, index):
    note = new_id()
    await apply(db, device, create_item(note, "자료구조"))
    await index.catch_up(db, blob_store)
    index.close()
    index._path.unlink()

    assert await index.catch_up(db, blob_store) > 0

    assert await held_sources(index) == [f"item:{note}"]


async def test_text_sqlite_cannot_store_is_indexed_with_spaces_in_its_place_and_a_block_without_text_is_not(db, blob_store, tmp_path, index):
    decomposed = unicodedata.normalize("NFD", "정렬")
    sha, _ = await extraction(db, blob_store, tmp_path, [("짝없는\ud800대리\x00문자", []), ("", []), (decomposed, [])])

    await index.catch_up(db, blob_store)

    assert [document.text for document in await index.documents()] == ["짝없는 대리 문자", "정렬"]
    assert await sources(index, "대리") == [f"blob:{sha}"]


MARKER = "퍼지표식 ZQXJ7731"
MARKER_BYTES = [text.encode() for text in ("ZQXJ7731", "zqxj7731", "퍼지표식", "퍼지", "지표", "표식")]


def cache_bytes(index):
    return database_file_bytes(index._path)


async def marked(db, device, blob_store, tmp_path, index):
    hub = hub_with(db, blob_store, device, tmp_path)
    await hub.capture((f"{MARKER}.md", f"{MARKER} 문단\n\n둘째 문단".encode()), ("other.md", "다른 자료의 문단".encode()))
    await hub.settle()
    await index.catch_up(db, blob_store)
    held = cache_bytes(index)
    assert all(text in held for text in MARKER_BYTES[:3])
    return hub


async def test_a_purge_leaves_no_byte_of_what_it_removed_in_the_index_files(default_secure_delete_off, db, device, blob_store, tmp_path, index):
    hub = await marked(db, device, blob_store, tmp_path, index)
    hooks = PurgeHooks()
    hooks.register("search", index.forget)
    [marked_extraction] = [artifact for artifact in await hub.artifacts("extraction") if MARKER.encode() in (await hub.body(artifact))[1]]

    purged = await purge.purge_blob(db, blob_store, marked_extraction["input_hashes"][0], hooks=hooks)

    assert purged.finished
    left = cache_bytes(index)
    assert [text for text in MARKER_BYTES if text in left] == []
    assert await sources(index, "다른") != []


KEPT_BLOCKS = 6000
ROUNDS = 20
MARKED_PER_ROUND = 10
"""Each round indexes kept and marked blocks in one transaction, so the markers share every FTS5 segment with kept text
and a purge deletes from segments it cannot drop whole."""
KEPT_SYLLABLES = [chr(0xAC00 + 2 * i) for i in range(150)]
MARKER_SYLLABLES = [chr(0xAC01 + 2 * i) for i in range(150)]
"""Syllables only markers use, falling between the kept text's in the index's term order."""


def words(rng, syllables, count, low=2, high=5):
    return " ".join("".join(rng.choice(syllables) for _ in range(rng.randint(low, high))) for _ in range(count))


def index_terms_holding(index, syllables):
    conn = sqlite3.connect(index._path)
    try:
        return [bytes(term).decode("utf-8", "replace") for (term,) in conn.execute("SELECT term FROM fts_idx") if any(s.encode() in bytes(term) for s in syllables)]
    finally:
        conn.close()


async def test_a_purge_leaves_no_byte_of_what_it_removed_from_segments_it_shares_with_kept_text(default_secure_delete_off, db, blob_store, tmp_path, index):
    rng = random.Random(11)
    markers, purged = [], []
    for round_ in range(ROUNDS):
        kept = [(words(rng, KEPT_SYLLABLES, 25), []) for _ in range(KEPT_BLOCKS // ROUNDS)]
        await extraction(db, blob_store, tmp_path, kept, subject_bytes=f"kept original {round_}".encode())
        marked_here = [words(rng, MARKER_SYLLABLES, 1, 6, 6) for _ in range(MARKED_PER_ROUND)]
        sha, _ = await extraction(db, blob_store, tmp_path, [(f"{words(rng, KEPT_SYLLABLES, 5)} {marker}", []) for marker in marked_here], subject_bytes=f"purged original {round_}".encode())
        markers += marked_here
        purged.append(sha)
        await index.catch_up(db, blob_store)
    marker_bigrams = {marker[j : j + 2].encode() for marker in markers for j in range(len(marker) - 1)}
    held = cache_bytes(index)
    assert all(bigram in held for bigram in marker_bigrams)
    hooks = PurgeHooks()
    hooks.register("search", index.forget)

    for sha in purged:
        assert (await purge.purge_blob(db, blob_store, sha, hooks=hooks)).finished

    left = cache_bytes(index)
    assert [bigram.decode() for bigram in marker_bigrams if bigram in left] == []
    assert index_terms_holding(index, MARKER_SYLLABLES) == []
    assert len(await index.documents()) == KEPT_BLOCKS


async def test_a_connection_deleting_with_secure_delete_off_leaves_the_marker_in_the_index_files(db, device, blob_store, tmp_path, index):
    await marked(db, device, blob_store, tmp_path, index)
    index.close()
    control = sqlite3.connect(index._path, isolation_level=None)
    control.execute("PRAGMA secure_delete = OFF")
    control.execute("INSERT INTO fts (fts, rank) VALUES ('secure-delete', 0)")
    control.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM docs WHERE original LIKE '%ZQXJ7731%')")
    control.execute("DELETE FROM docs WHERE original LIKE '%ZQXJ7731%'")
    control.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    control.close()

    left = cache_bytes(index)
    assert [text for text in MARKER_BYTES if text in left] != []


@pytest.mark.parametrize("named", ["items", "attachments", "artifacts", "blobs"])
async def test_forget_drops_the_documents_of_what_a_purge_target_names_and_no_others(db, device, blob_store, tmp_path, index, named):
    hub = hub_with(db, blob_store, device, tmp_path)
    capture = await hub.capture(("자료구조노트.md", NOTES))
    note = new_id()
    await apply(db, device, create_item(note, "큐 정리"))
    await hub.settle()
    await index.catch_up(db, blob_store)
    [extracted] = await hub.artifacts("extraction")
    attachment = capture["attachments"][0]["id"]
    blob = extracted["subject_ref"].removeprefix("blob:")
    targets = {
        "items": (f"item:{note}", note),
        "attachments": (f"attachment:{attachment}", attachment),
        "artifacts": (extracted["subject_ref"], extracted["id"]),
        "blobs": (extracted["subject_ref"], blob),
    }
    source, identifier = targets[named]
    before = set(await held_sources(index))

    index.forget(purge.PurgeTarget(**{named: (identifier,)}))

    assert set(await held_sources(index)) == before - {source}


async def test_forget_on_a_hub_that_never_indexed_makes_no_file(index):
    index.forget(purge.PurgeTarget(blobs=("0" * 64,)))

    assert not index._path.exists()


async def test_indexing_the_largest_body_keeps_the_event_loop_answering_within_a_second(db, blob_store, tmp_path, index):
    limits = Limits()
    per_block = limits.max_text_chars // limits.max_blocks
    line = ("이진탐색트리 균형 회전 " * per_block)[:per_block]
    await extraction(db, blob_store, tmp_path, [(line, []) for _ in range(limits.max_blocks)])
    worst = 0.0
    indexing = asyncio.create_task(index.catch_up(db, blob_store))
    while not indexing.done():
        started = time.monotonic()
        await asyncio.sleep(0.01)
        worst = max(worst, time.monotonic() - started - 0.01)
    await indexing

    assert len(await index.documents()) == limits.max_blocks
    assert worst < 1.0, worst
    print(f"worst loop lag {worst:.3f} s")


async def capture_named(core, filename):
    sha = await store_bytes(core.blobs, core.layout.root, f"{filename} synthetic".encode())
    capture = {
        "id": new_id(), "created_at": "2026-03-02T00:30:00.000Z", "origin": {"type": "import"}, "links": [],
        "attachments": [{"id": new_id(), "blob_sha256": sha, "role": "file", "original_filename": filename, "cloud_policy": "local_only"}],
    }
    await apply(core.db, core.hub_device, commit(capture))
    await core.search.catch_up(core.db, core.blobs)
    return sha


async def test_a_purge_through_a_core_forgets_what_it_removed_from_that_core_s_index(tmp_path):
    for _ in range(2):
        async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
            sha = await capture_named(core, f"{MARKER}.txt")
            assert await sources(core.search, "퍼지") != []

            await purge.purge_blob(core.db, core.blobs, sha, hooks=core.purge_hooks)

            assert await sources(core.search, "퍼지") == []


async def test_a_core_opening_finishes_the_index_s_part_of_a_purge_a_stopped_hub_left(tmp_path, monkeypatch):
    def fails(self, target):
        raise OSError("synthetic")

    with monkeypatch.context() as patched:
        patched.setattr(SearchIndex, "forget", fails)
        async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
            sha = await capture_named(core, f"{MARKER}.txt")
            assert not (await purge.purge_blob(core.db, core.blobs, sha, hooks=core.purge_hooks)).finished
            assert await sources(core.search, "퍼지") != []

            path = core.search._path
    assert MARKER_BYTES[0] in database_file_bytes(path)

    async with open_core(tmp_path / "root", create=False, sync_roots=[]):
        pass

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM docs WHERE original LIKE '%ZQXJ7731%'").fetchone() == (0,)
    conn.close()
    assert [text for text in MARKER_BYTES if text in database_file_bytes(path)] == []


def reading(path):
    """A connection holding a read transaction on the index, which keeps a checkpoint from emptying its WAL."""
    reader = sqlite3.connect(path, isolation_level=None, timeout=0.1)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM docs").fetchone()
    return reader


async def test_forget_says_so_when_a_reader_keeps_it_from_emptying_the_wal(db, device, blob_store, index):
    note = new_id()
    await apply(db, device, create_item(note, MARKER))
    await index.catch_up(db, blob_store)
    reader = reading(index._path)

    try:
        with pytest.raises(WalKept):
            index.forget(purge.PurgeTarget(items=(note,)))
    finally:
        reader.close()


async def test_a_purge_a_reader_keeps_from_emptying_the_index_wal_stays_unfinished_until_finished_again(default_secure_delete_off, db, blob_store, tmp_path, index):
    sha, _ = await extraction(db, blob_store, tmp_path, [(f"{MARKER} 문단", [])])
    await index.catch_up(db, blob_store)
    hooks = PurgeHooks()
    hooks.register("search", index.forget)
    reader = reading(index._path)
    try:
        purged = await purge.purge_blob(db, blob_store, sha, hooks=hooks)
    finally:
        reader.close()
    assert (purged.finished, purged.failed_hooks) == (False, ("search",))

    [finished] = await purge.finish_unfinished(db, blob_store, hooks)

    assert finished.finished
    assert [text for text in MARKER_BYTES if text in cache_bytes(index)] == []


async def test_a_purge_that_comes_while_a_batch_indexes_waits_for_it_and_leaves_nothing(default_secure_delete_off, db, blob_store, tmp_path, index, monkeypatch):
    sha, _ = await extraction(db, blob_store, tmp_path, [(f"{MARKER} 문단", [])])
    hooks = PurgeHooks()
    hooks.register("search", index.forget)
    real = search_index._documents_of
    read_the_body, go_on = threading.Event(), threading.Event()

    def held_after_reading(*args):
        documents = real(*args)
        read_the_body.set()
        go_on.wait(10)
        return documents

    monkeypatch.setattr(search_index, "_documents_of", held_after_reading)
    loop = asyncio.get_running_loop()
    batch = asyncio.create_task(index.catch_up(db, blob_store))
    await loop.run_in_executor(None, read_the_body.wait, 10)
    purging = asyncio.create_task(purge.purge_blob(db, blob_store, sha, hooks=hooks))
    await asyncio.sleep(0.3)
    go_on.set()
    await batch
    assert (await purging).finished

    assert [text for text in MARKER_BYTES if text in cache_bytes(index)] == []
    assert await index.documents() == []


async def test_a_job_about_an_older_extraction_does_not_make_it_current_again(db, blob_store, tmp_path, index):
    sha, older = await extraction(db, blob_store, tmp_path, [("옛날추출 문단", [])])
    newer_body = await store_bytes(blob_store, tmp_path, json.dumps({"v": 1, "blocks": [{"id": "c0", "block_type": "paragraph", "text": "새추출 문단", "refs": []}]}, ensure_ascii=False).encode())
    job = await db.write(lambda tx: jobs.enqueue(tx, JobSpec("extract.text", f"blob:{sha}", (sha,), {"n": new_id()}), Pipeline("text-2")))
    newer = await db.write(lambda tx: artifacts.record(tx, job, "extraction", newer_body))

    await db.write(lambda tx: jobs.enqueue(tx, JobSpec("summarize", f"artifact:{older['id']}", (older["content_blob"],), {}), Pipeline("summary-1")))
    await index.catch_up(db, blob_store)

    assert (await db.read(lambda snap: artifacts.current(snap, f"blob:{sha}", "extraction")))["id"] == newer["id"]
    assert (await sources(index, "새추출"), await sources(index, "옛날추출")) == ([f"blob:{sha}"], [])


async def test_a_core_that_cannot_register_its_index_hook_closes_its_database(tmp_path, monkeypatch):
    closed = []
    real = Database.close

    async def close(self):
        closed.append(self)
        await real(self)

    monkeypatch.setattr(Database, "close", close)
    taken = PurgeHooks()
    taken.register("search", lambda target: None)

    with pytest.raises(ValueError):
        async with open_core(tmp_path / "root", create=True, sync_roots=[], purge_hooks=taken):
            pass

    assert len(closed) == 1
