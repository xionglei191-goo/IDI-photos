CREATE TABLE platform_draft_field_sources (
    case_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    field TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('automatic','manual')),
    value TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    photo_ids TEXT NOT NULL DEFAULT '[]',
    needs_review INTEGER NOT NULL DEFAULT 1 CHECK(needs_review IN (0,1)),
    input_signature TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(case_id,stage,field),
    FOREIGN KEY(case_id,stage) REFERENCES platform_drafts(case_id,stage)
);
