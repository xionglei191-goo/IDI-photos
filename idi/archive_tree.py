from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path, PurePosixPath

from .db import Database, dump, now, uid
from .domain import DomainError
from .storage import Storage, safe_name

log = logging.getLogger(__name__)
TREE_STAGES = {"unknown": "待分类", "survey": "查勘", "work": "施工", "acceptance": "完工"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def distinct_names(rows, caption, limit=56, always_suffix=False, reserved=()):
    """Windows names are case-insensitive; IDs separate concurrent cases and sanitized collisions."""
    names = {row["id"]: safe_name(caption(row), limit) for row in rows}
    counts = {}
    for value in names.values():
        counts[value.casefold()] = counts.get(value.casefold(), 0) + 1
    used = {name.casefold() for name in reserved}
    result = {}
    for key in sorted(names):
        name = names[key]
        if always_suffix or counts[name.casefold()] > 1 or name.casefold() in used:
            name += "__" + key[:12]
        if name.casefold() in used:
            name = names[key] + "__" + key
        used.add(name.casefold())
        result[key] = name
    return result


def case_caption(case):
    parts = []
    for key, suffix in (("building", "栋"), ("unit", "单元"), ("floor", "层")):
        value = case[key].strip()
        if value:
            parts.append(value if value.endswith(suffix) else value + suffix)
    if case["room"]:
        parts.append(case["room"])
    if case["location_detail"]:
        parts.append(case["location_detail"])
    return "_".join(filter(None, (case.get('short_code') or case.get('platform_report_code'), "".join(parts), case["title"])))


class ArchiveTree:
    """A recoverable filesystem view. Copies never share file data with the immutable originals."""

    def __init__(self, db: Database, storage: Storage):
        self.db, self.storage = db, storage
        self.root = storage.settings.data_dir / "项目资料"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def status(self):
        value = self.db.one("SELECT * FROM archive_tree_state WHERE id=1")
        value.pop("id")
        value["errors"] = json.loads(value["errors"])
        value["pending"] = value["revision"] != value["synced_revision"]
        if value["state"] == "ready" and value["pending"]:
            value["state"] = "pending"
        return {**value, "root": str(self.root), "automatic": self.storage.settings.worker_enabled}

    @staticmethod
    def _key(relative):
        return os.path.normcase(relative)

    def _rekey(self, old, relative):
        if old and old["path"] != relative:
            with self.db.tx() as con:
                con.execute("UPDATE archive_tree_entries SET path=? WHERE path=?", (relative, old["path"]))

    def _path(self, relative=""):
        parts = PurePosixPath(relative.replace("\\", "/"))
        if parts.is_absolute() or ".." in parts.parts or ":" in relative:
            raise ValueError("目录树路径无效")
        candidate = self.root.joinpath(*parts.parts)
        # A junction or symlink must never redirect creation, cleanup, or opening outside this tree.
        for path in (candidate, *candidate.parents):
            if path == self.storage.settings.data_dir:
                break
            if path.is_symlink() or path.is_junction():
                raise ValueError("目录树路径含链接，请移走该链接后重试：" + str(path))
        if not candidate.resolve().is_relative_to(self.root):
            raise ValueError("目录树路径越界")
        return candidate

    def _snapshot(self):
        with self.db.read() as con:
            con.execute("BEGIN")
            revision = con.execute("SELECT revision FROM archive_tree_state WHERE id=1").fetchone()[0]
            projects = [dict(r) for r in con.execute("SELECT * FROM projects")]
            cases = [dict(r) for r in con.execute("SELECT * FROM cases")]
            photos = [dict(r) for r in con.execute("""SELECT p.*,b.stored_path,b.extension,
                (SELECT original_name FROM photo_sources s WHERE s.photo_id=p.id ORDER BY s.received_at,s.rowid LIMIT 1) AS original_name
                FROM photos p JOIN blobs b ON b.hash=p.blob_hash""")]
            packages = [dict(r) for r in con.execute("SELECT * FROM packages WHERE state IN ('ready','submitted','accepted','returned')")]
            platform_packages = [dict(r) for r in con.execute("SELECT * FROM platform_packages")]
            receipts = [dict(r) for r in con.execute("""SELECT e.*,b.stored_path,b.extension,b.hash FROM package_events e
                JOIN blobs b ON b.hash=e.attachment_hash""")]
        return revision, projects, cases, photos, packages, receipts, platform_packages

    def _plan(self):
        revision, projects, cases, photos, packages, receipts, platform_packages = self._snapshot()
        project_dirs = distinct_names(projects, lambda r: r["name"], 36, reserved=("待归档",))
        case_names = distinct_names(cases, case_caption, 56, always_suffix=True)
        case_dirs = {r["id"]: project_dirs[r["project_id"]] + "/" + case_names[r["id"]] for r in cases}
        # Metadata alone must not create folders. Each local file adds only the
        # project, case and stage directories needed to expose that file.
        directories, files, used_paths = set(), {}, set()

        def add(key, relative, source, expected):
            if relative.casefold() in used_paths:
                raise ValueError("目录树文件名冲突：" + relative)
            used_paths.add(relative.casefold())
            files[relative] = {"entry_key": key, "source": self.storage.resolve(source), "hash": expected}
            for parent in PurePosixPath(relative).parents:
                if str(parent) != ".":
                    directories.add(str(parent))

        # Original names stay recognizable; a stable internal suffix prevents same-name overwrites.
        photo_names = distinct_names(photos, lambda p: Path((p["original_name"] or "照片").replace("\\", "/")).stem, 48, always_suffix=True)
        for photo in photos:
            base = case_dirs.get(photo["case_id"])
            if base is None:
                project = project_dirs.get(photo["project_id"])
                base = (project + "/" if project else "") + "待归档"
            relative = base + "/" + TREE_STAGES[photo["stage"]] + "/" + photo_names[photo["id"]] + photo["extension"]
            add("photo:" + photo["id"], relative, photo["stored_path"], photo["blob_hash"])
        package_dirs = {}
        for package in packages:
            base = case_dirs[package["case_id"]] + "/报送资料/第" + str(package["version"]).zfill(3) + "版"
            package_dirs[package["id"]] = base
            add("package:" + package["id"], base + "/资料报送包.zip", package["zip_path"], package["zip_hash"])
        for receipt in receipts:
            if receipt["package_id"] in package_dirs:
                relative = package_dirs[receipt["package_id"]] + "/回执/" + safe_name(receipt["event_at"], 20) + "__" + receipt["id"] + receipt["extension"]
                add("receipt:" + receipt["id"], relative, receipt["stored_path"], receipt["hash"])
        from .platform_contract import STAGES as PLATFORM_STAGES
        for package in platform_packages:
            label = PLATFORM_STAGES[package["stage"]]["label"]
            relative = case_dirs[package["case_id"]] + "/报送资料/官网资料准备/" + label + "__" + package["id"][:12] + ".zip"
            add("platform_package:" + package["id"], relative, package["zip_path"], package["zip_hash"])
        return revision, directories, files

    def _directory(self, relative, old):
        target = self._path(relative)
        if target.is_dir():
            if old and old["path"] != relative:
                previous = self._path(old["path"])
                if not previous.samefile(target):
                    raise ValueError("名称相近的目录存在冲突，已保留")
                if previous.name != target.name:
                    previous.rename(target)
                self._rekey(old, relative)
            return
        self._rekey(old, relative)
        target.mkdir()  # Parents are created first; do not claim existing user directories.
        with self.db.tx() as con:
            con.execute("INSERT OR IGNORE INTO archive_tree_entries(path,entry_key,kind,created_at) VALUES(?,?,'directory',?)", (relative, "directory:" + relative, now()))

    def _file(self, relative, wanted, old, force):
        target = self._path(relative)
        if target.exists() and (not old or old["kind"] != "file" or old["entry_key"] != wanted["entry_key"]):
            raise ValueError("目标位置已有非系统文件，已保留，请移开后重试")
        if not wanted["source"].is_file():
            raise ValueError("内部归档原件丢失，请从备份核对恢复")
        if old and old["hash"] != wanted["hash"]:
            raise ValueError("同一目录记录的原件标识发生变化，已保留现有副本，请核对")
        if target.is_file():
            if old and old["path"] != relative and not self._path(old["path"]).samefile(target):
                raise ValueError("名称相近的文件存在冲突，已保留")
            stat = target.stat()
            changed = not old or stat.st_size != old["size_bytes"] or stat.st_mtime_ns != old["mtime_ns"]
            if force or changed:
                if digest(target) != wanted["hash"]:
                    raise ValueError("目录中的副本已被手动修改，已保留；请另存或移走后重新同步")
            if force and digest(wanted["source"]) != wanted["hash"]:
                raise ValueError("内部归档原件校验失败，未修改目录中的副本")
            if old and old["path"] != relative:
                previous = self._path(old["path"])
                if previous.name != target.name:
                    previous.rename(target)
            self._rekey(old, relative)
        else:
            if target.exists():
                raise ValueError("目标文件位置被其他目录占用")
            self._rekey(old, relative)
            temp = self.storage.settings.data_dir / "staging" / ("tree-" + uid() + ".tmp")
            try:
                shutil.copyfile(wanted["source"], temp)
                if digest(temp) != wanted["hash"]:
                    raise ValueError("内部归档原件校验失败，未生成目录副本")
                # Persist ownership before publishing so interruption can be resumed safely.
                with self.db.tx() as con:
                    con.execute("""INSERT INTO archive_tree_entries(path,entry_key,kind,hash,created_at) VALUES(?,?,'file',?,?)
                        ON CONFLICT(path) DO NOTHING""", (relative, wanted["entry_key"], wanted["hash"], now()))
                self._path(relative)
                if os.name == "nt":
                    os.rename(temp, target)  # Windows rename fails if a target already exists.
                else:
                    os.link(temp, target)  # Atomic no-replace publication; temp is an independent copy.
                stat = target.stat()
            finally:
                temp.unlink(missing_ok=True)
        with self.db.tx() as con:
            con.execute("UPDATE archive_tree_entries SET hash=?,size_bytes=?,mtime_ns=? WHERE path=?", (wanted["hash"], stat.st_size, stat.st_mtime_ns, relative))

    def sync(self, force=False, progress=None):
        with self._lock:
            with self.db.tx() as con:
                con.execute("UPDATE archive_tree_state SET state='syncing',last_started_at=? WHERE id=1", (now(),))
            errors = []
            try:
                self._path().mkdir(exist_ok=True)
                revision, directories, files = self._plan()
                old = {r["path"]: r for r in self.db.all("SELECT * FROM archive_tree_entries")}
                old_by_key = {self._key(path): entry for path, entry in old.items()}
                desired_keys = {self._key(path) for path in (*directories, *files)}
                for directory in sorted(directories, key=lambda v: (v.count("/"), v)):
                    try:
                        self._directory(directory, old_by_key.get(self._key(directory)))
                    except (OSError, ValueError) as error:
                        errors.append({"path": directory, "error": str(error)})
                completed = set()
                for index, (relative, wanted) in enumerate(files.items(), 1):
                    try:
                        self._file(relative, wanted, old_by_key.get(self._key(relative)), force)
                        completed.add(wanted["entry_key"])
                    except (OSError, ValueError) as error:
                        errors.append({"path": relative, "error": str(error)})
                    if progress:
                        progress(index, len(files))
                expected_keys = {wanted["entry_key"] for wanted in files.values()}
                # Never remove an old location until its replacement has been verified.
                for relative, entry in sorted(old.items(), key=lambda item: -item[0].count("/")):
                    if self._key(relative) in desired_keys:
                        continue
                    try:
                        target = self._path(relative)
                        if entry["kind"] == "file":
                            if entry["entry_key"] in expected_keys and entry["entry_key"] not in completed:
                                continue
                            if target.exists():
                                if not target.is_file() or digest(target) != entry["hash"]:
                                    raise ValueError("旧位置有人工修改的文件，已保留，请核对后另存或移走")
                                target.unlink()
                        elif target.is_dir():
                            if any(target.iterdir()):
                                continue  # Manually added files and nonempty directories are retained.
                            target.rmdir()
                        elif target.exists():
                            raise ValueError("原目录位置已变为文件，已保留")
                        with self.db.tx() as con:
                            con.execute("DELETE FROM archive_tree_entries WHERE path=?", (relative,))
                    except (OSError, ValueError) as error:
                        errors.append({"path": relative, "error": str(error)})
                stamp = now()
                with self.db.tx() as con:
                    con.execute("""UPDATE archive_tree_state SET state=?,errors=?,error_count=?,file_count=?,
                        synced_revision=CASE WHEN ?=0 THEN ? ELSE synced_revision END,
                        last_synced_at=CASE WHEN ?=0 THEN ? ELSE last_synced_at END WHERE id=1""",
                        ("partial" if errors and completed else "failed" if errors else "ready", dump(errors[:100]), len(errors), len(completed), len(errors), revision, len(errors), stamp))
            except Exception as error:
                log.exception("Archive directory synchronization failed")
                errors.append({"path": "", "error": str(error)})
                with self.db.tx() as con:
                    con.execute("UPDATE archive_tree_state SET state='failed',errors=?,error_count=? WHERE id=1", (dump(errors[:100]), len(errors)))
            result = self.status()
            if not errors:
                result["state"] = "completed"
            return result

    def process(self, payload, progress):
        return self.sync(force=True, progress=progress)

    def open_folder(self):
        target = self._path()
        if not target.is_dir():
            raise DomainError("目录尚未生成，请先同步目录")
        if os.name != "nt":
            raise DomainError("此平台请按显示路径手动打开目录")
        os.startfile(str(target), "explore")
        return {"path": str(target)}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="idi-archive-tree", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        first, last_scan, retry_after = True, 0.0, 0.0
        while not self._stop.is_set():
            try:
                current = time.monotonic()
                if current >= retry_after and (first or self.status()["pending"] or current - last_scan >= 60):
                    result = self.sync(force=first)
                    first, last_scan = False, time.monotonic()
                    retry_after = last_scan + (30 if result["error_count"] else 0)
            except Exception:
                log.exception("Archive directory background worker failed")
                retry_after = time.monotonic() + 30
            self._stop.wait(1)
