-- A Blob row that goes (a purge, the collection of unreferenced Blobs, the copy a backup prunes) releases the job_key
-- of every finished job that read it, in the same transaction (#5). The job row stays as history under the key
-- 'released:<id>', and the same bytes stored again are planned and processed again instead of being answered by a
-- job whose results went with them. Pending and running jobs keep their key: what a purge cancels is finished by then,
-- and a job it leaves running (capture.ingest) is not touched.
CREATE TRIGGER blob_gone_releases_its_jobs AFTER DELETE ON blobs
BEGIN
    UPDATE processing_jobs SET job_key = 'released:' || id
    WHERE state IN ('succeeded', 'failed', 'blocked', 'cancelled')
      AND EXISTS (SELECT 1 FROM json_each(processing_jobs.inputs) WHERE value = OLD.sha256);
END;
