CREATE TABLE tags (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data))
);

-- Values a patch submitted that the Hub did not write and must not lose (E8): an x-text
-- column that lost compare-and-set, or any column of an entity that was not live (D18).
-- Not synced; #18 turns them into ConflictRecords.
CREATE TABLE conflict_stash (
    id TEXT PRIMARY KEY,
    mutation_id TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices (id),
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field TEXT NOT NULL,
    change TEXT NOT NULL CHECK (json_valid(change)),
    theirs_rev INTEGER,
    stashed_at TEXT NOT NULL
);

CREATE INDEX applied_mutations_by_age ON applied_mutations (applied_at);
