-- Processing state lives here and nowhere else (D1): a Capture or an Attachment has no status.
-- Not synced; clients see the processing_summary the Hub computes from these rows.
-- job_key (D4) hashes job_type, inputs, params and the pipeline columns.
CREATE TABLE processing_jobs (
    id TEXT PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    job_type TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    inputs TEXT NOT NULL CHECK (json_valid(inputs)),
    params TEXT NOT NULL CHECK (json_valid(params)),
    pipeline_version TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    prompt_version TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'retry_wait', 'succeeded', 'failed', 'blocked', 'cancelled')),
    outcome_code TEXT CHECK (outcome_code IN (
        'unsupported', 'password_required', 'corrupt', 'needs_conversion', 'partial', 'policy_blocked', 'budget_blocked', 'unsafe_input'
    )),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    next_attempt_at TEXT CHECK ((next_attempt_at IS NOT NULL) = (state IN ('pending', 'retry_wait'))),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX processing_jobs_due ON processing_jobs (next_attempt_at) WHERE next_attempt_at IS NOT NULL;
CREATE INDEX processing_jobs_by_subject ON processing_jobs (subject_ref);
CREATE INDEX processing_jobs_running ON processing_jobs (state) WHERE state = 'running';
