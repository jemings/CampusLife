-- Backup generations this Hub finished (E20, U3). The manifest on the backup drive is what a
-- restore reads; these rows are what the overdue warning reads while the drive is away. Not synced.
CREATE TABLE backups (
    generation TEXT NOT NULL,
    target TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    blob_count INTEGER NOT NULL CHECK (blob_count >= 0),
    copied_count INTEGER NOT NULL CHECK (copied_count >= 0),
    PRIMARY KEY (target, generation)
);
CREATE INDEX backups_by_time ON backups (finished_at);
