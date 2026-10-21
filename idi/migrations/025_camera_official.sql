-- Official SDK bytes remain outside photos until the official verifier passes.
CREATE TABLE camera_official_captures (
    device_id TEXT NOT NULL REFERENCES camera_devices(id),
    capture_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    blob_hash TEXT NOT NULL REFERENCES blobs(hash),
    metadata TEXT NOT NULL,
    state TEXT NOT NULL,
    task_id TEXT,
    credential_group_hash TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '{}',
    report TEXT NOT NULL DEFAULT '{}',
    photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(device_id, capture_id)
);
CREATE INDEX camera_official_pending ON camera_official_captures(state,next_attempt_at);
