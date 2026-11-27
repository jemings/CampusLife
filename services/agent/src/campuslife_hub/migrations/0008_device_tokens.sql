-- Device tokens, pairing secrets and the audit log (#19, D3, D6). None of them is synced.

-- A Device's bearer tokens, kept only as the SHA-256 of the token. A Device has one current token and, while a
-- reissue waits for the new token's first use, one pending token. A revoked Device keeps its rows, so its tokens
-- answer device_revoked rather than unauthorized.
CREATE TABLE device_tokens (
    token_sha256 TEXT PRIMARY KEY CHECK (length(token_sha256) = 64 AND token_sha256 NOT GLOB '*[^0-9a-f]*'),
    device_id TEXT NOT NULL REFERENCES devices (id),
    status TEXT NOT NULL CHECK (status IN ('current', 'pending')),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_current_token ON device_tokens (device_id) WHERE status = 'current';
CREATE UNIQUE INDEX one_pending_token ON device_tokens (device_id) WHERE status = 'pending';

-- Pairing secrets the admin screen offered, kept only as the SHA-256 of the secret. A used secret keeps its row and
-- the Device it paired, so reusing it is caught after a restart too. At most one offer is open: a new one replaces it.
CREATE TABLE pairing_secrets (
    secret_sha256 TEXT PRIMARY KEY CHECK (length(secret_sha256) = 64 AND secret_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    device_id TEXT REFERENCES devices (id),
    used_at TEXT,
    CHECK ((device_id IS NULL) = (used_at IS NULL))
);
CREATE UNIQUE INDEX one_open_offer ON pairing_secrets ((used_at IS NULL)) WHERE used_at IS NULL;

-- When a Device was last refused, and its last diagnostic heartbeat (device/status) with when it came.
ALTER TABLE devices ADD COLUMN last_rejected_at TEXT;
ALTER TABLE devices ADD COLUMN last_status TEXT CHECK (last_status IS NULL OR json_valid(last_status));
ALTER TABLE devices ADD COLUMN last_status_at TEXT CHECK ((last_status IS NULL) = (last_status_at IS NULL));

-- What happened that the Hub answers for (D3): pairing, revokes, approvals, dormancy and token reissues for now.
-- Append-only; it holds no token or secret.
CREATE TABLE audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    subject TEXT,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND json_type(details_json) = 'object')
);
CREATE TRIGGER audit_log_keeps_its_rows BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER audit_log_loses_no_rows BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
