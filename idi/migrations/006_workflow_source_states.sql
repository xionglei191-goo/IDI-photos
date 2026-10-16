-- Extend the non-key stage column without rebuilding cases or its dependent
-- archives. Each migration is applied in one transaction with foreign keys on.
ALTER TABLE cases ADD COLUMN workflow_stage_next TEXT NOT NULL DEFAULT 'pending_intake'
  CHECK(workflow_stage_next IN ('pending_intake','insurer_intake','insurer_review','historical_unverified','survey','repair','acceptance','settlement','closed','direct_payment','noncoverage'));
UPDATE cases SET workflow_stage_next=workflow_stage;
DROP INDEX cases_workflow_idx;
ALTER TABLE cases DROP COLUMN workflow_stage;
ALTER TABLE cases RENAME COLUMN workflow_stage_next TO workflow_stage;
CREATE INDEX cases_workflow_idx ON cases(workflow_stage,project_id);
