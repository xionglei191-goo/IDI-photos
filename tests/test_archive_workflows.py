import hashlib
import io
import json
import os
import time
import zipfile

import pytest
from openpyxl import Workbook

from idi.api import create_app
from idi.cli import restore
from idi.config import Settings
from idi.db import uid
from idi.domain import DomainError
from idi.recognition import extract_fields
from conftest import image_bytes


def run_jobs(services):
    for _ in range(20):
        if not services["jobs"].run_one():
            return
    raise AssertionError("unexpected continuing queue")


def test_same_household_cases_keep_independent_ids_and_optional_numbers(client, case, project):
    second = client.post("/api/cases", json={"project_id": project["id"], "title": "卫生间渗漏", "building": "1", "room": "201", "material_number": "IGNORED"}).json()
    assert second["id"] != case["id"]
    assert second["report_number"] is None and second["material_number"] is None
    assert client.get("/api/cases").json()[0]["status"] == "pending"
    number = "01234567890123456789"
    saved = client.patch(f"/api/cases/{second['id']}", json={"report_number": number, "revision": second["revision"]})
    assert saved.status_code == 200 and saved.json()["report_number"] == number
    assert client.patch(f"/api/cases/{second['id']}", json={"title": "过期修改", "revision": second["revision"]}).status_code == 409
    assert client.patch(f"/api/cases/{case['id']}", json={"report_number": number}).status_code == 200
    assert client.patch(f"/api/cases/{case['id']}", json={"platform_report_code": "BX-UNIQUE"}).status_code == 200
    assert client.patch(f"/api/cases/{second['id']}", json={"platform_report_code": "BX-UNIQUE"}).status_code == 409
    assert client.get(f"/api/cases/{case['id']}").json()["title"] == case["title"]


def test_duplicate_original_retains_sources_and_cross_case_membership(services, case, project):
    importer, db = services["importer"], services["db"]
    first = importer.add_file(image_bytes(), "首次.png", case_id=case["id"], source_path="first/首次.png")
    repeated = importer.add_file(image_bytes(), "转发.png", case_id=case["id"], source_path="second/转发.png")
    assert repeated["duplicate"] and first["photo_id"] == repeated["photo_id"]
    assert len(services["domain"].photo(first["photo_id"])["sources"]) == 2
    other_case = services["domain"].save_case({"project_id": project["id"], "title": "另一次维修", "building": "1", "room": "201"})
    other = importer.add_file(image_bytes(), "另一案件.png", case_id=other_case["id"])
    assert other["photo_id"] != first["photo_id"]
    assert db.one("SELECT COUNT(*) AS n FROM blobs")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM photos")["n"] == 2


def test_upload_partial_retry_and_original_download(client, services, case):
    response = client.post("/api/import/upload", data={"case_id": case["id"], "auto_ocr": "false"}, files=[("files", ("good.png", image_bytes(), "image/png")), ("files", ("broken.jpg", b"not-an-image", "image/jpeg"))])
    assert response.status_code == 200
    payload = response.json()
    run_jobs(services)
    result = client.get(f"/api/jobs/{payload['job']['id']}").json()
    assert result["state"] == "partial" and result["result"]["failed"] == 1
    batch = client.get(f"/api/imports/{payload['batch_id']}").json()
    assert batch["imported"] == 1 and batch["failed"] == 1
    photo_id = result["result"]["photo_ids"][0]
    assert client.get(f"/api/photos/{photo_id}/original").content == image_bytes()
    assert client.get(f"/api/photos/{photo_id}/thumbnail").headers["content-type"] == "image/jpeg"
    assert client.post(f"/api/jobs/{payload['job']['id']}/retry").status_code == 200
    run_jobs(services)
    assert client.get("/api/photos").json()["total"] == 1
    assert client.get(f"/api/imports/{payload['batch_id']}").json()["failed"] == 1


def test_different_files_with_identical_names_are_both_imported(client, services, case):
    payload = client.post("/api/import/upload", data={"case_id": case["id"], "auto_ocr": "false"}, files=[("files", ("image.png", image_bytes(), "image/png")), ("files", ("image.png", image_bytes("red"), "image/png"))]).json()
    run_jobs(services)
    batch = client.get(f"/api/imports/{payload['batch_id']}").json()
    assert batch["imported"] == 2 and batch["failed"] == 0
    photos = client.get("/api/photos").json()["items"]
    assert len(photos) == 2 and len({p["blob_hash"] for p in photos}) == 2
    assert {p["original_name"] for p in photos} == {"image.png"}


@pytest.mark.parametrize("unsafe", ["../escape.png", "/escape.png", "C:/escape.png", "safe/../../escape.png"])
def test_zip_traversal_is_rejected_without_external_writes(services, tmp_path, unsafe):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(unsafe, image_bytes())
    with pytest.raises(DomainError, match="不安全"):
        services["importer"].stage_zip(source)
    assert not (tmp_path / "escape.png").exists()
    assert services["domain"].summary()["total"] == 0


def test_zip_preserves_nested_stage_and_ignores_unsupported_files(client, services, case):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("查勘照片/问题.png", image_bytes())
        archive.writestr("施工照片/过程.png", image_bytes("red"))
        archive.writestr("ignored.exe", b"ignore")
    response = client.post("/api/import/zip", data={"case_id": case["id"], "auto_ocr": "false"}, files={"file": ("资料.zip", stream.getvalue(), "application/zip")})
    assert response.status_code == 200
    run_jobs(services)
    photos = client.get("/api/photos").json()["items"]
    assert {(p["stage"], p["role"]) for p in photos} == {("survey", "problem"), ("work", "construction")}


def test_watermark_and_filename_time_are_independent(services, case, monkeypatch):
    text = "碧桂园铂悦府\n2026.9.22 16:50\n施工区域：1栋201客厅沙发旁\n施工内容：打磨刷油漆"
    extracted = extract_fields(text)
    assert extracted["watermark_time"] == "2026-09-22 16:50"
    assert extracted["building"] == "1" and extracted["room"] == "201" and extracted["part"] == "客厅沙发旁"
    imported = services["importer"].add_file(image_bytes(), "微信图片_20260922175333.png", project_id=case["project_id"])
    monkeypatch.setattr(services["recognizer"], "local_ocr", lambda path: (text, []))
    result = services["recognizer"].recognize(imported["photo_id"])
    row = services["domain"].photo(imported["photo_id"])
    assert result["auto_assigned"] and row["case_id"] == case["id"]
    assert row["watermark_time"].endswith("16:50")
    assert row["exif_time"] is None and row["message_time"] is None
    assert services["domain"].case(case["id"])["status"] == "pending"


def test_ambiguous_same_household_remains_unassigned_and_manual_fields_survive(services, case, monkeypatch):
    services["domain"].save_case({"project_id": case["project_id"], "title": "另一处油漆起皮", "building": "1", "room": "201"})
    imported = services["importer"].add_file(image_bytes(), "待核对.png", project_id=case["project_id"])
    text = "2026.9.22 16:50\n施工区域：1栋201客厅\n施工内容：打磨刷油漆"
    monkeypatch.setattr(services["recognizer"], "local_ocr", lambda path: (text, []))
    services["recognizer"].recognize(imported["photo_id"])
    row = services["domain"].photo(imported["photo_id"])
    assert row["case_id"] is None and len(row["recognitions"][0]["candidates"]) == 2
    services["domain"].update_photo(row["id"], {"case_id": case["id"], "part": "人工核对位置", "stage": "survey", "confirm": True})
    services["recognizer"].recognize(row["id"])
    changed = services["domain"].photo(row["id"])
    assert changed["part"] == "人工核对位置" and changed["stage"] == "survey"
    assert changed["recognition_state"] == "confirmed"


def test_same_day_checkin_does_not_cover_another_visit(services, case, photo):
    photo("navy", stage="work", role="door_checkin", watermark_time="2026-09-19 08:49")
    second = photo("red", stage="work", role="construction", watermark_time="2026-09-22 16:50")
    quality = services["domain"].completeness(case["id"])
    doors = [x for x in quality["items"] if x["stage"] == "work" and x["role"] == "door_checkin"]
    assert len(doors) == 2
    assert {r["scope_key"]: r["missing"] for r in doors} == {"date:2026-09-19": 0, "date:2026-09-22": 1}
    visit = services["domain"].save_visit(case["id"], {"stage": "work", "visit_date": "2026-09-25", "visit_key": "再次到访"})
    quality = services["domain"].completeness(case["id"])
    assert any(x["scope_key"] == "visit:" + visit["id"] and x["missing"] for x in quality["items"])
    selected = services["domain"].completeness(case["id"], photo_ids=[second["id"]])
    assert selected["total_images"] == 1
    assert not any("2026-09-19" in x["scope"] for x in selected["items"])


def test_cross_case_visit_assignment_and_stale_photo_edits_are_rejected(services, case, photo, client):
    p = photo(stage="work", role="construction")
    other = services["domain"].save_case({"project_id": case["project_id"], "title": "另案"})
    visit = services["domain"].save_visit(other["id"], {"stage": "work", "visit_date": "2026-10-15"})
    assert client.patch(f"/api/photos/{p['id']}", json={"visit_id": visit["id"]}).status_code == 400
    assert client.patch(f"/api/photos/{p['id']}", json={"notes": "已核对", "revision": p["revision"]}).status_code == 200
    assert client.patch(f"/api/photos/{p['id']}", json={"notes": "旧数据", "revision": p["revision"]}).status_code == 409


def test_selected_export_snapshot_receipt_and_supplement(services, case, photo, client):
    services['domain'].save_case({'platform_report_code': 'ZLBH202610132005010577'}, case['id'])
    p = photo(stage="survey", role="problem", part="客厅", process="=HYPERLINK(\"bad\")")
    excluded = photo("red", stage="acceptance", role="finished")
    request = client.post("/api/packages", json={"photo_ids": [p["id"]]})
    assert request.status_code == 200
    run_jobs(services)
    package_id = client.get(f"/api/jobs/{request.json()['id']}").json()["result"]["packages"][0]["id"]
    data = client.get(f"/api/packages/{package_id}/download").content
    original_digest = hashlib.sha256(data).hexdigest()
    manifest = client.get(f"/api/packages/{package_id}").json()['manifest']
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert [x["id"] for x in manifest["photos"]] == [p["id"]]
        assert excluded["id"] not in json.dumps(manifest)
        assert hashlib.sha256(archive.read(manifest["photos"][0]["package_path"])).hexdigest() == p["blob_hash"]
        assert [entry.filename for entry in archive.infolist() if not entry.is_dir()] == [manifest['photos'][0]['package_path']]
    services["domain"].save_case({"title": "后续更名"}, case["id"])
    services["domain"].update_photo(p["id"], {"process": "修改施工内容"})
    receipt = client.post(f"/api/packages/{package_id}/receipt", data={"state": "accepted", "note": "资料已收件", "event_at": "2026-10-15 09:00"}, files={"file": ("回执.txt", b"received", "text/plain")}).json()
    assert receipt["state"] == "accepted" and receipt["manifest"]["case"]["title"] == case["title"]
    assert services["domain"].case(case["id"])["status"] == "pending"
    assert hashlib.sha256(client.get(f"/api/packages/{package_id}/download").content).hexdigest() == original_digest
    assert client.get(f"/api/receipts/{receipt['events'][0]['id']}/attachment").content == b"received"
    second = services["reporter"].build(uid(), case["id"], [excluded["id"]], parent_id=package_id)
    assert second["version"] == 2 and second["manifest"]["parent_id"] == package_id
    old_listed = next(x for x in client.get("/api/packages").json() if x["id"] == package_id)
    assert old_listed["case_title"] == case["title"]
    assert old_listed["project_id"] == case["project_id"]
    for field in ("short_code", "building", "room"):
        assert old_listed[field] == manifest["case"][field]
    assert old_listed["download_url"] == f"/api/packages/{package_id}/download"


def test_incomplete_package_has_no_download_link(services, case, photo, client):
    services['domain'].save_case({'platform_report_code': 'ZLBH202610132005010577'}, case['id'])
    p = photo(stage='survey')
    package_id = uid()
    services["reporter"]._snapshot(package_id, case["id"], [p["id"]], "general", None, "")
    for state in ("building", "failed"):
        with services["db"].tx() as con:
            con.execute("UPDATE packages SET state=? WHERE id=?", (state, package_id))
        listed = client.get("/api/packages", params={"case_id": case["id"]}).json()
        assert len(listed) == 1 and listed[0]["download_url"] is None
        assert client.get(f"/api/packages/{package_id}/download").status_code == 409


def test_unassigned_export_and_cross_case_supplement_are_rejected(services, case, photo):
    services['domain'].save_case({'platform_report_code': 'ZLBH202610132005010577'}, case['id'])
    free = services["importer"].add_file(image_bytes(), "自由.png")
    with pytest.raises(DomainError, match="先确认案件"):
        services["reporter"].preview([free["photo_id"]])
    p = photo("red", stage='survey')
    first = services["reporter"].build(uid(), case["id"], [p["id"]])
    other = services["domain"].save_case({"project_id": case["project_id"], "title": "另案"})
    assigned = services["domain"].update_photo(free["photo_id"], {"case_id": other["id"]})
    with pytest.raises(DomainError, match="同一案件"):
        services["reporter"].build(uid(), other["id"], [assigned["id"]], parent_id=first["id"])


def test_corrupt_archive_is_not_silently_reused(services, case, photo):
    services['domain'].save_case({'platform_report_code': 'ZLBH202610132005010577'}, case['id'])
    p = photo(stage='survey')
    blob = services["db"].one("SELECT * FROM blobs WHERE hash=?", (p["blob_hash"],))
    services["storage"].resolve(blob["stored_path"]).write_bytes(b"damaged")
    with pytest.raises(ValueError, match="哈希"):
        services["importer"].add_file(image_bytes(), "重传.png", case_id=case["id"])
    with pytest.raises(DomainError, match="哈希"):
        services["reporter"].build(uid(), case["id"], [p["id"]])
    with pytest.raises(DomainError, match="哈希"):
        services["reporter"].backup({}, lambda *args: None)


def test_ledger_preview_preserves_text_ids_and_blocks_numeric_precision_loss(client, services, project):
    book = Workbook()
    sheet = book.active
    sheet.append(["案件内容", "报案号", "栋号", "房号", "资料编号", "报案时间"])
    sheet.append(["文本长编号", "01234567890123456789", "1", "201", "SHOULD-NOT-IMPORT", "2026/10/15 08:30"])
    sheet.append(["数值长编号", 1234567890123456789, "1", "201", "", "2026-10-15"])
    sheet.append(["公式编号", '=CONCAT("123","456")', "1", "201"])
    sheet.append(["地下车库渗漏", "", "2", "负二"])
    stream = io.BytesIO()
    book.save(stream)
    preview = client.post("/api/import/ledger-preview", data={"project_id": project["id"]}, files={"file": ("台账.xlsx", stream.getvalue())}).json()
    assert preview["valid"] == 2 and preview["invalid"] == 2
    assert any("精度" in value for value in preview["rows"][1]["errors"])
    assert any("公式" in value for value in preview["rows"][2]["errors"])
    commit = client.post("/api/import/ledger-commit", json={"batch_id": preview["batch_id"]}).json()
    run_jobs(services)
    assert client.get(f"/api/jobs/{commit['job']['id']}").json()["state"] == "partial"
    cases = client.get("/api/cases").json()
    assert len(cases) == 2
    assert next(c for c in cases if c["title"] == "文本长编号")["report_number"] == "01234567890123456789"
    assert all(c["material_number"] is None for c in cases)
    assert next(c for c in cases if c["room"] == "负二")["location_type"] == "public"
    client.post("/api/import/ledger-commit", json={"batch_id": preview["batch_id"]})
    run_jobs(services)
    assert len(client.get("/api/cases").json()) == 2


def test_connector_message_receipt_media_failure_retry_and_dedup(services, case, tmp_path):
    inbox = tmp_path / "feed-inbox"
    inbox.mkdir()
    service = services["connectors"]
    feed = service.save({"name": "文件接收验证", "kind": "feed", "enabled": True, "config": {"path": str(inbox), "case_id": case["id"], "auto_ocr": False}})
    service.ingest(feed["id"], {"source_key": "text-1", "group_name": "项目群", "sender": "师傅", "content": "1栋201油漆维修", "message_type": "text"})
    event = {"source_key": "image-1", "group_name": "项目群", "sender": "师傅", "file_path": str(inbox / "original.png"), "message_type": "image"}
    failed = service.ingest(feed["id"], event)
    assert failed["media_state"] == "failed" and failed["photo_id"] is None
    assert service.get(feed["id"])["counts"]["messages"] == 2
    (inbox / "original.png").write_bytes(image_bytes())
    saved = service.retry_message(failed["message_id"])
    assert saved["media_state"] == "saved"
    assert service.ingest(feed["id"], event)["duplicate"]
    assert service.get(feed["id"])["counts"]["messages"] == 2
    source = services["domain"].photo(saved["photo_id"])["sources"][0]
    assert "1栋201油漆维修" in source["metadata"]["context_text"]
    assert source["message_time"] is None and source["sender"] == "师傅"
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes("red"))
    rejected = service.ingest(feed["id"], {**event, "source_key": "outside", "file_path": str(outside)})
    assert rejected["media_state"] == "failed" and "收件目录" in rejected["error"]


def test_folder_watcher_deduplicates_across_restart(services, case, tmp_path, monkeypatch):
    inbox = tmp_path / "folder-inbox"
    inbox.mkdir()
    path = inbox / "photo.png"
    path.write_bytes(image_bytes())
    os.utime(path, (time.time() - 10, time.time() - 10))
    service = services["connectors"]
    service.save({"name": "收件夹", "kind": "folder", "enabled": True, "config": {"path": str(inbox), "case_id": case["id"], "auto_ocr": False}})
    service.scan_once()
    monkeypatch.setattr('idi.connectors.service.time.monotonic', lambda: 999999999)
    service.scan_once()
    new_app = create_app(services["settings"])
    new_app.state.services["connectors"].scan_once()
    assert new_app.state.services["domain"].summary()["total"] == 1
    assert services["db"].one("SELECT COUNT(*) AS n FROM messages")["n"] == 1


def test_interrupted_job_recovers_and_attempt_count_is_preserved(services):
    jobs = services["jobs"]
    jobs.register("probe", lambda data, progress: {"value": data["value"]})
    job = jobs.enqueue("probe", {"value": "saved"})
    with services["db"].tx() as con:
        con.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (job["id"],))
    jobs.start()
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and jobs.get(job["id"])["state"] != "completed":
            time.sleep(.05)
        final = jobs.get(job["id"])
        assert final["state"] == "completed" and final["attempts"] == 2
        assert final["result"]["value"] == "saved"
    finally:
        jobs.stop()


def test_backup_restore_verifies_originals_packages_and_disables_receivers(services, case, photo, tmp_path):
    services['domain'].save_case({'platform_report_code': 'ZLBH202610132005010577'}, case['id'])
    p = photo(stage="acceptance", role="finished")
    package = services["reporter"].build(uid(), case["id"], [p["id"]])
    feed = services["connectors"].save({"name": "资料入口", "kind": "feed", "enabled": True, "config": {}})
    services["jobs"].enqueue("recognize", {"photo_ids": [p["id"]]})
    backup = services["reporter"].backup({}, lambda *args: None)
    archive = services["settings"].data_dir / "backups" / backup["filename"]
    destination = tmp_path / "restored"
    result = restore(archive, destination)
    assert result["verified_files"] >= 6
    recovered = create_app(Settings(destination, worker_enabled=False)).state.services
    assert recovered["domain"].case(case["id"])["title"] == case["title"]
    assert recovered["domain"].summary()["total"] == 1
    assert not recovered["connectors"].get(feed["id"])["enabled"]
    assert recovered['workflow'].synchronizer.policy()['enabled'] is False
    assert recovered["jobs"].list()[0]["state"] == "failed"
    blob = recovered["db"].one("SELECT * FROM blobs WHERE hash=?", (p["blob_hash"],))
    assert recovered["storage"].verify(blob)
    assert recovered["reporter"].package(package["id"])["manifest"] == package["manifest"]
    with pytest.raises(ValueError, match="空目录"):
        restore(archive, destination)


def test_backup_tampering_rejected_and_data_directory_not_importable(services, tmp_path):
    with pytest.raises(DomainError, match="档案目录"):
        services["importer"].folder_entries(str(services["settings"].data_dir))
    source = tmp_path / "bad-backup.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("archive.sqlite3", b"not-db")
        archive.writestr("backup-metadata.json", json.dumps({"schema": 1, "files": {"archive.sqlite3": "0" * 64}}))
    with pytest.raises(ValueError, match="校验失败"):
        restore(source, tmp_path / "bad-restore")


def test_local_write_boundary_and_unconfigured_external_services(client):
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/projects", json={"name": "bad"}, headers={"X-IDI-Request": "0"}).status_code == 403
    assert client.post("/api/projects", json={"name": "bad"}, headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "unrelated.example"}).status_code == 400
    state = client.get("/api/settings/recognition").json()
    assert not state["vision_ready"] and not state["vision_enabled"]
    assert client.post("/api/recognize", json={"photo_ids": ["unknown"], "use_vision": True}).status_code == 400
    assert client.post("/api/connectors", json={"name": "微信", "kind": "wxauto", "enabled": True, "config": {"groups": []}}).status_code == 400
