CREATE TABLE followup_contacts (
    project_id TEXT PRIMARY KEY REFERENCES projects(id), connector_id TEXT NOT NULL REFERENCES connectors(id),
    open_id TEXT NOT NULL, name TEXT NOT NULL, auto_feedback INTEGER NOT NULL DEFAULT 1,
    enabled_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE photo_quality_reviews (
    photo_id TEXT PRIMARY KEY REFERENCES photos(id), signature TEXT NOT NULL,
    state TEXT NOT NULL, metrics TEXT NOT NULL, findings TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    error TEXT, reviewed_at TEXT NOT NULL
);
CREATE TABLE followup_issues (
    id TEXT PRIMARY KEY, issue_key TEXT NOT NULL UNIQUE, project_id TEXT REFERENCES projects(id),
    case_id TEXT REFERENCES cases(id), photo_id TEXT REFERENCES photos(id), kind TEXT NOT NULL,
    title TEXT NOT NULL, detail TEXT NOT NULL, action TEXT NOT NULL, evidence TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved','dismissed')),
    signature TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, changed_at TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT '', feedback_generation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX followup_issues_project ON followup_issues(project_id,state);
CREATE TABLE followup_runs (
    id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'queued', signature TEXT NOT NULL DEFAULT '',
    result TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE followup_feedback (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL REFERENCES projects(id),
    connector_id TEXT NOT NULL REFERENCES connectors(id), open_id TEXT NOT NULL, name TEXT NOT NULL,
    chat_id TEXT NOT NULL, issue_versions TEXT NOT NULL, content TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','sending','sent','failed','unknown','cancelled')),
    remote_message_id TEXT, error TEXT, created_at TEXT NOT NULL, sent_at TEXT
);
INSERT INTO settings(key,value) VALUES('followup','{"auto_review":true,"ai_review":false,"template_id":"insurer-v2"}');
