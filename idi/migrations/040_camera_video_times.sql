-- Only new uploads enqueue extraction. Existing captures remain untouched.
CREATE TABLE camera_video_times (
    device_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    photo_id TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    result TEXT NOT NULL DEFAULT '{}',
    job_id TEXT NOT NULL REFERENCES jobs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(device_id, capture_id),
    FOREIGN KEY(device_id, capture_id) REFERENCES camera_official_captures(device_id, capture_id)
);
