CREATE TABLE platform_list_entries (
  scope TEXT NOT NULL CHECK(scope IN ('intake','repair')),
  report_receiving_id TEXT NOT NULL,
  case_id TEXT NOT NULL REFERENCES cases(id),
  position INTEGER NOT NULL,
  snapshot TEXT NOT NULL,
  sync_run_id TEXT NOT NULL REFERENCES platform_sync_runs(id),
  synced_at TEXT NOT NULL,
  PRIMARY KEY(scope,report_receiving_id),
  UNIQUE(scope,case_id)
);
CREATE INDEX platform_lists_case_idx ON platform_list_entries(case_id);
