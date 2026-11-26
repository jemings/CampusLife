-- Open upload sessions (D7, E19), one per (Device, sha256). The chunks received
-- are the files uploads/<id>/<index>; every session gets a fresh id, so a new
-- session never sees a dead one's chunks, and a directory with no row is an orphan.
CREATE TABLE upload_sessions (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices (id),
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    chunk_size INTEGER NOT NULL CHECK (chunk_size >= 1),
    touched_at TEXT NOT NULL,
    UNIQUE (device_id, sha256)
);
CREATE INDEX upload_sessions_by_touch ON upload_sessions (touched_at);

-- blobs.stored_at is when the Blob's bytes last reached the store; collection counts from it (D2).
CREATE INDEX blobs_by_stored_at ON blobs (stored_at);
