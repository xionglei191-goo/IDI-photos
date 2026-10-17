CREATE TABLE model_health (
    config_key TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'unverified',
    failures INTEGER NOT NULL DEFAULT 0, failure_window REAL NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0, server_retry_at REAL NOT NULL DEFAULT 0, cooldown INTEGER NOT NULL DEFAULT 0,
    probe_until REAL NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0,
    last_attempt REAL, last_transport_ok REAL, last_response_ok REAL,
    error TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE photo_rule_reviews (
    photo_id TEXT PRIMARY KEY REFERENCES photos(id), signature TEXT NOT NULL,
    state TEXT NOT NULL, metrics TEXT NOT NULL DEFAULT '{}', findings TEXT NOT NULL DEFAULT '[]',
    error TEXT, reviewed_at TEXT NOT NULL
);
CREATE TABLE photo_ai_tasks (
    id TEXT PRIMARY KEY, task_key TEXT NOT NULL UNIQUE, photo_id TEXT NOT NULL REFERENCES photos(id),
    kind TEXT NOT NULL CHECK(kind IN ('quality','classification')),
    source TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL, config_key TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0, deferred INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX photo_ai_tasks_queue ON photo_ai_tasks(state,retry_at,created_at);
CREATE INDEX photo_ai_tasks_photo ON photo_ai_tasks(photo_id,kind,created_at);
CREATE TABLE followup_run_requests (
    run_id TEXT PRIMARY KEY REFERENCES followup_runs(id), automatic INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE photo_pipeline (
    photo_id TEXT PRIMARY KEY REFERENCES photos(id), dirty INTEGER NOT NULL DEFAULT 1,
    signature TEXT NOT NULL DEFAULT '', official_code TEXT NOT NULL DEFAULT '', short_code TEXT NOT NULL DEFAULT '',
    identity TEXT NOT NULL DEFAULT 'unverified', state TEXT NOT NULL DEFAULT 'queued',
    reason TEXT NOT NULL DEFAULT '', draft_json TEXT NOT NULL DEFAULT '{}',
    photo_revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE INDEX photo_pipeline_dirty ON photo_pipeline(dirty,updated_at);
CREATE INDEX photo_pipeline_official ON photo_pipeline(official_code);
CREATE INDEX photo_pipeline_short ON photo_pipeline(short_code);
CREATE TABLE platform_draft_exclusions (
    case_id TEXT NOT NULL REFERENCES cases(id), stage TEXT NOT NULL, field TEXT NOT NULL,
    photo_id TEXT NOT NULL REFERENCES photos(id), created_at TEXT NOT NULL,
    PRIMARY KEY(case_id,stage,field,photo_id)
);
CREATE TABLE rule_draft_intents (
    id TEXT PRIMARY KEY, intent_key TEXT NOT NULL UNIQUE, task_id TEXT REFERENCES feishu_agent_tasks(id),
    case_id TEXT NOT NULL REFERENCES cases(id), stage TEXT NOT NULL,
    photo_ids TEXT NOT NULL, signature TEXT NOT NULL, case_signature TEXT NOT NULL, review_id TEXT REFERENCES platform_review_batches(id),
    state TEXT NOT NULL DEFAULT 'queued', result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX rule_draft_intents_queue ON rule_draft_intents(state,created_at);
ALTER TABLE feishu_agent_bindings ADD COLUMN ai_enabled INTEGER NOT NULL DEFAULT 1;
INSERT OR IGNORE INTO settings(key,value) VALUES('workflow_policy',json_object(
    'rules_enabled',json('true'),'auto_drafts',json('true'),
    'activated_at',strftime('%Y-%m-%dT%H:%M:%f+00:00','now')));
UPDATE settings SET value=json_set(value,'$.activated_at',strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
WHERE key='workflow_policy' AND json_extract(value,'$.activated_at') IS NULL;

-- Index existing OCR evidence without reprocessing or reporting historical files.
INSERT INTO photo_pipeline(photo_id,dirty,official_code,short_code,state,photo_revision,updated_at)
SELECT p.id,0,COALESCE(json_extract(r.suggestions,'$.fields.platform_report_code'),''),
    COALESCE(json_extract(r.suggestions,'$.fields.short_code'),''),'historical',p.revision,p.updated_at
FROM photos p LEFT JOIN recognitions r ON r.rowid=(SELECT r2.rowid FROM recognitions r2
    WHERE r2.photo_id=p.id AND r2.provider='local_ocr' AND r2.state='completed'
    ORDER BY r2.created_at DESC,r2.rowid DESC LIMIT 1);

CREATE TRIGGER resilient_photo_created AFTER INSERT ON photos BEGIN
    INSERT INTO photo_pipeline(photo_id,updated_at) VALUES(NEW.id,NEW.updated_at)
    ON CONFLICT(photo_id) DO UPDATE SET dirty=1,updated_at=excluded.updated_at;
END;
CREATE TRIGGER resilient_photo_changed AFTER UPDATE OF case_id,project_id,stage,role,photo_group,part,process,watermark_time,recognition_state,confirmed_fields ON photos BEGIN
    INSERT INTO photo_pipeline(photo_id,updated_at) VALUES(NEW.id,NEW.updated_at)
    ON CONFLICT(photo_id) DO UPDATE SET dirty=1,updated_at=excluded.updated_at;
END;
CREATE TRIGGER resilient_source_added AFTER INSERT ON photo_sources BEGIN
    UPDATE photo_pipeline SET dirty=1,updated_at=NEW.received_at WHERE photo_id=NEW.photo_id;
END;
CREATE TRIGGER resilient_case_added AFTER INSERT ON cases BEGIN
    UPDATE photo_pipeline SET dirty=1 WHERE photo_id IN (SELECT id FROM photos WHERE case_id IS NULL)
      AND (official_code=NEW.platform_report_code AND official_code<>'' OR short_code=NEW.short_code AND short_code<>'');
END;
CREATE TRIGGER resilient_case_changed AFTER UPDATE OF platform_report_code,short_code,workflow_stage ON cases
WHEN NEW.platform_report_code IS NOT OLD.platform_report_code OR NEW.short_code IS NOT OLD.short_code OR NEW.workflow_stage IS NOT OLD.workflow_stage BEGIN
    UPDATE photo_pipeline SET dirty=1 WHERE
      (photo_id IN (SELECT id FROM photos WHERE case_id IS NULL)
        AND (official_code=NEW.platform_report_code AND official_code<>'' OR short_code=NEW.short_code AND short_code<>''))
      OR (state<>'historical' AND photo_id IN (SELECT id FROM photos WHERE case_id=NEW.id));
END;
CREATE TRIGGER resilient_alias_added AFTER INSERT ON case_code_aliases BEGIN
    UPDATE photo_pipeline SET dirty=1 WHERE short_code=NEW.code AND short_code<>''
      AND photo_id IN (SELECT id FROM photos WHERE case_id IS NULL);
END;
CREATE TRIGGER resilient_ocr_completed AFTER INSERT ON recognitions WHEN NEW.provider='local_ocr' AND NEW.state='completed' BEGIN
    UPDATE photo_pipeline SET dirty=1 WHERE photo_id=NEW.photo_id;
END;
