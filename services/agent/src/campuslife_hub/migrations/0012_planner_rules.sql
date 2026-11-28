-- The readings each planner last looked through the Hub's files with (#6, D26): a Hub that starts with a reading it
-- did not have, a reader of a type, plans it, once, for the files it already holds. readings is {job type: [the
-- media types it reads that refine can pick]}. Not synced. A restore brings back an older row, and the next start
-- looks through the files again, which queues only readings the Blobs lack.
CREATE TABLE planner_rules (
    planner TEXT PRIMARY KEY,
    readings TEXT NOT NULL CHECK (json_valid(readings))
);
