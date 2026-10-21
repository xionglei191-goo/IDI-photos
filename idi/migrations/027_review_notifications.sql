CREATE TABLE review_notifications (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL REFERENCES cases(id),
    stage TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    review_id TEXT NOT NULL REFERENCES platform_review_batches(id),
    proposal_key TEXT NOT NULL,
    connector_id TEXT NOT NULL REFERENCES connectors(id),
    open_id TEXT NOT NULL,
    name TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','sending','sent','failed','unknown','cancelled')),
    remote_message_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX review_notifications_queue ON review_notifications(state,created_at);
CREATE INDEX review_notifications_case ON review_notifications(case_id,stage);
