CREATE TABLE feishu_agent_bindings (
    connector_id TEXT PRIMARY KEY REFERENCES connectors(id),
    enabled INTEGER NOT NULL DEFAULT 0,
    chat_enabled INTEGER NOT NULL DEFAULT 1,
    images_enabled INTEGER NOT NULL DEFAULT 1,
    save_drafts INTEGER NOT NULL DEFAULT 1,
    daily_brief INTEGER NOT NULL DEFAULT 1,
    brief_time TEXT NOT NULL DEFAULT '18:00',
    bot_open_id TEXT NOT NULL DEFAULT '',
    project_id TEXT REFERENCES projects(id),
    project_ids TEXT NOT NULL DEFAULT '[]',
    chat_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    activated_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE feishu_agent_tasks (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    connector_id TEXT NOT NULL REFERENCES connectors(id),
    project_id TEXT REFERENCES projects(id),
    binding_revision INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('chat','images','brief')),
    source_message_id TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed','partial','failed','cancelled','missed')),
    input_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    usage TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX feishu_agent_tasks_queue ON feishu_agent_tasks(state,created_at);
CREATE TABLE feishu_agent_seen (
    connector_id TEXT NOT NULL REFERENCES connectors(id),
    message_id TEXT NOT NULL,
    disposition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(connector_id,message_id)
);
CREATE TABLE feishu_agent_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES feishu_agent_tasks(id),
    step INTEGER NOT NULL,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE feishu_agent_images (
    task_id TEXT NOT NULL REFERENCES feishu_agent_tasks(id),
    photo_id TEXT NOT NULL REFERENCES photos(id),
    blob_hash TEXT NOT NULL,
    photo_revision INTEGER NOT NULL,
    case_id TEXT REFERENCES cases(id),
    summary TEXT NOT NULL,
    suggestion TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('organized','needs_check')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_id,photo_id)
);
CREATE TABLE feishu_agent_drafts (
    task_id TEXT NOT NULL REFERENCES feishu_agent_tasks(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    stage TEXT NOT NULL,
    review_id TEXT REFERENCES platform_review_batches(id),
    result TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id,case_id)
);
CREATE TABLE feishu_agent_outbox (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES feishu_agent_tasks(id),
    recipient_id TEXT NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','sending','sent','failed','unknown','cancelled')),
    remote_message_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX feishu_agent_outbox_queue ON feishu_agent_outbox(state,created_at);
