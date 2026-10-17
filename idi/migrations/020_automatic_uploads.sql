ALTER TABLE platform_review_batches ADD COLUMN save_only INTEGER NOT NULL DEFAULT 0;

CREATE TABLE automatic_upload_cases (
    case_id TEXT PRIMARY KEY REFERENCES cases(id), enabled INTEGER NOT NULL DEFAULT 1,
    stage TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'queued',
    signature TEXT NOT NULL DEFAULT '', review_guard TEXT NOT NULL DEFAULT '',
    save_review_id TEXT REFERENCES platform_review_batches(id),
    review_id TEXT REFERENCES platform_review_batches(id),
    detail TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX automatic_upload_queue ON automatic_upload_cases(enabled,retry_at,updated_at);
INSERT OR IGNORE INTO settings(key,value) VALUES('automatic_upload_policy',
    json_object('enabled',json('false'),'activated_at',''));
