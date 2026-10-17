CREATE TABLE agent_maintenance_actions (
    id TEXT PRIMARY KEY, action_key TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL CHECK(channel IN ('workspace','feishu')),
    task_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('refresh_case','retry_photo_analysis')),
    target_id TEXT NOT NULL, request_quote TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','completed','failed','interrupted')),
    result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(channel,task_id,kind)
);
CREATE INDEX agent_maintenance_target ON agent_maintenance_actions(kind,target_id,state);
