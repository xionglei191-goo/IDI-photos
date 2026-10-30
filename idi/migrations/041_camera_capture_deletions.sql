-- Keep a device-scoped deletion even when its upload has not arrived yet.
-- Originals and existing capture/audit rows remain available for traceability.
CREATE TABLE camera_capture_deletions (
    device_id TEXT NOT NULL REFERENCES camera_devices(id),
    capture_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    requested_by TEXT REFERENCES idi_users(id),
    deleted_at TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY(device_id, capture_id)
);
