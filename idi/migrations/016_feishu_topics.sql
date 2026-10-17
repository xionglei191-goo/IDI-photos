ALTER TABLE feishu_agent_tasks ADD COLUMN conversation_key TEXT NOT NULL DEFAULT '';
CREATE INDEX feishu_agent_conversation ON feishu_agent_tasks(connector_id,binding_revision,conversation_key,source_message_id);
ALTER TABLE agent_context_summaries ADD COLUMN conversation_key TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_context_summaries ADD COLUMN chat_id TEXT NOT NULL DEFAULT '';

CREATE TABLE feishu_topic_aliases (
    connector_id TEXT NOT NULL REFERENCES connectors(id),
    chat_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    reference TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(connector_id,chat_id,binding_revision,reference)
);
CREATE INDEX feishu_topic_alias_key ON feishu_topic_aliases(connector_id,chat_id,binding_revision,topic_key);

CREATE TABLE feishu_topic_messages (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL REFERENCES connectors(id),
    chat_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    topic_key TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(connector_id,chat_id,binding_revision,source_message_id)
);
CREATE INDEX feishu_topic_message_scope ON feishu_topic_messages(connector_id,chat_id,binding_revision,topic_key);
