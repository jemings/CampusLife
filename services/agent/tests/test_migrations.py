import sqlite3

import pytest

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
