"""What the Hub needs from the SQLite it runs on (D16, E2).

RETURNING sets the version floor; the rest are probed by trying them, because a
build's version does not say which optional modules it was compiled with.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

MIN_VERSION = (3, 35, 0)

FEATURE_PROBES = {
    "fts5": "CREATE VIRTUAL TABLE temp.probe_fts5 USING fts5(body)",
    "fts5 trigram tokenizer": "CREATE VIRTUAL TABLE temp.probe_trigram USING fts5(body, tokenize='trigram')",
    "json_extract": "SELECT json_extract('{\"a\": 1}', '$.a')",
    "generated column": "CREATE TEMP TABLE probe_generated (a INTEGER, b INTEGER GENERATED ALWAYS AS (a + 1))",
}


class Executes(Protocol):
    def execute(self, sql: str, /) -> sqlite3.Cursor: ...


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def secure_delete_problems(conn: Executes) -> list[str]:
    """Turn secure_delete on for conn and say so when it did not take: a deleted row's bytes would stay in the file (U2)."""
    conn.execute("PRAGMA secure_delete = ON")
    (setting,) = conn.execute("PRAGMA secure_delete").fetchone()
    if setting != 1:
        return [f"SQLite did not turn secure_delete on (it reads {setting}); a purge would leave deleted text in the database file"]
    return []


def probe(conn: Executes) -> list[str]:
    """Why this SQLite cannot run the Hub; empty when it can."""
    problems = secure_delete_problems(conn)
    (version,) = conn.execute("SELECT sqlite_version()").fetchone()
    if _version(version) < MIN_VERSION:
        floor = ".".join(map(str, MIN_VERSION))
        problems.append(f"SQLite {version} is older than {floor} (RETURNING)")
    for feature, sql in FEATURE_PROBES.items():
        try:
            conn.execute(sql)
        except sqlite3.Error as error:
            problems.append(f"SQLite lacks {feature}: {error}")
    return problems
