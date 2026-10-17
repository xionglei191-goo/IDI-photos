CREATE TABLE workspace_chat_threads (
    id TEXT PRIMARY KEY,
    owner_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    project_ids TEXT NOT NULL,
    case_id TEXT REFERENCES cases(id) ON DELETE SET NULL,
    context_label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX workspace_chat_threads_owner ON workspace_chat_threads(owner_hash,updated_at);
CREATE TABLE workspace_chat_turns (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES workspace_chat_threads(id) ON DELETE CASCADE,
    owner_hash TEXT NOT NULL,
    request_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed','failed','cancelled')),
    progress TEXT NOT NULL DEFAULT '等待处理',
    steps TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    usage TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(owner_hash,request_id)
);
CREATE INDEX workspace_chat_turns_queue ON workspace_chat_turns(state,created_at);
CREATE INDEX workspace_chat_turns_thread ON workspace_chat_turns(thread_id,created_at);
