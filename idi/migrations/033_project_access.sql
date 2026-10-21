CREATE TABLE idi_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    system_role TEXT NOT NULL DEFAULT 'user' CHECK(system_role IN ('admin','user')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE project_members (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES idi_users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('manager','member')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,user_id)
);
CREATE INDEX project_members_by_user ON project_members(user_id,project_id);
CREATE TABLE idi_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES idi_users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX idi_sessions_by_user ON idi_sessions(user_id,expires_at);
CREATE TABLE idi_login_attempts (
    key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    window_started_at TEXT NOT NULL
);
ALTER TABLE camera_devices ADD COLUMN user_id TEXT REFERENCES idi_users(id);
ALTER TABLE camera_pairings ADD COLUMN user_id TEXT REFERENCES idi_users(id);
ALTER TABLE photos ADD COLUMN uploaded_by TEXT REFERENCES idi_users(id);
ALTER TABLE import_batches ADD COLUMN user_id TEXT REFERENCES idi_users(id);
ALTER TABLE jobs ADD COLUMN user_id TEXT REFERENCES idi_users(id);
CREATE INDEX photos_by_uploader ON photos(uploaded_by,created_at);
CREATE TABLE project_website_accounts (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    updated_at TEXT NOT NULL
);
CREATE TABLE website_operations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    operation TEXT NOT NULL,
    actor_id TEXT REFERENCES idi_users(id),
    actor_name TEXT NOT NULL DEFAULT '',
    website_account TEXT NOT NULL,
    account_revision TEXT NOT NULL,
    photo_ids TEXT NOT NULL DEFAULT '[]',
    source_users TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
CREATE INDEX website_operations_project_time ON website_operations(project_id,created_at DESC);
