CREATE TABLE archive_tree_state (
    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 1,
    synced_revision INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
    last_started_at TEXT, last_synced_at TEXT, errors TEXT NOT NULL DEFAULT '[]',
    file_count INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0
);
INSERT INTO archive_tree_state(id) VALUES(1);
CREATE TABLE archive_tree_entries (
    path TEXT PRIMARY KEY, entry_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('file','directory')),
    hash TEXT, size_bytes INTEGER, mtime_ns INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX archive_tree_entry_key ON archive_tree_entries(entry_key);

CREATE TRIGGER tree_projects_insert AFTER INSERT ON projects BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_projects_update AFTER UPDATE ON projects BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_projects_delete AFTER DELETE ON projects BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_cases_insert AFTER INSERT ON cases BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_cases_update AFTER UPDATE ON cases BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_cases_delete AFTER DELETE ON cases BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_photos_insert AFTER INSERT ON photos BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_photos_update AFTER UPDATE ON photos BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_photos_delete AFTER DELETE ON photos BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_sources_insert AFTER INSERT ON photo_sources BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_sources_update AFTER UPDATE ON photo_sources BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_sources_delete AFTER DELETE ON photo_sources BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_packages_insert AFTER INSERT ON packages BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_packages_update AFTER UPDATE ON packages BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_packages_delete AFTER DELETE ON packages BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_receipts_insert AFTER INSERT ON package_events BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_receipts_update AFTER UPDATE ON package_events BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
CREATE TRIGGER tree_receipts_delete AFTER DELETE ON package_events BEGIN UPDATE archive_tree_state SET revision=revision+1 WHERE id=1; END;
