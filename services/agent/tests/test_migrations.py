import asyncio
import json
import sqlite3

import pytest
from campuslife_contracts import validate

from campuslife_hub.data_root import Layout
from campuslife_hub.db import Database
from campuslife_hub.lock import HubLock
from campuslife_hub.migrations import MIGRATIONS, DatabaseTooNew, Migration, migrate


def user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def connect():
    return sqlite3.connect(":memory:", isolation_level=None)


def test_a_new_database_gets_every_migration():
    conn = connect()
    migrate(conn)
    assert user_version(conn) == len(MIGRATIONS) >= 1


def test_migrating_twice_changes_nothing():
    conn = connect()
    migrate(conn)
    schema = conn.execute("SELECT sql FROM sqlite_schema ORDER BY name").fetchall()
    migrate(conn)
    assert conn.execute("SELECT sql FROM sqlite_schema ORDER BY name").fetchall() == schema


def test_only_pending_migrations_run():
    conn = connect()
    first = Migration(1, "first", "CREATE TABLE a (x)")
    second = Migration(2, "second", "CREATE TABLE b (x)")
    migrate(conn, [first])
    migrate(conn, [first, second])
    assert user_version(conn) == 2
    assert {row[0] for row in conn.execute("SELECT name FROM sqlite_schema")} == {"a", "b"}


def test_a_database_newer_than_the_code_is_refused():
    conn = connect()
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    with pytest.raises(DatabaseTooNew) as refused:
        migrate(conn)
    assert str(len(MIGRATIONS) + 1) in str(refused.value)


def test_a_failing_migration_leaves_the_database_at_the_last_good_version():
    conn = connect()
    good = Migration(1, "good", "CREATE TABLE a (x)")
    broken = Migration(2, "broken", "CREATE TABLE b (x); CREATE TABLE a (x)")
    with pytest.raises(sqlite3.Error):
        migrate(conn, [good, broken])
    assert user_version(conn) == 1
    assert {row[0] for row in conn.execute("SELECT name FROM sqlite_schema")} == {"a"}


def test_shipped_migrations_are_numbered_from_one_without_gaps():
    assert [migration.number for migration in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


DEVICE = "INSERT INTO devices (id, platform, device_name, app_version, state, created_at) VALUES ('d', 'android', 'n', '1', 'active', 't')"
TOKEN = "INSERT INTO device_tokens (token_sha256, device_id, status, created_at) VALUES ('{digest}', 'd', '{status}', 't')"
SECRET = "INSERT INTO pairing_secrets (secret_sha256, created_at, expires_at, device_id, used_at) VALUES ('{digest}', 't', 't', {device}, {used})"


@pytest.mark.parametrize(
    ("allowed", "refused"),
    [
        pytest.param([TOKEN.format(digest="a" * 64, status="current")], TOKEN.format(digest="b" * 64, status="current"), id="two current tokens"),
        pytest.param([TOKEN.format(digest="a" * 64, status="pending")], TOKEN.format(digest="b" * 64, status="pending"), id="two pending tokens"),
        pytest.param([], TOKEN.format(digest="a" * 63, status="current"), id="a token digest that is short"),
        pytest.param([], TOKEN.format(digest="A" * 64, status="current"), id="a token digest that is not lowercase hex"),
        pytest.param([], SECRET.format(digest="g" * 64, device="NULL", used="NULL"), id="a secret digest that is not hex"),
        pytest.param([SECRET.format(digest="a" * 64, device="NULL", used="NULL")], SECRET.format(digest="b" * 64, device="NULL", used="NULL"), id="two open offers"),
        pytest.param([], SECRET.format(digest="a" * 64, device="'d'", used="NULL"), id="a secret with a device and no use"),
        pytest.param([], SECRET.format(digest="a" * 64, device="NULL", used="'t'"), id="a secret used by no device"),
        pytest.param([], "UPDATE devices SET last_status = '{}' WHERE id = 'd'", id="a heartbeat without its time"),
    ],
)
def test_migration_0008_refuses_rows_the_device_code_never_writes(allowed, refused):
    conn = connect()
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    for statement in [DEVICE, *allowed]:
        conn.execute(statement)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(refused)


STASHED = "INSERT INTO conflict_stash (id, mutation_id, device_id, entity, entity_id, field, change, theirs_rev, stashed_at) VALUES (?, ?, 'd', ?, ?, ?, ?, NULL, 't')"
PATCHED = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TAGGED = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
ITEM = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
TAG = "0f8fad5b-d9cb-469f-a165-70867728950e"
PURGED_NOTE = "9b2e5c1a-7d34-4f6e-8a1b-2c3d4e5f6a7b"
PURGED_ATTACHMENT = "1e7f9a2b-3c4d-4e5f-9a6b-7c8d9e0f1a2b"
LATE_ON_NOTE = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
LATE_ON_ATTACHMENT = "5d6e7f80-91a2-4b3c-9d4e-5f6a7b8c9d0e"
LATE_TEXT = "퍼지뒤도착해스태시된글QWZX"
JOURNALED = "INSERT INTO purge_journal (id, target, registered_at) VALUES ('j', ?, 't')"
PURGE_TARGET = json.dumps({"artifacts": [], "attachments": [PURGED_ATTACHMENT], "blobs": [], "items": [PURGED_NOTE], "relations": [], "suggestions": []})


def without_repeats(pairs):
    """json_group_object keeps a key as often as it comes (E8), and json.loads would hide that by keeping the last."""
    keys = [key for key, _ in pairs]
    assert len(keys) == len(set(keys)), keys
    return dict(pairs)


def test_migration_0011_turns_the_stash_into_one_conflict_record_per_mutation():
    """D24 decision 8: duplicates of a key a resend stashed again go first (E8), and each record takes its own rev
    above change_seq in the order its first value was stashed. PATCHED sorts after TAGGED and was stashed both
    before and after it, so neither mutation_id order nor last-stashed order gives these revs. What a patch stashed
    for an Item or Attachment a purge removed is dropped before that (D24 decision 2)."""
    conn = connect()
    migrate(conn, MIGRATIONS[:10])
    conn.execute("INSERT INTO hub_state (singleton, hub_id, epoch, change_seq) VALUES (1, 'h', 'e', 5)")
    conn.execute("INSERT INTO sync_index (entity, id, rev, deleted) VALUES ('item', ?, 5, 0)", (ITEM,))
    conn.execute(JOURNALED, (PURGE_TARGET,))
    body = {"from_hash": None, "to": "다른 장치에서 쓴 글"}
    stash = [
        ("s0", LATE_ON_NOTE, "item", PURGED_NOTE, "body", {"from_hash": None, "to": LATE_TEXT}),
        ("s1", PATCHED, "item", ITEM, "body", body),
        ("s2", TAGGED, "tag", TAG, "color", {"from": "#ff8800", "to": "#0088ff"}),
        ("s3", PATCHED, "item", ITEM, "title", {"to": "과제 2", "from": "과제 제출"}),
        ("s4", LATE_ON_ATTACHMENT, "attachment", PURGED_ATTACHMENT, "cloud_policy", {"from": "local_only", "to": "blocked"}),
        ("s5", PATCHED, "item", ITEM, "body", body),
    ]
    for identifier, mutation_id, entity, entity_id, key, change in stash:
        conn.execute(STASHED, (identifier, mutation_id, entity, entity_id, key, json.dumps(change, ensure_ascii=False)))

    migrate(conn)

    records = {identifier: json.loads(data, object_pairs_hook=without_repeats) for identifier, data in conn.execute("SELECT id, data FROM conflict_records")}
    assert records == {
        PATCHED: {"id": PATCHED, "entity_ref": f"item:{ITEM}", "set": {"body": body, "title": {"from": "과제 제출", "to": "과제 2"}}},
        TAGGED: {"id": TAGGED, "entity_ref": f"tag:{TAG}", "set": {"color": {"from": "#ff8800", "to": "#0088ff"}}},
    }
    assert all(validate("conflict_record/conflict_record", record) == [] for record in records.values())
    assert conn.execute("SELECT entity, id, rev, deleted FROM sync_index WHERE rev > 5 ORDER BY rev").fetchall() == [
        ("conflict_record", PATCHED, 6, 0), ("conflict_record", TAGGED, 7, 0),
    ]
    assert conn.execute("SELECT change_seq FROM hub_state").fetchone() == (7,)
    assert conn.execute("SELECT name FROM sqlite_schema WHERE name = 'conflict_stash'").fetchall() == []


def test_migration_0011_leaves_no_text_of_a_stash_it_drops_in_the_database_file(tmp_path, monkeypatch):
    """The Hub's writer migrates with secure_delete on whatever the build defaults to, so once its WAL is emptied the
    text of a stash row kept for a purged Note is not in the file."""
    layout = Layout(tmp_path)
    layout.database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(layout.database, isolation_level=None)
    migrate(conn, MIGRATIONS[:10])
    conn.execute("INSERT INTO hub_state (singleton, hub_id, epoch, change_seq) VALUES (1, 'h', 'e', 5)")
    conn.execute(JOURNALED, (PURGE_TARGET,))
    conn.execute(STASHED, ("s0", LATE_ON_NOTE, "item", PURGED_NOTE, "body", json.dumps({"from_hash": None, "to": LATE_TEXT}, ensure_ascii=False)))
    conn.close()
    assert LATE_TEXT.encode() in layout.database.read_bytes()
    real = sqlite3.connect

    def secure_delete_off(*args, **kwargs):
        opened = real(*args, **kwargs)
        opened.execute("PRAGMA secure_delete = OFF")
        return opened

    monkeypatch.setattr(sqlite3, "connect", secure_delete_off)

    async def open_the_hub():
        with HubLock.acquire(layout.lock_file) as lock:
            db = await Database.open(layout, lock=lock, create=False)
            try:
                kept = await db.read(lambda snap: snap.execute("SELECT count(*) FROM conflict_records").fetchone())
                return kept, await db.truncate_wal()
            finally:
                await db.close()

    assert asyncio.run(open_the_hub()) == ((0,), True)
    wal = layout.database.with_name(layout.database.name + "-wal")
    assert LATE_TEXT.encode() not in b"".join(path.read_bytes() for path in (layout.database, wal) if path.exists())
