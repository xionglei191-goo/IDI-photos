ALTER TABLE review_notifications ADD COLUMN purpose TEXT NOT NULL DEFAULT 'review'
    CHECK(purpose IN ('review','checkin_gap'));
ALTER TABLE review_notifications ADD COLUMN official_checked_at TEXT;
ALTER TABLE review_notifications ADD COLUMN retry_at REAL NOT NULL DEFAULT 0;
CREATE INDEX review_notifications_followup ON review_notifications(case_id,stage,purpose,state);
