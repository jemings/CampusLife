import sqlite3

from campuslife_hub.sqlite_probe import probe


class SqliteWithout:
    """A connection to the real SQLite whose statements mentioning `missing` fail, as on a build without it."""

    def __init__(self, missing: str, version: str | None = None, secure_delete: int | None = None):
        self._conn = sqlite3.connect(":memory:")
        self._missing = missing
        self._version = version
        self._secure_delete = secure_delete

    def execute(self, sql: str, *args):
        if self._version is not None and "sqlite_version()" in sql:
            return self._conn.execute("SELECT ?", (self._version,))
        if self._secure_delete is not None and sql == "PRAGMA secure_delete":
            return self._conn.execute("SELECT ?", (self._secure_delete,))
        if self._missing and self._missing in sql:
            raise sqlite3.OperationalError(f"no such module: {self._missing}")
        return self._conn.execute(sql, *args)


def test_this_sqlite_passes():
    assert probe(sqlite3.connect(":memory:")) == []


def test_a_sqlite_older_than_3_35_is_refused():
    problems = probe(SqliteWithout("", version="3.34.1"))
    assert len(problems) == 1
    assert "3.34.1" in problems[0] and "3.35" in problems[0]


def test_version_numbers_compare_as_numbers():
    assert probe(SqliteWithout("", version="3.100.0")) == []


def test_each_missing_feature_is_named():
    for missing, name in [("fts5", "fts5"), ("trigram", "trigram"), ("json_extract", "json_extract"), ("GENERATED", "generated column"), ("secure-delete", "fts5 secure-delete"), ("contentless_delete", "fts5 contentless_delete")]:
        problems = probe(SqliteWithout(missing))
        assert any(name in problem for problem in problems), (missing, problems)


def test_a_sqlite_that_will_not_turn_secure_delete_on_is_refused():
    problems = probe(SqliteWithout("", secure_delete=0))
    assert any("secure_delete" in problem for problem in problems)
