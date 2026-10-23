-- Keep device identity for capture provenance after removal from the admin list.
ALTER TABLE camera_devices ADD COLUMN deleted_at TEXT;
