ALTER TABLE camera_pairings ADD COLUMN issued_by TEXT REFERENCES idi_users(id);
