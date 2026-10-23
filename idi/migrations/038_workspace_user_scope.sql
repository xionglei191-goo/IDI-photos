ALTER TABLE workspace_chat_threads ADD COLUMN user_id TEXT REFERENCES idi_users(id);
CREATE INDEX workspace_chat_threads_user ON workspace_chat_threads(user_id);
ALTER TABLE case_agent_runs ADD COLUMN user_id TEXT REFERENCES idi_users(id);
