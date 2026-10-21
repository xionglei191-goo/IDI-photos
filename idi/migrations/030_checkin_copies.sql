-- A reused check-in is a distinct archive record with its own classification.
-- The source ID is historical provenance, so deleting either record does not
-- delete or relabel the other one.
ALTER TABLE photos ADD COLUMN reuse_source_id TEXT;
ALTER TABLE photos ADD COLUMN reuse_source_revision INTEGER;
ALTER TABLE photos ADD COLUMN reuse_metadata TEXT NOT NULL DEFAULT '{}';

DROP INDEX photos_case_blob;
DROP INDEX photos_unassigned_blob;
CREATE UNIQUE INDEX photos_case_blob ON photos(case_id,blob_hash)
    WHERE case_id IS NOT NULL AND reuse_source_id IS NULL;
CREATE UNIQUE INDEX photos_unassigned_blob ON photos(COALESCE(project_id,''),blob_hash)
    WHERE case_id IS NULL AND reuse_source_id IS NULL;
CREATE UNIQUE INDEX photos_checkin_copy_blob ON photos(case_id,blob_hash)
    WHERE reuse_source_id IS NOT NULL;
CREATE INDEX photos_reuse_source ON photos(reuse_source_id);

-- Changes to the copied record already invalidate its own workflow evidence.
-- The source photo must keep its original revision and classification.
DROP TRIGGER shared_checkin_workflow_added;
DROP TRIGGER shared_checkin_workflow_removed;
CREATE TRIGGER shared_checkin_workflow_added AFTER INSERT ON visit_shared_checkins
BEGIN
    UPDATE photo_pipeline SET dirty=1 WHERE photo_id=NEW.photo_id;
END;
CREATE TRIGGER shared_checkin_workflow_removed AFTER DELETE ON visit_shared_checkins
BEGIN
    UPDATE photo_pipeline SET dirty=1 WHERE photo_id=OLD.photo_id;
END;
DROP TRIGGER invalidate_shared_checkin;
CREATE TRIGGER invalidate_shared_checkin
AFTER UPDATE OF case_id,stage,role,photo_group,watermark_time,exif_time ON photos
WHEN (OLD.case_id IS NOT NEW.case_id OR OLD.stage IS NOT NEW.stage OR OLD.role IS NOT NEW.role
   OR OLD.photo_group IS NOT NEW.photo_group OR OLD.watermark_time IS NOT NEW.watermark_time
   OR OLD.exif_time IS NOT NEW.exif_time)
 AND NOT EXISTS(SELECT 1 FROM photos copy WHERE copy.reuse_source_id=NEW.id)
BEGIN
    UPDATE visits SET revision=revision+1
        WHERE id IN (SELECT visit_id FROM visit_shared_checkins WHERE photo_id=NEW.id);
    DELETE FROM visit_shared_checkins WHERE photo_id=NEW.id;
END;
