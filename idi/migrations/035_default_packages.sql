ALTER TABLE projects ADD COLUMN insurer_template_id TEXT NOT NULL DEFAULT 'insurer-v2';

CREATE TABLE case_default_packages (
    case_id TEXT PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    job_id TEXT REFERENCES jobs(id),
    package_id TEXT REFERENCES packages(id),
    state TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
