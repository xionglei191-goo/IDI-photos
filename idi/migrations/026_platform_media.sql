-- Local originals for official attachments are independent of upload selections.
CREATE TABLE platform_media (
    case_id TEXT NOT NULL REFERENCES cases(id),
    report_receiving_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    blob_hash TEXT NOT NULL REFERENCES blobs(hash),
    original_name TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (case_id, report_receiving_id, file_id, source_url)
);
