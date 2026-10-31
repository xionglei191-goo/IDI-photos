CREATE TABLE project_members_next (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES idi_users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('manager','member','worker')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,user_id)
);
INSERT INTO project_members_next SELECT * FROM project_members;
DROP TABLE project_members;
ALTER TABLE project_members_next RENAME TO project_members;
CREATE INDEX project_members_by_user ON project_members(user_id,project_id);

ALTER TABLE cases ADD COLUMN worker_assignment_revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE case_worker_assignments (
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES idi_users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    assigned_by TEXT REFERENCES idi_users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(case_id,user_id)
);
CREATE INDEX case_workers_by_user ON case_worker_assignments(user_id,project_id,case_id);
CREATE TRIGGER case_workers_project_changed AFTER UPDATE OF project_id ON cases
WHEN OLD.project_id<>NEW.project_id
BEGIN
    DELETE FROM case_worker_assignments WHERE case_id=NEW.id;
    UPDATE cases SET worker_assignment_revision=worker_assignment_revision+1 WHERE id=NEW.id;
END;
