ALTER TABLE automatic_upload_cases ADD COLUMN photo_signature TEXT NOT NULL DEFAULT '';
ALTER TABLE automatic_upload_cases ADD COLUMN proposal_not_before REAL NOT NULL DEFAULT 0;
