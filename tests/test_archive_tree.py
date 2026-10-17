import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from idi.api import create_app
from idi.archive_tree import ArchiveTree, digest
from idi.cli import restore
from idi.config import Settings
from idi.db import uid

from conftest import image_bytes


def managed_file(services, entry_key):
    row = services["db"].one("SELECT * FROM archive_tree_entries WHERE entry_key=? AND kind='file'", (entry_key,))
    assert row is not None
    return services["archive_tree"].root / row["path"]


def test_copies_originals_into_readable_folders_and_preserves_same_names(services, case, photo, tmp_path):
    source = tmp_path / "现场来源.png"
    source.write_bytes(image_bytes("navy"))
    first = services["importer"].add_file(source.read_bytes(), source.name, case_id=case["id"], source_path=str(source), metadata={"stage": "survey"})
    second = photo("red", name=source.name, stage="survey")
    tree = services["archive_tree"]
    result = tree.sync(force=True)
    assert result["state"] == "completed" and result["file_count"] == 2
    copied = managed_file(services, "photo:" + first["photo_id"])
    another = managed_file(services, "photo:" + second["id"])
    assert copied != another and copied.parent == another.parent
    assert copied.parent.name == "查勘" and "1栋201" in copied.parent.parent.name
    assert copied.parent.parent.parent.name == "从化铂悦府"
    assert copied.name.startswith("现场来源__")
    assert copied.read_bytes() == source.read_bytes() == image_bytes("navy")
    blob = services["db"].one("SELECT b.* FROM blobs b JOIN photos p ON p.blob_hash=b.hash WHERE p.id=?", (first["photo_id"],))
    original = services["storage"].resolve(blob["stored_path"])
    assert os.stat(original).st_ino != os.stat(copied).st_ino
    assert {p.name for p in copied.parent.parent.iterdir() if p.is_dir()} == {"查勘"}
    before = copied.stat().st_mtime_ns
    assert tree.sync()["state"] == "completed"
    assert copied.stat().st_mtime_ns == before


def test_project_and_case_metadata_do_not_create_empty_folders(services, case, photo):
    domain, tree = services['domain'], services['archive_tree']
    other_project = domain.save_project({'name': '仅官网台账项目'})
    domain.save_case({'project_id': other_project['id'], 'title': '尚无本地照片'})
    domain.save_case({'project_id': case['project_id'], 'title': '同项目另一个空案件'})
    assert tree.sync()['state'] == 'completed'
    assert list(tree.root.iterdir()) == []
    assert services['db'].one('SELECT COUNT(*) AS n FROM archive_tree_entries')['n'] == 0
    p = photo(stage='work')
    assert tree.sync()['state'] == 'completed'
    copied = managed_file(services, 'photo:' + p['id'])
    assert list(tree.root.iterdir()) == [copied.parent.parent.parent]
    assert list(copied.parent.parent.parent.iterdir()) == [copied.parent.parent]
    assert list(copied.parent.parent.iterdir()) == [copied.parent]
    assert services['domain'].case(case['id'])['id'] == case['id']
    assert tree.sync(force=True)['state'] == 'completed'
    assert not (tree.root / '仅官网台账项目').exists()


def test_legacy_empty_managed_directories_are_pruned_without_claiming_user_folders(services, photo):
    p = photo(stage='survey')
    tree = services['archive_tree']
    tree.sync()
    copied = managed_file(services, 'photo:' + p['id'])
    before = copied.stat().st_mtime_ns
    legacy = ['空项目', '空项目/空案件', '空项目/空案件/查勘', '待归档',
              '手工资料项目', '手工资料项目/空案件',
              copied.parent.parent.relative_to(tree.root).as_posix() + '/施工']
    for path in sorted(legacy, key=lambda value: value.count('/')):
        tree._directory(path, None)
    manual = tree.root / '手工资料项目/交接说明.txt'
    manual.write_text('保留人工文件', encoding='utf-8')
    unregistered = tree.root / '人工创建的空目录'
    unregistered.mkdir()
    result = tree.sync(force=True)
    assert result['state'] == 'completed' and result['file_count'] == 1
    assert not (tree.root / '空项目').exists()
    assert not (tree.root / '待归档').exists()
    assert not (tree.root / '手工资料项目/空案件').exists()
    assert not (copied.parent.parent / '施工').exists()
    assert manual.read_text(encoding='utf-8') == '保留人工文件'
    assert unregistered.is_dir()
    assert copied.stat().st_mtime_ns == before and digest(copied) == p['blob_hash']
    assert services['db'].one('SELECT * FROM archive_tree_entries WHERE path=?', ('人工创建的空目录',)) is None


def test_reassigning_last_photo_removes_only_old_empty_project_folder(services, case, photo):
    p = photo(stage='survey')
    domain, tree = services['domain'], services['archive_tree']
    tree.sync()
    old_project = managed_file(services, 'photo:' + p['id']).parent.parent.parent
    project = domain.save_project({'name': '实际归属项目'})
    other = domain.save_case({'project_id': project['id'], 'title': '重新归档'})
    domain.update_photo(p['id'], {'case_id': other['id'], 'stage': 'work'})
    assert tree.sync()['state'] == 'completed'
    current = managed_file(services, 'photo:' + p['id'])
    assert current.parent.parent.parent.name == project['name']
    assert digest(current) == p['blob_hash'] and not old_project.exists()
    assert domain.case(case['id'])['id'] == case['id']


def test_rename_and_reclassification_keep_user_files_and_separate_concurrent_cases(services, case, photo):
    p = photo(stage="survey")
    tree, domain = services["archive_tree"], services["domain"]
    tree.sync()
    old = managed_file(services, "photo:" + p["id"])
    manual = old.parent.parent / "人工备注.txt"
    manual.write_text("保留这份交接说明", encoding="utf-8")
    other = domain.save_case({"project_id": case["project_id"], "title": case["title"], "building": "1", "room": "201"})
    domain.update_photo(p["id"], {"case_id": other["id"], "stage": "work"})
    assert tree.status()["pending"]
    assert tree.sync()["state"] == "completed"
    moved = managed_file(services, "photo:" + p["id"])
    assert moved.parent.name == "施工" and moved.parent.parent != old.parent.parent
    assert not old.exists() and manual.read_text(encoding="utf-8") == "保留这份交接说明"
    domain.save_project({"name": "更名项目"}, case["project_id"])
    domain.save_case({"title": "客厅墙面修复"}, other["id"])
    assert tree.sync()["state"] == "completed"
    renamed = managed_file(services, "photo:" + p["id"])
    assert renamed.parent.parent.parent.name == "更名项目" and "客厅墙面修复" in renamed.parent.parent.name
    assert not moved.exists() and manual.exists()


def test_pending_files_follow_assignment_and_sanitized_project_names_do_not_collide(services, tmp_path):
    domain, importer, tree = services["domain"], services["importer"], services["archive_tree"]
    first = domain.save_project({"name": "工程/A"})
    second = domain.save_project({"name": "工程:A"})
    p = importer.add_file(image_bytes(), "CON.png")
    q = importer.add_file(image_bytes("red"), "照片.png", project_id=second["id"])
    tree.sync()
    assert managed_file(services, "photo:" + p["photo_id"]).relative_to(tree.root).parts[:2] == ("待归档", "待分类")
    assert "待归档" in managed_file(services, "photo:" + q["photo_id"]).parts
    assigned = domain.save_case({"project_id": first["id"], "title": "室内/修复"})
    domain.update_photo(p["photo_id"], {"case_id": assigned["id"], "stage": "acceptance"})
    tree.sync()
    destination = managed_file(services, "photo:" + p["photo_id"])
    assert destination.parent.name == "完工"
    project_folders = [f.name for f in tree.root.iterdir() if f.name.startswith("工程_A")]
    assert len(project_folders) == 2 and len(set(project_folders)) == 2
    assert services["domain"].case(assigned["id"])["status"] == "pending"


def test_hand_edited_copies_are_not_overwritten_or_deleted_on_reclassification(services, case, photo):
    p = photo(stage="survey")
    tree = services["archive_tree"]
    tree.sync()
    old = managed_file(services, "photo:" + p["id"])
    old.write_bytes(b"manual annotation")
    result = tree.sync(force=True)
    assert result["state"] == "failed" and result["error_count"] == 1
    assert old.read_bytes() == b"manual annotation"
    services["domain"].update_photo(p["id"], {"stage": "work"})
    result = tree.sync()
    assert result["state"] == "partial" and old.read_bytes() == b"manual annotation"
    current = [f for f in tree.root.rglob("*.png") if f.parent.name == "施工"]
    assert len(current) == 1 and current[0].read_bytes() == image_bytes()
    blob = services["db"].one("SELECT * FROM blobs WHERE hash=?", (p["blob_hash"],))
    assert services["storage"].verify(blob)


def test_unregistered_target_and_missing_internal_file_fail_without_claiming_user_files(services, photo):
    p = photo(stage="work")
    tree = services["archive_tree"]
    _, _, plan = tree._plan()
    relative = next(iter(plan))
    target = tree.root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user document")
    assert tree.sync()["error_count"] == 1
    assert target.read_bytes() == b"user document"
    assert services["db"].one("SELECT * FROM archive_tree_entries WHERE path=?", (relative,)) is None
    target.unlink()
    blob = services["db"].one("SELECT * FROM blobs WHERE hash=?", (p["blob_hash"],))
    services["storage"].resolve(blob["stored_path"]).write_bytes(b"corruption")
    assert tree.sync()["state"] == "failed"
    assert not target.exists()


def test_interrupted_publish_and_deleted_copy_are_rebuilt(services, photo, monkeypatch):
    p = photo(stage="work")
    tree = services["archive_tree"]
    method = "rename" if os.name == "nt" else "link"
    original = getattr(os, method)
    def interrupted(*args, **kwargs):
        raise OSError("simulated interrupted publication")
    monkeypatch.setattr(os, method, interrupted)
    assert tree.sync()["state"] == "failed"
    monkeypatch.setattr(os, method, original)
    restarted = ArchiveTree(services["db"], services["storage"])
    assert restarted.sync(force=True)["state"] == "completed"
    path = managed_file(services, "photo:" + p["id"])
    assert digest(path) == p["blob_hash"]
    path.unlink()
    assert restarted.sync()["state"] == "completed"
    assert digest(path) == p["blob_hash"]


def test_directory_links_cannot_escape_archive(services, photo, tmp_path):
    import pytest
    photo(stage="work")
    tree = services["archive_tree"]
    outside = tmp_path / "outside"
    outside.mkdir()
    untouched = outside / "keep.txt"
    untouched.write_text("keep", encoding="utf-8")
    try:
        tree.root.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    result = tree.sync()
    assert result["state"] == "failed"
    assert list(outside.iterdir()) == [untouched]


def test_report_versions_receipts_and_backup_restore_rebuild_tree(services, case, photo, tmp_path):
    p = photo(stage="survey")
    reporter, tree = services["reporter"], services["archive_tree"]
    package = reporter.build(uid(), case["id"], [p["id"]])
    receipt = services["storage"].put(b"received", "回执.txt")
    reporter.add_event(package["id"], {"state": "accepted", "event_at": "2026-10-15 10:30"}, receipt["hash"])
    assert tree.sync()["file_count"] == 3
    zip_path = managed_file(services, "package:" + package["id"])
    assert zip_path.parent.name == "第001版" and zip_path.parent.parent.name == "报送资料"
    assert digest(zip_path) == package["zip_hash"]
    assert next(zip_path.parent.glob("回执/*.txt")).read_bytes() == b"received"
    backup = reporter.backup({}, lambda *_: None)
    target = tmp_path / "restored"
    restore(services["settings"].data_dir / "backups" / backup["filename"], target)
    recovered = create_app(Settings(target.resolve(), worker_enabled=False)).state.services
    assert recovered["archive_tree"].sync(force=True)["file_count"] == 3
    assert digest(managed_file(recovered, "photo:" + p["id"])) == p["blob_hash"]
    assert digest(managed_file(recovered, "package:" + package["id"])) == package["zip_hash"]


def test_live_worker_and_manual_api_sync(services, client, photo, monkeypatch):
    p = photo(stage="survey")
    tree = services["archive_tree"]
    tree.start()
    try:
        limit = time.monotonic() + 8
        while tree.status()["state"] != "ready" and time.monotonic() < limit:
            time.sleep(0.05)
        assert managed_file(services, "photo:" + p["id"]).is_file()
        response = client.patch("/api/photos/" + p["id"], json={"stage": "work"})
        assert response.status_code == 200
        limit = time.monotonic() + 8
        while tree.status()["pending"] and time.monotonic() < limit:
            time.sleep(0.05)
        assert managed_file(services, "photo:" + p["id"]).parent.name == "施工"
    finally:
        tree.stop()
    queued = client.post("/api/archive-tree/sync").json()
    assert services["jobs"].run_one()
    assert services["jobs"].get(queued["id"])["state"] == "completed"
    assert client.get("/api/archive-tree").json()["file_count"] == 1
    if os.name == "nt":
        opened = []
        monkeypatch.setattr(os, "startfile", lambda path, operation: opened.append((path, operation)))
        assert client.post("/api/archive-tree/open").status_code == 200
        assert opened == [(str(tree.root), "explore")]
    with TestClient(client.app) as untrusted:
        assert untrusted.post("/api/archive-tree/open").status_code == 403


def test_case_only_rename_updates_windows_names_without_duplicate_copies(services, case, photo):
    domain, tree = services["domain"], services["archive_tree"]
    domain.save_project({"name": "ProjectA"}, case["project_id"])
    domain.save_case({"title": "Crack"}, case["id"])
    p = photo(stage="work", name="Photo.png")
    tree.sync()
    domain.save_project({"name": "projecta"}, case["project_id"])
    domain.save_case({"title": "crack"}, case["id"])
    result = tree.sync()
    assert result["state"] == "completed", result["errors"]
    assert [f.name for f in tree.root.iterdir() if f.name.lower() == "projecta"] == ["projecta"]
    files = list(tree.root.rglob("*.png"))
    assert len(files) == 1 and "crack" in files[0].parent.parent.name
    assert digest(files[0]) == p["blob_hash"]
    assert len(services["db"].all("SELECT * FROM archive_tree_entries WHERE kind='file'")) == 1


def test_change_during_sync_is_still_pending_and_applied_next_pass(services, photo, monkeypatch):
    p = photo(stage="survey")
    tree = services["archive_tree"]
    original = tree._file
    changed = False
    def update_during_copy(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            services["domain"].update_photo(p["id"], {"stage": "work"})
        return original(*args, **kwargs)
    monkeypatch.setattr(tree, "_file", update_during_copy)
    tree.sync()
    assert tree.status()["pending"]
    assert managed_file(services, "photo:" + p["id"]).parent.name == "查勘"
    tree.sync()
    assert not tree.status()["pending"]
    assert managed_file(services, "photo:" + p["id"]).parent.name == "施工"
