ALTER TABLE platform_sync_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'full';
ALTER TABLE platform_sync_runs ADD COLUMN checkpoint TEXT NOT NULL DEFAULT '{}';
ALTER TABLE platform_sync_runs ADD COLUMN retry_at TEXT;
ALTER TABLE platform_sync_runs ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0;
UPDATE platform_sync_runs SET state='failed',error='旧版同步中断，请重新发起完整对账' WHERE state='running';
CREATE INDEX platform_sync_pending_idx ON platform_sync_runs(state,mode,retry_at);
CREATE TABLE platform_sync_rows (
  run_id TEXT NOT NULL REFERENCES platform_sync_runs(id) ON DELETE CASCADE,
  scope TEXT NOT NULL, report_receiving_id TEXT NOT NULL, position INTEGER NOT NULL,
  snapshot TEXT NOT NULL, PRIMARY KEY(run_id,scope,report_receiving_id)
);
CREATE TABLE platform_sync_items (
  run_id TEXT NOT NULL REFERENCES platform_sync_runs(id) ON DELETE CASCADE,
  report_receiving_id TEXT NOT NULL, scope TEXT NOT NULL, snapshot TEXT NOT NULL,
  action TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', case_id TEXT, error TEXT,
  PRIMARY KEY(run_id,report_receiving_id)
);
CREATE TABLE platform_sync_usage (
  account TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
  photo_requests INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account,day)
);
CREATE TABLE platform_repair_activity (
  report_receiving_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
  active INTEGER NOT NULL CHECK(active IN (0,1)), checked_at TEXT NOT NULL,
  source TEXT NOT NULL
);
INSERT INTO platform_repair_activity
SELECT report_receiving_id,case_id,1,synced_at,'baseline'
FROM platform_list_entries WHERE scope='repair';
INSERT OR IGNORE INTO settings(key,value) VALUES('platform_sync_policy',
  '{"enabled":true,"hour":8,"minute":0,"recent_days":2,"request_interval":1.0,"batch_requests":300,"daily_requests":1000,"photo_requests":100,"photo_interval_hours":24}');
