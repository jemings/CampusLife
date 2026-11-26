"""Schema migrations (E3): numbered SQL files and PRAGMA user_version, no ORM."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files

FILE_NAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")


class DatabaseTooNew(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    sql: str


def _shipped() -> list[Migration]:
    found = []
    for entry in files(__package__).iterdir():
        match = FILE_NAME.fullmatch(entry.name)
        if match:
            found.append(Migration(int(match[1]), match[2], entry.read_text(encoding="utf-8")))
    return sorted(found, key=lambda migration: migration.number)


MIGRATIONS = _shipped()


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> None:
    """Bring the database to the last migration, one transaction per migration.

    `conn` runs in autocommit mode (isolation_level=None) so each script's own
    BEGIN and COMMIT decide the transaction.
    """
    current = _user_version(conn)
    if current > len(migrations):
        raise DatabaseTooNew(f"the database is at schema version {current}, this Hub knows up to {len(migrations)}; run a newer Hub")
    for migration in migrations[current:]:
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{migration.sql};\nPRAGMA user_version = {migration.number};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
