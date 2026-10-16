ALTER TABLE projects ADD COLUMN short_prefix TEXT NOT NULL DEFAULT '';
ALTER TABLE cases ADD COLUMN platform_report_code TEXT;
ALTER TABLE cases ADD COLUMN policy_number TEXT NOT NULL DEFAULT '';
ALTER TABLE cases ADD COLUMN short_code TEXT;
ALTER TABLE cases ADD COLUMN workflow_stage TEXT NOT NULL DEFAULT 'pending_intake'
  CHECK(workflow_stage IN ('pending_intake','insurer_review','survey','repair','acceptance','settlement','closed','direct_payment','noncoverage'));
ALTER TABLE cases ADD COLUMN workflow_source TEXT NOT NULL DEFAULT 'local';
ALTER TABLE cases ADD COLUMN workflow_verified_at TEXT;
ALTER TABLE cases ADD COLUMN responsible TEXT NOT NULL DEFAULT '';
ALTER TABLE cases ADD COLUMN insurer_template_id TEXT NOT NULL DEFAULT 'insurer-v2';
ALTER TABLE cases ADD COLUMN remote_status TEXT NOT NULL DEFAULT '';
ALTER TABLE cases ADD COLUMN remote_updated_at TEXT;
DROP INDEX cases_report_unique;
CREATE UNIQUE INDEX cases_platform_report_unique ON cases(platform_report_code)
  WHERE platform_report_code IS NOT NULL AND platform_report_code<>'';
CREATE UNIQUE INDEX cases_short_code_unique ON cases(short_code)
  WHERE short_code IS NOT NULL AND short_code<>'';
CREATE UNIQUE INDEX projects_short_prefix_unique ON projects(short_prefix) WHERE short_prefix<>'';
CREATE INDEX cases_workflow_idx ON cases(workflow_stage,project_id);
UPDATE cases SET workflow_stage=CASE status WHEN 'survey' THEN 'survey' WHEN 'working' THEN 'repair'
  WHEN 'completed' THEN 'acceptance' WHEN 'accepted' THEN 'settlement' WHEN 'closed' THEN 'closed' ELSE 'pending_intake' END;
UPDATE cases SET platform_report_code=(SELECT NULLIF(platform_report_code,'') FROM platform_links WHERE platform_links.case_id=cases.id);
ALTER TABLE photos ADD COLUMN photo_group TEXT NOT NULL DEFAULT 'unknown'
  CHECK(photo_group IN ('unknown','survey_checkin','survey_problem','work_checkin','construction','completion'));
UPDATE photos SET photo_group=CASE
  WHEN stage='survey' AND role IN ('building_checkin','door_checkin') THEN 'survey_checkin'
  WHEN stage='survey' AND role='problem' THEN 'survey_problem'
  WHEN stage='work' AND role IN ('building_checkin','door_checkin') THEN 'work_checkin'
  WHEN stage='work' AND role='construction' THEN 'construction'
  WHEN role='finished' THEN 'completion' ELSE 'unknown' END;
CREATE INDEX photos_group_idx ON photos(case_id,photo_group);
CREATE TABLE case_events (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), kind TEXT NOT NULL,
  from_stage TEXT, to_stage TEXT, source TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
  event_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX case_events_case_idx ON case_events(case_id,created_at);
CREATE TABLE platform_sync_runs (
  id TEXT PRIMARY KEY, state TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL, finished_at TEXT, total INTEGER NOT NULL DEFAULT 0,
  created_count INTEGER NOT NULL DEFAULT 0, updated_count INTEGER NOT NULL DEFAULT 0,
  error TEXT, details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE platform_review_batches (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), stage TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'prepared', fingerprint TEXT NOT NULL,
  snapshot TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, confirmed_at TEXT, finished_at TEXT
);
CREATE INDEX platform_reviews_case_idx ON platform_review_batches(case_id,created_at);
