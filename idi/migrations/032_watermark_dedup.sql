-- A watermark identifies a capture; the content hash still identifies a file.
ALTER TABLE photos ADD COLUMN watermark_code TEXT NOT NULL DEFAULT '';
ALTER TABLE photos ADD COLUMN watermark_code_source TEXT NOT NULL DEFAULT '';
ALTER TABLE photos ADD COLUMN watermark_code_confidence REAL NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN watermark_dedup_reason TEXT NOT NULL DEFAULT '';
CREATE INDEX photos_watermark_code ON photos(watermark_code)
    WHERE watermark_code<>'' AND reuse_source_id IS NULL;

-- Reuse the OCR performed on an incoming attachment without a second OCR pass.
CREATE TABLE photo_ocr_cache (
    blob_hash TEXT PRIMARY KEY REFERENCES blobs(hash) ON DELETE CASCADE,
    raw_text TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL
);

-- Already archived versions remain available to reports, receipts and audits.
CREATE TABLE photo_file_versions (
    id TEXT PRIMARY KEY, photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
    blob_hash TEXT NOT NULL REFERENCES blobs(hash), replacement_hash TEXT NOT NULL REFERENCES blobs(hash),
    snapshot TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TRIGGER watermark_file_changed AFTER UPDATE OF blob_hash ON photos
WHEN OLD.blob_hash<>NEW.blob_hash BEGIN
    UPDATE photo_pipeline SET dirty=1,updated_at=NEW.updated_at WHERE photo_id=NEW.id;
END;
