-- Prepare defaults for new, explicitly confirmed submissions or manual requests.
-- Existing cases/packages are not enrolled by the upgrade.
CREATE TABLE default_package_requests (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK(source IN ('submission','manual')),
    source_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','handled')),
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    UNIQUE(source, source_id)
);
CREATE INDEX default_package_requests_pending ON default_package_requests(state, case_id);
INSERT OR IGNORE INTO settings(key,value) VALUES('default_package_auto_policy',
    json_object('activated_at',strftime('%Y-%m-%dT%H:%M:%fZ','now')));
