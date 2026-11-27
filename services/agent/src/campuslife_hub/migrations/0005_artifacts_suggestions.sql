-- What jobs made (D9, E14), never changed once written. One per job and type, so a job run
-- again after a crash before it finished finds the Artifact its first run recorded.
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data)),
    job_key TEXT GENERATED ALWAYS AS (json_extract(data, '$.job_key')) VIRTUAL,
    artifact_type TEXT GENERATED ALWAYS AS (json_extract(data, '$.artifact_type')) VIRTUAL,
    content_blob TEXT GENERATED ALWAYS AS (json_extract(data, '$.content_blob')) VIRTUAL
);
CREATE UNIQUE INDEX artifacts_by_job ON artifacts (job_key, artifact_type);
CREATE INDEX artifacts_by_blob ON artifacts (content_blob);

-- Proposals made from Artifacts (D10). The user decides state with suggestion.accept or
-- suggestion.dismiss; a decided fingerprint is not proposed again.
CREATE TABLE suggestions (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data)),
    source_artifact_id TEXT GENERATED ALWAYS AS (json_extract(data, '$.source_artifact_id')) VIRTUAL,
    fingerprint TEXT GENERATED ALWAYS AS (json_extract(data, '$.fingerprint')) VIRTUAL,
    state TEXT GENERATED ALWAYS AS (json_extract(data, '$.state')) VIRTUAL
);
CREATE INDEX suggestions_by_fingerprint ON suggestions (fingerprint, state);
CREATE INDEX suggestions_by_artifact ON suggestions (source_artifact_id);

-- A Relation that ends at a Blob keeps it from collection, as an Attachment does (D2).
ALTER TABLE relations ADD COLUMN from_blob TEXT GENERATED ALWAYS AS (
    CASE WHEN substr(json_extract(data, '$.from'), 1, 5) = 'blob:' THEN substr(json_extract(data, '$.from'), 6) END
) VIRTUAL;
ALTER TABLE relations ADD COLUMN to_blob TEXT GENERATED ALWAYS AS (
    CASE WHEN substr(json_extract(data, '$.to'), 1, 5) = 'blob:' THEN substr(json_extract(data, '$.to'), 6) END
) VIRTUAL;
CREATE INDEX relations_by_from_blob ON relations (from_blob);
CREATE INDEX relations_by_to_blob ON relations (to_blob);
