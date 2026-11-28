-- The readers each planner last looked through the Hub's files with (#6, D28): a Hub that starts with readers it did
-- not have then plans them, once, for the files it already holds. Not synced. A restore brings back an older row, and
-- the next start looks through the files again, which queues only what is still missing.
CREATE TABLE planner_rules (
    planner TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
);
