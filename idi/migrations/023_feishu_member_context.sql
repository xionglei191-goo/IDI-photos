ALTER TABLE agent_context_summaries ADD COLUMN member_context_cursor INTEGER NOT NULL DEFAULT 0;

-- Preserve the boundary of summaries that were explicitly cleared before this update.
UPDATE agent_context_summaries SET member_context_cursor=COALESCE((
    SELECT MAX(i.rowid) FROM feishu_inbox i
    WHERE i.connector_id=agent_context_summaries.connector_id
      AND i.received_at<=agent_context_summaries.updated_at
),0)
WHERE channel='feishu' AND conversation_key='' AND cursor_seq>0 AND summary_json='{}';

CREATE INDEX feishu_inbox_member_context ON feishu_inbox(
    connector_id,state,json_extract(payload,'$.chat_id'),
    COALESCE(json_extract(payload,'$.sender.sender_id.open_id'),json_extract(payload,'$.sender.id')),
    CAST(json_extract(payload,'$.create_time') AS INTEGER)
);
