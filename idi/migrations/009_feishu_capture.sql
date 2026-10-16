-- A separate durable inbox keeps network delivery independent of attachment work.
CREATE TABLE feishu_state (
    connector_id TEXT PRIMARY KEY REFERENCES connectors(id),
    started_at INTEGER NOT NULL, cursor INTEGER NOT NULL,
    phase TEXT NOT NULL DEFAULT 'starting', connected_at TEXT,
    last_event_at TEXT, last_history_at TEXT, heartbeat_at TEXT,
    error TEXT, history_error TEXT
);
CREATE TABLE feishu_inbox (
    connector_id TEXT NOT NULL REFERENCES connectors(id),
    message_id TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','completed','failed','ignored')),
    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
    error TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY(connector_id,message_id)
);
CREATE INDEX feishu_inbox_pending ON feishu_inbox(connector_id,state,retry_at);
