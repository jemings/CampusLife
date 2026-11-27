-- The media type identify read from each Blob's bytes (#5, decision 7); not synced. An Attachment's detected_mime is
-- refined from it. It goes with its Blob's row, whatever removes that: a purge or the collection of unreferenced Blobs.
CREATE TABLE blob_media (
    sha256 TEXT PRIMARY KEY REFERENCES blobs (sha256) ON DELETE CASCADE,
    media_type TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    artifact_id TEXT NOT NULL
);
