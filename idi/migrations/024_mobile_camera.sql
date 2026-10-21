CREATE TABLE camera_devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    project_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT
);
CREATE TABLE camera_pairings (
    code_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_ids TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE camera_captures (
    device_id TEXT NOT NULL REFERENCES camera_devices(id),
    capture_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
    original_hash TEXT NOT NULL REFERENCES blobs(hash),
    stamped_hash TEXT NOT NULL REFERENCES blobs(hash),
    metadata TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(device_id, capture_id)
);
