-- A manual attachment choice applies to one field and one version of a photo.
CREATE TABLE platform_manual_file_selections (
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    field TEXT NOT NULL,
    photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    photo_revision INTEGER NOT NULL,
    blob_hash TEXT NOT NULL REFERENCES blobs(hash),
    target_report_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (case_id, stage, field, photo_id)
);

-- Recover existing choices only when the current draft is exactly the version
-- saved by a person and every retained photo predates that explicit save.
INSERT OR IGNORE INTO platform_manual_file_selections
SELECT d.case_id, d.stage, f.key, p.id, p.revision, p.blob_hash,
       d.target_report_id, a.created_at
FROM platform_drafts d
JOIN cases c ON c.id=d.case_id AND c.workflow_stage=d.stage
JOIN platform_links l ON l.case_id=d.case_id AND l.report_receiving_id=d.target_report_id
JOIN audit a ON a.entity_type='platform_draft' AND a.entity_id=d.case_id
    AND a.action='local_save' AND a.actor='local'
    AND json_extract(a.after_json,'$.stage')=d.stage
    AND json_extract(a.after_json,'$.revision')=d.revision
    AND json_extract(a.after_json,'$.files')=d.files_json
JOIN json_each(d.files_json) f
JOIN json_each(f.value) selected
JOIN photos p ON p.id=selected.value AND p.case_id=d.case_id AND p.updated_at<=a.created_at
WHERE d.target_report_id<>'';

-- Also covers receipt readback, withdrawal and local deletion, which update
-- selections without going through the form's save method.
CREATE TRIGGER clear_removed_manual_file_selections
AFTER UPDATE OF files_json, target_report_id ON platform_drafts
BEGIN
    DELETE FROM platform_manual_file_selections
    WHERE case_id=NEW.case_id AND stage=NEW.stage
      AND (target_report_id<>NEW.target_report_id OR NOT EXISTS (
          SELECT 1 FROM json_each(NEW.files_json) f, json_each(f.value) selected
          WHERE f.key=platform_manual_file_selections.field
            AND selected.value=platform_manual_file_selections.photo_id
      ));
END;
