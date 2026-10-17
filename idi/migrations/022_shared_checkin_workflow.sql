-- Same-entry reuse changes reporting eligibility even when the original stays
-- untouched. Invalidate queued material/field evidence on both add and remove.
CREATE TRIGGER shared_checkin_workflow_added AFTER INSERT ON visit_shared_checkins BEGIN
    UPDATE photos SET revision=revision+1,updated_at=NEW.created_at WHERE id=NEW.photo_id;
    UPDATE photo_pipeline SET dirty=1,updated_at=NEW.created_at WHERE photo_id=NEW.photo_id;
END;
CREATE TRIGGER shared_checkin_workflow_removed AFTER DELETE ON visit_shared_checkins BEGIN
    UPDATE photos SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now') WHERE id=OLD.photo_id;
    UPDATE photo_pipeline SET dirty=1,updated_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now') WHERE photo_id=OLD.photo_id;
END;
