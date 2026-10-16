CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, aliases TEXT NOT NULL DEFAULT '[]',
    insurer TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE cases (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    report_number TEXT, material_number TEXT, title TEXT NOT NULL,
    building TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', floor TEXT NOT NULL DEFAULT '',
    room TEXT NOT NULL DEFAULT '', location_type TEXT NOT NULL DEFAULT 'household' CHECK(location_type IN ('household','public')),
    location_detail TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    insurer TEXT NOT NULL DEFAULT '', contact_name TEXT NOT NULL DEFAULT '', contact_phone TEXT NOT NULL DEFAULT '',
    repair_parts TEXT NOT NULL DEFAULT '[]', report_at TEXT, submitted_at TEXT, quoted_at TEXT, accepted_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','survey','working','completed','accepted','closed')),
    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX cases_report_unique ON cases(project_id, report_number) WHERE report_number IS NOT NULL AND report_number <> '';
CREATE INDEX cases_project_idx ON cases(project_id);
CREATE TABLE visits (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), stage TEXT NOT NULL,
    visit_date TEXT NOT NULL, visit_key TEXT NOT NULL DEFAULT '', basis TEXT NOT NULL DEFAULT 'manual',
    notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    UNIQUE(case_id, stage, visit_date, visit_key)
);
CREATE TABLE blobs (
    hash TEXT PRIMARY KEY, stored_path TEXT NOT NULL, thumbnail_path TEXT,
    extension TEXT NOT NULL, mime_type TEXT NOT NULL, media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, width INTEGER, height INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE photos (
    id TEXT PRIMARY KEY, blob_hash TEXT NOT NULL REFERENCES blobs(hash),
    project_id TEXT REFERENCES projects(id), case_id TEXT REFERENCES cases(id), visit_id TEXT REFERENCES visits(id),
    stage TEXT NOT NULL DEFAULT 'unknown' CHECK(stage IN ('unknown','survey','work','acceptance')),
    role TEXT NOT NULL DEFAULT 'unknown' CHECK(role IN ('unknown','building_checkin','door_checkin','problem','construction','finished','other')),
    part TEXT NOT NULL DEFAULT '', process TEXT NOT NULL DEFAULT '', watermark_time TEXT, exif_time TEXT,
    notes TEXT NOT NULL DEFAULT '', recognition_state TEXT NOT NULL DEFAULT 'pending',
    confirmed_fields TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX photos_case_blob ON photos(case_id, blob_hash) WHERE case_id IS NOT NULL;
CREATE UNIQUE INDEX photos_unassigned_blob ON photos(COALESCE(project_id,''), blob_hash) WHERE case_id IS NULL;
CREATE INDEX photos_case_idx ON photos(case_id,stage);
CREATE TABLE import_batches (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
    project_id TEXT REFERENCES projects(id), case_id TEXT REFERENCES cases(id),
    total INTEGER NOT NULL DEFAULT 0, imported INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0, details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE import_items (
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id), source_name TEXT NOT NULL,
    status TEXT NOT NULL, photo_id TEXT REFERENCES photos(id), error TEXT, created_at TEXT NOT NULL
);
CREATE TABLE connectors (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('folder','wxauto','feed')),
    enabled INTEGER NOT NULL DEFAULT 0, config TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'disabled',
    last_seen_at TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY, connector_id TEXT NOT NULL REFERENCES connectors(id), source_key TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '', sent_at TEXT, received_at TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '', message_type TEXT NOT NULL DEFAULT 'text',
    media_state TEXT NOT NULL DEFAULT 'none', photo_id TEXT REFERENCES photos(id),
    raw TEXT NOT NULL DEFAULT '{}', error TEXT, UNIQUE(connector_id,source_key)
);
CREATE TABLE photo_sources (
    id TEXT PRIMARY KEY, photo_id TEXT NOT NULL REFERENCES photos(id), batch_id TEXT REFERENCES import_batches(id),
    message_id TEXT REFERENCES messages(id), source_type TEXT NOT NULL, source_key TEXT NOT NULL,
    original_name TEXT NOT NULL, original_path TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '', message_time TEXT, received_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}', UNIQUE(photo_id, source_type, source_key)
);
CREATE TABLE recognitions (
    id TEXT PRIMARY KEY, photo_id TEXT NOT NULL REFERENCES photos(id), provider TEXT NOT NULL,
    state TEXT NOT NULL, raw_text TEXT NOT NULL DEFAULT '', suggestions TEXT NOT NULL DEFAULT '{}',
    candidates TEXT NOT NULL DEFAULT '[]', error TEXT, created_at TEXT NOT NULL
);
CREATE TABLE templates (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, insurer TEXT NOT NULL DEFAULT '', requirements TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE packages (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), template_id TEXT REFERENCES templates(id),
    parent_id TEXT REFERENCES packages(id), version INTEGER NOT NULL, title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready', manifest TEXT NOT NULL, zip_path TEXT NOT NULL,
    zip_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(case_id,version)
);
CREATE TABLE package_events (
    id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES packages(id), state TEXT NOT NULL,
    event_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', attachment_hash TEXT REFERENCES blobs(hash),
    created_at TEXT NOT NULL
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX jobs_state_idx ON jobs(state,created_at);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE audit (
    id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL,
    before_json TEXT, after_json TEXT, actor TEXT NOT NULL DEFAULT 'local', created_at TEXT NOT NULL
);
CREATE INDEX audit_entity_idx ON audit(entity_type,entity_id,created_at);
