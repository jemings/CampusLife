-- Reference shape of a client's local sync store (D17, #3 decision 17). The Android Room database
-- creates exactly these tables, columns, keys and indexes, and its tests compare the two; the iPad
-- store (#16) follows the same file.
--
-- Room declares column types, NOT NULL, primary keys, foreign keys and plain indexes only, so the
-- file stays inside that: no CHECK, STRICT, WITHOUT ROWID, partial index, trigger or view. The
-- allowed values of the state columns are listed here because no constraint can hold them.
-- Times are epoch milliseconds (*_ms); JSON columns hold the text exactly as received or made.

-- Where this device stands against its Hub, one row with id 0: epoch is the base epoch, set from
-- the first GET /v1/hub and replaced only by a 409 epoch_changed; cursor is the next since.
CREATE TABLE meta (
    id INTEGER NOT NULL PRIMARY KEY,
    hub_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    cursor INTEGER NOT NULL
);

-- The Hub's last state of every changes-feed row of a known entity; the Hub owns it and only the
-- feed writes it. data is the row JSON, rev the row's change_seq.
CREATE TABLE entity_cache (
    entity TEXT NOT NULL,
    id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (entity, id)
);

-- Every Mutation this device made, in the order it made them; a seq is never reused. body is the
-- mutation/mutation JSON, sent unchanged on every resend. entity and entity_id name the row it
-- writes (capture for capture.commit, suggestion for suggestion.*).
-- state: held (a capture.commit whose Blobs are not all stored), pending (to send), acked (applied,
-- waiting for a pull that began after the ACK), pulling (applied, such a pull is running), settled
-- (the result is in entity_cache, or a conflict lost), rejected (needs attention).
-- result is the mutation/result that ended the sending; acked_at_ms starts the retention of a sent
-- Mutation (decision 7).
CREATE TABLE mutation_outbox (
    seq INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    mutation_id TEXT NOT NULL COLLATE NOCASE,
    op TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    acked_at_ms INTEGER
);
CREATE UNIQUE INDEX index_mutation_outbox_mutation_id ON mutation_outbox (mutation_id);
CREATE INDEX index_mutation_outbox_entity_entity_id ON mutation_outbox (entity, entity_id DESC);

-- One row per Blob file this device keeps, by sha256; the row goes when the local file goes.
-- state: waiting (to upload), uploading (a session is open), retry_wait (failed for now), stored.
CREATE TABLE blob_transfer (
    sha256 TEXT NOT NULL PRIMARY KEY,
    size INTEGER NOT NULL,
    state TEXT NOT NULL
);

-- Captures made on this device (D1: the client owns the seal state). seal: open, sealed.
-- draft is the capture/commit JSON without its attachments, which local_attachment holds in order.
CREATE TABLE local_capture (
    id TEXT NOT NULL PRIMARY KEY,
    seal TEXT NOT NULL,
    draft TEXT NOT NULL
);

-- body is the capture/commit_attachment JSON; sha256 repeats its blob_sha256 for joins.
CREATE TABLE local_attachment (
    id TEXT NOT NULL PRIMARY KEY,
    capture_id TEXT NOT NULL REFERENCES local_capture (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE UNIQUE INDEX index_local_attachment_capture_id_position ON local_attachment (capture_id, position);
CREATE INDEX index_local_attachment_sha256 ON local_attachment (sha256);
