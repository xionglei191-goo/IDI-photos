CREATE TABLE agent_context_summaries (
    scope_key TEXT PRIMARY KEY,
    channel TEXT NOT NULL CHECK(channel IN ('workspace','feishu')),
    owner_key TEXT NOT NULL,
    thread_id TEXT REFERENCES workspace_chat_threads(id) ON DELETE CASCADE,
    connector_id TEXT REFERENCES connectors(id),
    binding_revision INTEGER,
    sender_id TEXT NOT NULL DEFAULT '',
    project_ids TEXT NOT NULL,
    cursor_seq INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX agent_context_owner ON agent_context_summaries(channel,owner_key);

CREATE TABLE agent_memories (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL CHECK(scope IN ('browser','member','project','case')),
    owner_key TEXT NOT NULL DEFAULT '',
    connector_id TEXT REFERENCES connectors(id),
    binding_revision INTEGER,
    project_ids TEXT NOT NULL DEFAULT '[]',
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('preference','note','todo','fact')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('candidate','active','stale','deleted')),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('manual','workspace','feishu','case_snapshot')),
    source_id TEXT NOT NULL DEFAULT '',
    source_quote TEXT NOT NULL DEFAULT '',
    source_time TEXT NOT NULL,
    source_signature TEXT NOT NULL DEFAULT '',
    origin_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX agent_memory_scope ON agent_memories(scope,owner_key,project_id,case_id,state);
CREATE INDEX agent_memory_source ON agent_memories(source_kind,source_id);
CREATE TABLE agent_memory_events (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
