import re
import sqlite3
from pathlib import Path

import pytest

DDL = Path(__file__).resolve().parents[1] / "ddl" / "client.sql"

ROOM_COLUMN_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB"}


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(DDL.read_text(encoding="utf-8"))
    yield connection
    connection.close()


def tables(db) -> set[str]:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {name for (name,) in rows}


def columns(db, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def test_ddl_creates_the_client_sync_tables(db):
    assert tables(db) == {
        "meta",
        "entity_cache",
        "mutation_outbox",
        "blob_transfer",
        "local_capture",
        "local_attachment",
    }


def test_meta_holds_the_base_epoch_and_the_cursor_and_the_outbox_holds_the_ack_time(db):
    assert {"hub_id", "epoch", "cursor"} <= set(columns(db, "meta"))
    assert "acked_at_ms" in columns(db, "mutation_outbox")


def test_ddl_uses_only_what_room_can_declare(db):
    kinds = {kind for (kind,) in db.execute("SELECT type FROM sqlite_master")}
    assert kinds <= {"table", "index"}
    for table in tables(db):
        strict, without_rowid = db.execute(
            "SELECT strict, wr FROM pragma_table_list WHERE schema = 'main' AND name = ?", (table,)
        ).fetchone()
        assert (table, strict, without_rowid) == (table, 0, 0)
        (sql,) = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
        assert not re.search(r"\bCHECK\b", sql, re.IGNORECASE), table
        for row in db.execute(f"PRAGMA table_info({table})"):
            assert row[2] in ROOM_COLUMN_TYPES, (table, row[1], row[2])
        for row in db.execute(f"PRAGMA index_list({table})"):
            assert row[4] == 0, (table, row[1])


def test_outbox_order_is_never_reused(db):
    insert = (
        "INSERT INTO mutation_outbox (mutation_id, op, entity, entity_id, body, state)"
        " VALUES (?, 'delete', 'item', 'i', '{}', 'pending')"
    )
    db.execute(insert, ("m1",))
    db.execute(insert, ("m2",))
    db.execute("DELETE FROM mutation_outbox WHERE mutation_id = 'm2'")
    db.execute(insert, ("m3",))

    assert [seq for (seq,) in db.execute("SELECT seq FROM mutation_outbox ORDER BY seq")] == [1, 3]


def test_a_mutation_id_is_queued_once(db):
    insert = (
        "INSERT INTO mutation_outbox (mutation_id, op, entity, entity_id, body, state)"
        " VALUES ('m1', 'delete', 'item', 'i', '{}', 'pending')"
    )
    db.execute(insert)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert)


def test_dropping_a_local_capture_drops_its_attachments(db):
    db.execute("INSERT INTO local_capture (id, seal, draft) VALUES ('c', 'open', '{}')")
    db.execute(
        "INSERT INTO local_attachment (id, capture_id, position, sha256, body) VALUES ('a', 'c', 0, 's', '{}')"
    )
    db.execute("DELETE FROM local_capture WHERE id = 'c'")

    assert db.execute("SELECT count(*) FROM local_attachment").fetchone() == (0,)
