CREATE TABLE feishu_identity_bindings (
    id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    open_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES idi_users(id),
    connector_id TEXT NOT NULL REFERENCES connectors(id),
    name TEXT NOT NULL,
    revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(app_id,open_id),
    UNIQUE(app_id,user_id)
);

ALTER TABLE review_notifications ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text';
ALTER TABLE review_notifications ADD COLUMN app_id TEXT NOT NULL DEFAULT '';
ALTER TABLE review_notifications ADD COLUMN card_token TEXT NOT NULL DEFAULT '';
ALTER TABLE review_notifications ADD COLUMN card_view TEXT NOT NULL DEFAULT '{}';
ALTER TABLE review_notifications ADD COLUMN card_dirty INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review_notifications ADD COLUMN card_error TEXT NOT NULL DEFAULT '';
ALTER TABLE review_notifications ADD COLUMN card_retry_at REAL NOT NULL DEFAULT 0;
ALTER TABLE review_notifications ADD COLUMN card_checked_at REAL NOT NULL DEFAULT 0;
ALTER TABLE review_notifications ADD COLUMN pending_action_id TEXT;

CREATE TABLE feishu_review_actions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    notification_id TEXT NOT NULL REFERENCES review_notifications(id),
    card_token TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES idi_users(id),
    open_id TEXT NOT NULL,
    binding_revision TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('edit','preview','submit','photos','refresh','readback')),
    payload TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','running','done','rejected','interrupted')),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(app_id,event_id)
);
CREATE INDEX feishu_review_actions_queue ON feishu_review_actions(state,created_at);
CREATE TABLE feishu_card_images (
    app_id TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    image_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(app_id,blob_hash)
);
