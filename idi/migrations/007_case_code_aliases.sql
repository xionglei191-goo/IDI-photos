-- Every issued code keeps its case owner, including the current display code.
CREATE TABLE case_code_aliases (
    code TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE INDEX case_code_aliases_case_idx ON case_code_aliases(case_id);
INSERT INTO case_code_aliases(code,case_id,created_at)
    SELECT short_code,id,created_at FROM cases WHERE short_code IS NOT NULL AND short_code<>'';

-- Cover all writers, including official imports and startup code generation.
CREATE TRIGGER case_code_owner_insert BEFORE INSERT ON cases
WHEN NEW.short_code IS NOT NULL AND NEW.short_code<>''
BEGIN
    SELECT RAISE(ABORT,'short code belongs to another case')
    WHERE EXISTS(SELECT 1 FROM case_code_aliases WHERE code=NEW.short_code AND case_id<>NEW.id);
END;
CREATE TRIGGER case_code_owner_update BEFORE UPDATE OF short_code ON cases
WHEN NEW.short_code IS NOT NULL AND NEW.short_code<>''
BEGIN
    SELECT RAISE(ABORT,'short code belongs to another case')
    WHERE EXISTS(SELECT 1 FROM case_code_aliases WHERE code=NEW.short_code AND case_id<>NEW.id);
END;
CREATE TRIGGER case_code_remember_insert AFTER INSERT ON cases
WHEN NEW.short_code IS NOT NULL AND NEW.short_code<>''
BEGIN
    INSERT OR IGNORE INTO case_code_aliases(code,case_id,created_at) VALUES(NEW.short_code,NEW.id,NEW.created_at);
END;
CREATE TRIGGER case_code_remember_update AFTER UPDATE OF short_code ON cases
WHEN NEW.short_code IS NOT NULL AND NEW.short_code<>''
BEGIN
    INSERT OR IGNORE INTO case_code_aliases(code,case_id,created_at) VALUES(NEW.short_code,NEW.id,NEW.updated_at);
END;
