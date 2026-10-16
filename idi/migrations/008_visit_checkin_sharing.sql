ALTER TABLE visits ADD COLUMN started_at TEXT;
ALTER TABLE visits ADD COLUMN finished_at TEXT;
ALTER TABLE visits ADD COLUMN crew_number INTEGER;
ALTER TABLE visits ADD COLUMN same_day_repair INTEGER NOT NULL DEFAULT 0;
ALTER TABLE visits ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;

-- A survey original can serve one explicitly identified repair entry as well.
-- Another entry, even on the same date, needs its own check-in evidence.
CREATE TABLE visit_shared_checkins (
    photo_id TEXT PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    visit_id TEXT NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE INDEX visit_shared_checkins_visit ON visit_shared_checkins(visit_id);

CREATE TRIGGER invalidate_shared_checkin AFTER UPDATE OF case_id,stage,role,photo_group,watermark_time,exif_time ON photos
WHEN OLD.case_id IS NOT NEW.case_id OR OLD.stage IS NOT NEW.stage OR OLD.role IS NOT NEW.role
  OR OLD.photo_group IS NOT NEW.photo_group OR OLD.watermark_time IS NOT NEW.watermark_time OR OLD.exif_time IS NOT NEW.exif_time
BEGIN
    UPDATE visits SET revision=revision+1 WHERE id IN (SELECT visit_id FROM visit_shared_checkins WHERE photo_id=NEW.id);
    DELETE FROM visit_shared_checkins WHERE photo_id=NEW.id;
END;
