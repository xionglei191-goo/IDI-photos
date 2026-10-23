CREATE TABLE picc_record_drafts (
    case_id TEXT PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
    fields TEXT NOT NULL DEFAULT '{}',
    photo_overrides TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO templates(id,name,insurer,requirements,created_at,updated_at)
VALUES('picc-record-v1','人保查勘及施工记录表','中国人保','[]',
       strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'));
