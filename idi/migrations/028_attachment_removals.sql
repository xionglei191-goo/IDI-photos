CREATE TABLE platform_attachment_removals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    stage TEXT NOT NULL,
    field_key TEXT NOT NULL,
    file_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'prepared',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX platform_attachment_removals_pending
    ON platform_attachment_removals(case_id, stage, state);

-- Content-based exclusions survive a repeated import or a new local photo ID.
CREATE TABLE platform_attachment_blocks (
    case_id TEXT NOT NULL REFERENCES cases(id),
    stage TEXT NOT NULL,
    field TEXT NOT NULL,
    blob_hash TEXT NOT NULL REFERENCES blobs(hash),
    created_at TEXT NOT NULL,
    PRIMARY KEY (case_id, stage, field, blob_hash)
);
