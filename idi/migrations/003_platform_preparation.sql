CREATE TABLE platform_links (
    case_id TEXT PRIMARY KEY REFERENCES cases(id),
    report_receiving_id TEXT NOT NULL DEFAULT '',
    platform_report_code TEXT NOT NULL DEFAULT '',
    insurer_report_number TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL DEFAULT '{}',
    verified_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX platform_link_unique_report ON platform_links(report_receiving_id)
    WHERE report_receiving_id <> '';

CREATE TABLE platform_drafts (
    case_id TEXT NOT NULL REFERENCES cases(id),
    stage TEXT NOT NULL CHECK(stage IN ('intake','survey','repair','acceptance')),
    values_json TEXT NOT NULL DEFAULT '{}',
    files_json TEXT NOT NULL DEFAULT '{}',
    target_report_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (case_id,stage)
);

CREATE TABLE platform_packages (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    stage TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    zip_path TEXT NOT NULL,
    zip_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER tree_platform_packages_insert AFTER INSERT ON platform_packages
BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
