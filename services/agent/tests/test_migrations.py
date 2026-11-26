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
