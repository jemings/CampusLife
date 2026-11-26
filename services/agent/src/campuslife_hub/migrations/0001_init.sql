-- Hub identity and the global change counter (D3). One row, written by init.
CREATE TABLE hub_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    hub_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    change_seq INTEGER NOT NULL DEFAULT 0
);

-- Every synced row's place in the changes feed (E5). Only store.write touches it.
-- A tombstone (deleted = 1) outlives its row, so a cursor never expires.
CREATE TABLE sync_index (
    entity TEXT NOT NULL,
    id TEXT NOT NULL,
    rev INTEGER NOT NULL UNIQUE,
    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
    PRIMARY KEY (entity, id)
);

-- Synced rows: data is the row as the entity's contract schema describes it.
CREATE TABLE captures (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data))
);

CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data)),
    capture_id TEXT GENERATED ALWAYS AS (json_extract(data, '$.capture_id')) VIRTUAL,
    blob_sha256 TEXT GENERATED ALWAYS AS (json_extract(data, '$.blob_sha256')) VIRTUAL
);
CREATE INDEX attachments_by_capture ON attachments (capture_id);
CREATE INDEX attachments_by_blob ON attachments (blob_sha256);

CREATE TABLE relations (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data))
);

CREATE TABLE items (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data))
);

-- Stored Blobs (D2). A row exists only after its file reached blobs/.
CREATE TABLE blobs (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL CHECK (size >= 0),
    stored_at TEXT NOT NULL
);

CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('android', 'ipados', 'windows_hub')),
    device_name TEXT NOT NULL,
    app_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'dormant', 'revoked')),
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);
CREATE UNIQUE INDEX one_windows_hub ON devices (platform) WHERE platform = 'windows_hub';

-- First result per mutation_id, for duplicate answers (D18). retry is never recorded (E6).
CREATE TABLE applied_mutations (
    mutation_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices (id),
    result TEXT NOT NULL CHECK (json_valid(result)),
    applied_at TEXT NOT NULL
);

-- canonical_json sha256 of the payload that first created an entity id, for id_reuse (E7).
CREATE TABLE created_content (
    entity TEXT NOT NULL,
    id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (entity, id)
);
