CREATE TABLE followup_issue_confirmations (
    issue_id TEXT PRIMARY KEY REFERENCES followup_issues(id),
    context_signature TEXT NOT NULL,
    title TEXT NOT NULL, detail TEXT NOT NULL, action TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
ALTER TABLE followup_feedback ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE followup_feedback ADD COLUMN context_signatures TEXT NOT NULL DEFAULT '{}';
