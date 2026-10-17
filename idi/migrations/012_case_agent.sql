CREATE TABLE case_agent_runs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed','partial','failed','cancelled')),
    signature TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    model TEXT NOT NULL,
    result TEXT,
    error TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    usage TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX case_agent_runs_case ON case_agent_runs(case_id,created_at);
CREATE UNIQUE INDEX case_agent_one_active ON case_agent_runs(case_id) WHERE state IN ('queued','running');
CREATE TABLE case_agent_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES case_agent_runs(id),
    step INTEGER NOT NULL,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX case_agent_steps_run ON case_agent_steps(run_id,step,created_at);
CREATE TABLE case_agent_adoptions (
    run_id TEXT NOT NULL REFERENCES case_agent_runs(id),
    finding_index INTEGER NOT NULL,
    issue_id TEXT NOT NULL REFERENCES followup_issues(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,finding_index)
);
