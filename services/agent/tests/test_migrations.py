import json
import sqlite3

import pytest
from campuslife_contracts import validate

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
PATCHED = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
TAGGED = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
ITEM = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
TAG = "0f8fad5b-d9cb-469f-a165-70867728950e"


def without_repeats(pairs):
    """json_group_object keeps a key as often as it comes (E8), and json.loads would hide that by keeping the last."""
    keys = [key for key, _ in pairs]
    assert len(keys) == len(set(keys)), keys
    return dict(pairs)


def test_migration_0011_turns_the_stash_into_one_conflict_record_per_mutation():
    """D24 decision 8: duplicates of a key a resend stashed again go first (E8), and each record takes its own rev
    above change_seq in the order its first value was stashed."""
    conn = connect()
    migrate(conn, MIGRATIONS[:10])
    conn.execute("INSERT INTO hub_state (singleton, hub_id, epoch, change_seq) VALUES (1, 'h', 'e', 5)")
    conn.execute("INSERT INTO sync_index (entity, id, rev, deleted) VALUES ('item', ?, 5, 0)", (ITEM,))
    body = {"from_hash": None, "to": "다른 장치에서 쓴 글"}
    stash = [
        ("s1", PATCHED, "item", ITEM, "body", body),
        ("s2", TAGGED, "tag", TAG, "color", {"from": "#ff8800", "to": "#0088ff"}),
        ("s3", PATCHED, "item", ITEM, "title", {"to": "과제 2", "from": "과제 제출"}),
        ("s4", PATCHED, "item", ITEM, "body", body),
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
