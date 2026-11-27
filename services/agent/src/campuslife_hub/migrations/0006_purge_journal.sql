-- Purges (U2, E16). The rows a purge removes are gone when the transaction that writes its
-- journal row commits; the Blob files and what the purge hooks keep outside this database are
-- removed after that, and again at every start until finished_at is set. Not synced.
CREATE TABLE purge_journal (
    id TEXT PRIMARY KEY,
    target TEXT NOT NULL CHECK (json_valid(target)),
    registered_at TEXT NOT NULL,
    hooks_done TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(hooks_done)),
    finished_at TEXT
);
CREATE INDEX purge_journal_unfinished ON purge_journal (registered_at) WHERE finished_at IS NULL;
