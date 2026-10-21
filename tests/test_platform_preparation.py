import hashlib
import io
import json
import subprocess
import zipfile
from copy import deepcopy

import httpx
import pytest

from idi.domain import DomainError
from idi.platform_client import PlatformClient
from idi.platform_contract import BASE_URL
from tests.conftest import image_bytes


REMOTE_ID = "2110638801184980999"
SURVEY_ID = "2110638801184981001"
REPAIR_ID = "2110638801184981002"


def remote_file(file_id="old-1", name="已有关联.jpg"):
    return {"id": file_id, "fileName": name, "fileUrl": "/profile/verified-file.jpg", "createDate": "2026-09-21 10:00:00"}


@pytest.fixture
def platform(services, case):
    service = services["platform"]
    service.client.close()
    # These contract tests exercise a single mocked website client. Routing
    # between real project clients is covered by test_project_accounts.
    service.project_accounts = None
    requests = []
    row = {"reportReceivingId": REMOTE_ID, "reportCode": "ZLBH20260001", "reportNumber": "0001234567890123456789",
           "communityName": "从化铂悦府", "communityDesc": "1栋201", "accidentDesc": "墙面起皮",
           "siteSurveyId": SURVEY_ID, "siteSurveyState": "1", "repairMethod": "2",
           "repairAcceptanceId": REPAIR_ID, "repairAcceptanceState": "0", "repairAcceptanceFlowStatus": "0"}
    claim = {**row, "communityInfoId": "community-1", "engineeringModelingParentId": "building-1", "repairUnitId": "unit-1", "repairUnit": "测试维保单位",
             "bankAccount": "SHOULD-NOT-BE-STORED", "secretToken": "SHOULD-NOT-BE-STORED"}
    survey = {"reportReceivingId": REMOTE_ID, "siteSurveyId": SURVEY_ID, "siteSurveyDate": "2026-09-21 10:00:00",
              "buildingFileInfoList": [remote_file("survey-building")], "houseFileInfoList": [remote_file("survey-door")],
              "fileInfoList": [remote_file("defect")], "defectDesc": "按实际查勘记录填写的原因", "defectiveSite": "客厅",
              "divisionWorksCode": "part_engineering_03", "divisionWorks": "装饰装修工程", "defectTypeCode": "defect_type_other", "defectType": "其他",
              "repairMethod": "2", "planRepairDate": "2026-09-22", "repairUserId": "person-1", "repairPersonName": "甲",
              "estimateRestorationCostCode": "estimate_restoration_cost_01", "estimateRestorationCost": "3万以下"}
    repair = {"reportReceivingId": REMOTE_ID, "repairAcceptanceId": REPAIR_ID,
              "buildingFileInfoList": [], "houseFileInfoList": [], "preRepairFileInfo": [remote_file("defect")],
              "recoveredFileInfo": [], "supplementaryMaterialsFileInfo": [{"customTypeCode": "supplementary_material", "noRequire": True}]}
    personnel = [{"userId": "person-1", "name": "甲", "phone": "13800000001"}, {"userId": "person-2", "name": "乙", "mobilePhone": "13800000002"}]
    def transport(request):
        requests.append((request.url.path, json.loads(request.content)))
        path = request.url.path
        if path.endswith(("getThirdPartyRepairList", "getAttachmentSurveyList")):
            data = [row]
        elif path.endswith("reportReceiving/getInfo/" + REMOTE_ID):
            data = claim
        elif path.endswith("siteSurvey/getInfo/" + SURVEY_ID):
            data = survey
        elif path.endswith("repairAcceptance/getInfo/" + REPAIR_ID):
            data = repair
        elif path.endswith("getMaintenancePersonnelOrUnitPerson"):
            data = personnel
        else:
            pytest.fail("Unexpected remote endpoint: " + path)
        return httpx.Response(200, json={"code": 0, "data": data, "count": 1})
    service.client = PlatformClient(httpx.MockTransport(transport))
    service.client._token = "only-a-test-token"
    service.client._account = "test-account"
    service.bind(case["id"], {"report_receiving_id": REMOTE_ID, "revision": 0})
    service.test_requests, service.test_row, service.test_claim, service.test_survey, service.test_repair = requests, row, claim, survey, repair
    service.test_personnel = personnel
    return service


def draft_data(service, case_id, stage, values=None, files=None):
    draft = service.draft(case_id, stage)
    return {"values": values if values is not None else draft["values"], "files": files or {}, "revision": draft["revision"], "link_revision": service.link(case_id)["revision"]}


def enter_stage(service, case_id, stage):
    """Read an explicit official node before testing that node's editable form."""
    state = {
        'intake': {'flowStatus': '1'},
        'insurer_review': {'flowStatus': '3'},
        'survey': {'flowStatus': '2', 'siteSurveyState': '0', 'repairAcceptanceFlowStatus': '0'},
        'repair': {'flowStatus': '9', 'siteSurveyState': '1', 'repairAcceptanceFlowStatus': '0'},
        'acceptance': {'flowStatus': '9', 'siteSurveyState': '1', 'repairAcceptanceFlowStatus': '1'},
        'settlement': {'flowStatus': '9', 'siteSurveyState': '1', 'repairAcceptanceFlowStatus': '2'},
        'closed': {'flowStatus': '9', 'siteSurveyState': '1', 'repairAcceptanceFlowStatus': '9'},
    }[stage]
    service.test_row.update(state)
    return service.workflow.refresh_case(case_id)


def test_offline_candidate_lists_are_not_uploads_or_acceptance(services, case, photo):
    p = photo(stage="acceptance", role="finished")
    service = services["platform"]
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair' WHERE id=?", (case['id'],))
    repair = service.preview(case["id"], "repair")
    recovered = next(f for f in repair["file_fields"] if f["key"] == "recoveredFileInfo")
    assert recovered["suggested_ids"] == [p["id"]]
    assert [p['id'] for p in recovered["selected"]] == [p["id"]]
    acceptance = service.preview(case["id"], "acceptance")
    assert not next(f for f in acceptance["file_fields"] if f["key"] == "acceptanceCheckFileInfo")["suggested_ids"]
    assert acceptance["values"].get("acceptanceDate", "") == ""
    assert acceptance["error_count"] > 0 and acceptance["write_enabled"] is False
    assert service.client.status()["connected"] is False
    assert services["domain"].case(case["id"])["material_number"] is None


def test_binding_retains_string_ids_and_excludes_unneeded_data(platform, case, services):
    link = platform.link(case["id"])
    assert link["report_receiving_id"] == REMOTE_ID
    assert link["insurer_report_number"] == "0001234567890123456789"
    assert link["platform_report_code"] == "ZLBH20260001"
    assert "SHOULD-NOT-BE-STORED" not in json.dumps(link)
    assert link["verified_at"]
    other = services["domain"].save_case({"project_id": case["project_id"], "title": "同一户另一案件", "room": "201"})
    with pytest.raises(DomainError, match="已经关联"):
        platform.save_link(other["id"], {"report_receiving_id": REMOTE_ID, "revision": 0})
    assert services["domain"].case(case["id"])["report_number"] == "0001234567890123456789"


def test_wrong_readback_case_is_not_bound(platform, services, case):
    other = services["domain"].save_case({"project_id": case["project_id"], "title": "临时本地案"})
    platform.test_claim["reportReceivingId"] = "another-id"
    with pytest.raises(DomainError, match="详情 ID"):
        platform.bind(other["id"], {"report_receiving_id": REMOTE_ID, "revision": 0})
    assert platform.link(other["id"])["revision"] == 0


def test_survey_and_repair_checkins_do_not_cross_stages(platform, case):
    plan = platform.preview(case["id"], "repair")
    fields = {f["key"]: f for f in plan["file_fields"]}
    assert not fields["buildingFileInfoList"]["existing"] and not fields["houseFileInfoList"]["existing"]
    assert fields["preRepairFileInfo"]["existing"][0]["id"] == "defect"
    assert fields["preRepairFileInfo"]["readonly"]
    assert not fields["supplementaryMaterialsFileInfo"]["existing"]  # Empty website rows aren't files.


def test_survey_personnel_and_dictionary_codes_build_exact_business_fields(platform, case):
    enter_stage(platform, case['id'], 'survey')
    data = draft_data(platform, case["id"], "survey")
    data["values"]["repairUserId"] = "person-2,person-1"
    result = platform.save_draft(case["id"], "survey", data)
    assert result["error_count"] == 0
    payload = platform.build_payload(result, "save", {})
    assert payload["state"] == 0
    assert payload["siteSurveyId"] == SURVEY_ID and payload["reportReceivingId"] == REMOTE_ID
    assert payload["repairPersonName"] == "乙,甲" and payload["repairPersonPhone"] == "13800000002,13800000001"
    assert payload["divisionWorks"] == "装饰装修工程"
    assert payload["estimateRestorationCost"] == "3万以下"
    assert "passStatus" not in payload and "opinion" not in payload
    assert payload["fileInfoList"][0]["id"] == "defect"
    assert all("upload" not in p.lower() and "addOrEdit" not in p for p, _ in platform.test_requests)


def test_zero_rejection_hides_stale_repair_fields_and_requires_reason(platform, case):
    enter_stage(platform, case['id'], 'survey')
    data = draft_data(platform, case["id"], "survey")
    data["values"].update(repairMethod="3", zeroRejectReason="", repairUserId="unknown-person")
    plan = platform.preview(case["id"], "survey", data)
    assert any(i["field"] == "zeroRejectReason" for i in plan["issues"])
    assert not any(i["field"] == "repairUserId" for i in plan["issues"])
    data["values"]["zeroRejectReason"] = "由真实业务核定的原因"
    plan = platform.preview(case["id"], "survey", data)
    assert plan["error_count"] == 0
    payload = platform.build_payload(plan, "submit", {})
    assert payload["state"] == 1
    assert not {"planRepairDate", "repairUserId", "repairPersonName", "estimateRestorationCostCode"} & payload.keys()


def test_intake_changed_time_and_branch_cleanup(platform, case):
    enter_stage(platform, case['id'], 'intake')
    values = {"acceptanceResult": "1", "reasonDescription": "实际受理说明", "modifySurveyTimeMark": "1", "modifiedSurveyTime": "2026-10-15T10:00"}
    data = draft_data(platform, case["id"], "intake", values)
    assert any(i["field"] == "modifiedSurveyTimeEnd" for i in platform.preview(case["id"], "intake", data)["issues"])
    values["modifiedSurveyTimeEnd"] = "2026-10-15T09:00"
    assert any("不能早于" in i["message"] for i in platform.preview(case["id"], "intake", data)["issues"])
    values["acceptanceResult"] = "2"
    plan = platform.preview(case["id"], "intake", data)
    # Hidden fields must neither validate nor enter the payload.
    assert not any(i["field"] == "modifiedSurveyTimeEnd" for i in plan["issues"])
    payload = platform.build_payload(plan, "submit", {})
    assert payload == {"acceptanceResult": "2", "reasonDescription": "实际受理说明", "reportReceivingId": REMOTE_ID, "state": 1}
    with pytest.raises(DomainError, match="没有对应"):
        platform.build_payload(plan, "save", {})


def test_owner_pdf_format_scope_and_exact_type_code(platform, services, case, photo):
    enter_stage(platform, case['id'], 'acceptance')
    image = photo(stage="acceptance", role="finished")
    data = draft_data(platform, case["id"], "acceptance", {"acceptanceDate": "2026-09-24T16:30"}, {"acceptanceCheckFileInfo": [image["id"]]})
    assert any("不接受" in i["message"] for i in platform.preview(case["id"], "acceptance", data)["issues"])
    pdf = services["importer"].add_file(b"%PDF-1.4\nlocal test fixture\n%%EOF", "业主验收材料.pdf", case_id=case["id"])
    data["files"]["acceptanceCheckFileInfo"] = [pdf["photo_id"]]
    plan = platform.save_draft(case["id"], "acceptance", data)
    assert plan["error_count"] == 0
    with pytest.raises(DomainError, match="尚未获得"):
        platform.build_payload(plan, "submit", {})
    payload = platform.build_payload(plan, "submit", {pdf["photo_id"]: remote_file("pdf-1", "验收材料.pdf")})
    assert set(payload) == {"repairAcceptanceId", "acceptanceDate", "acceptanceCheckFileInfo", "supplementaryMaterialsFileInfo"}
    assert payload["acceptanceCheckFileInfo"][0]["customTypeCode"] == "owner_cceptance_materials"
    assert "state" not in payload


def test_false_pdf_and_cross_case_selection_rejected(platform, services, case):
    enter_stage(platform, case['id'], 'acceptance')
    fake = services["importer"].add_file(b"not a PDF", "文件.pdf", case_id=case["id"])
    data = draft_data(platform, case["id"], "acceptance", {"acceptanceDate": "2026-09-24T16:30"}, {"acceptanceCheckFileInfo": [fake["photo_id"]]})
    assert any("无法识别为 PDF" in i["message"] for i in platform.preview(case["id"], "acceptance", data)["issues"])
    other = services["domain"].save_case({"project_id": case["project_id"], "title": "另一案件"})
    services["domain"].update_photo(fake["photo_id"], {"case_id": other["id"]})
    with pytest.raises(DomainError, match="不属于"):
        platform.save_draft(case["id"], "acceptance", data)


def test_repair_duration_and_local_dates_do_not_use_upload_dates(platform, case, photo):
    building = photo(color="red", stage="work", role="building_checkin")
    door = photo(color="green", stage="work", role="door_checkin")
    finished = photo(color="blue", stage="acceptance", role="finished")
    files = {"buildingFileInfoList": [building["id"]], "houseFileInfoList": [door["id"]], "recoveredFileInfo": [finished["id"]]}
    data = draft_data(platform, case["id"], "repair", {"repairCrewNumber": "2"}, files)
    initial = platform.preview(case["id"], "repair", data)
    assert "approachStartWorkDate" not in initial["values"]
    data["values"].update(approachStartWorkDate="2026-09-22T15:00", finishWorkDate="2026-09-23T10:00")
    plan = platform.save_draft(case["id"], "repair", data)
    assert plan["error_count"] == 0 and plan["values"]["timeLimit"] == 2
    assert not any("APP" in i["message"] for i in plan["issues"])
    data["values"]["finishWorkDate"] = "2026-09-21T10:00"
    assert any("不能早于" in i["message"] for i in platform.preview(case["id"], "repair", data)["issues"])


def test_same_day_does_not_offer_supplementary_pdf(platform, case, services):
    platform.test_row["repairMethod"] = "1"
    platform.bind(case["id"], {"report_receiving_id": REMOTE_ID, "revision": 1})
    pdf = services["importer"].add_file(b"%PDF-1.4\nfixture\n%%EOF", "补充.pdf", case_id=case["id"])
    data = draft_data(platform, case["id"], "repair", {}, {"supplementaryMaterialsFileInfo": [pdf["photo_id"]]})
    plan = platform.preview(case["id"], "repair", data)
    assert not any(f["key"] == "supplementaryMaterialsFileInfo" for f in plan["file_fields"])
    assert any(i["field"] == "hidden_files" for i in plan["issues"])


def test_invalid_dict_personnel_dates_and_readonly_field_injection(platform, case):
    enter_stage(platform, case['id'], 'survey')
    data = draft_data(platform, case["id"], "survey")
    data["values"].update(divisionWorksCode="made-up", repairUserId="unknown", siteSurveyDate="not-a-date")
    plan = platform.preview(case["id"], "survey", data)
    assert {"divisionWorksCode", "repairUserId", "siteSurveyDate"}.issubset({i["field"] for i in plan["issues"] if i["severity"] == "error"})
    data["values"]["passStatus"] = "1"
    with pytest.raises(DomainError, match="不属于此环节"):
        platform.save_draft(case["id"], "survey", data)
    enter_stage(platform, case['id'], 'repair')
    with pytest.raises(DomainError, match="不属于此环节"):
        platform.save_draft(case["id"], "repair", draft_data(platform, case["id"], "repair", {}, {"preRepairFileInfo": []}))


def test_draft_revision_and_link_revision_protect_concurrent_edits(platform, case):
    enter_stage(platform, case['id'], 'intake')
    data = draft_data(platform, case["id"], "intake", {"acceptanceResult": "2", "reasonDescription": "受理依据"})
    platform.save_draft(case["id"], "intake", data)
    with pytest.raises(DomainError, match="草稿已被修改"):
        platform.save_draft(case["id"], "intake", data)
    data["revision"] = 1
    platform.test_claim['reasonDescription'] = '官网业务内容发生了变化'
    platform.bind(case["id"], {"report_receiving_id": REMOTE_ID, "revision": platform.link(case['id'])['revision']})
    with pytest.raises(DomainError, match="官网关联已变化"):
        platform.save_draft(case["id"], "intake", data)


def test_preparation_export_preserves_bytes_history_tree_and_backup(platform, services, case, photo, client):
    enter_stage(platform, case['id'], 'survey')
    p = photo(stage="survey", role="problem")
    data = draft_data(platform, case["id"], "survey", files={"fileInfoList": [p["id"]]})
    saved = platform.save_draft(case["id"], "survey", data)
    package = platform.package(case["id"], "survey", saved["draft"]["revision"])
    response = client.get(package["download_url"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("字段清单.json"))
        item = manifest["files"][0]
        assert item["field"] == "fileInfoList" and item["path"].startswith("缺陷照片_视频/")
        assert hashlib.sha256(archive.read(item["path"])).hexdigest() == p["blob_hash"]
        assert manifest["remote_written"] is False
        assert "only-a-test-token" not in archive.read("字段清单.json").decode()
    services["domain"].save_case({"title": "后续修改的标题"}, case["id"])
    assert client.get(package["download_url"]).content == response.content
    tree = services["archive_tree"].sync()
    assert tree["error_count"] == 0
    assert any("官网资料准备" in p["path"] for p in services["db"].all("SELECT path FROM archive_tree_entries"))
    backup = services["reporter"].backup({}, lambda *_: None)
    path = services["settings"].data_dir / "backups" / backup["filename"]
    with zipfile.ZipFile(path) as archive:
        assert f"exports/{package['id']}/platform-materials.zip" in archive.namelist()


def test_api_preview_and_prepare_do_not_write_platform(platform, client, case):
    response = client.get(f"/api/platform/cases/{case['id']}/stages/acceptance")
    assert response.status_code == 200 and response.json()["write_enabled"] is False
    assert client.post("/api/platform/submit", json={"state": 1}).status_code in (404, 405)
    for path, body in platform.test_requests:
        assert "/get" in path and "upload" not in path and "addOrEdit" not in path
    assert client.post("/api/platform/session", json={}, headers={"X-IDI-Request": "0"}).status_code == 403


def test_readonly_client_rejects_unknown_endpoints_and_clears_expired_session():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"code": 2, "msg": "expired"})
    client = PlatformClient(httpx.MockTransport(handler))
    client._token = "test-secret"
    with pytest.raises(DomainError, match="只读接口"):
        client.read("/insurance/sysFileInfo/upload", {})
    assert not calls
    with pytest.raises(DomainError, match="已过期"):
        client.search()
    assert client.status()["connected"] is False
    client.close()


def test_login_uses_normal_browser_and_secrets_only_in_pipe(monkeypatch):
    captured = {}
    def run(args, **kwargs):
        captured.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"token": "test-token"}), stderr="")
    monkeypatch.setattr("idi.platform_client.subprocess.run", run)
    monkeypatch.setattr("idi.platform_client.shutil.which", lambda _: "node")
    client = PlatformClient(httpx.MockTransport(lambda _: pytest.fail("login should use normal browser")))
    result = client.login("user-example", "password-example")
    assert result["connected"] and "token" not in result and "password" not in result
    assert "password-example" not in " ".join(captured["args"])
    assert json.loads(captured["input"])["password"] == "password-example"
    assert captured["capture_output"] and captured["timeout"] == 100
    client.close()


@pytest.mark.parametrize("status,failure,detail", [
    (401, "login_rejected", "官网登录未通过：账号或密码错误"),
    (429, "rate_limited", "官网暂时限制登录请求，请稍后再试。"),
    (504, "token_missing", "官网已响应，但未取得有效登录令牌或失败提示。"),
    (503, "browser_unavailable", "官网登录浏览器未能启动。"),
])
def test_login_failure_preserves_reason_without_logging_credentials(monkeypatch, caplog, status, failure, detail):
    receipt = {"error": detail, "status": status, "phase": "token_response", "diagnostics": {
        "requestSent": True, "httpStatus": 200, "businessCode": 1, "failure": failure,
        "private": "password-example", "response": {"token": "secret-token"}}}
    monkeypatch.setattr("idi.platform_client.subprocess.run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout=json.dumps(receipt), stderr=""))
    monkeypatch.setattr("idi.platform_client.shutil.which", lambda _: "node")
    client = PlatformClient(httpx.MockTransport(lambda _: pytest.fail("no direct login request")))
    client._token = "previous-token"
    try:
        with pytest.raises(DomainError) as caught:
            client.login("user-example", "password-example")
        assert caught.value.status == status and str(caught.value) == detail
        assert not client.status()["connected"]
        assert "http=200 code=1" in caplog.text
        assert all(secret not in caplog.text for secret in ("password-example", "secret-token", "previous-token", "user-example"))
    finally:
        client.close()


@pytest.mark.parametrize("output", ["not-json", "[]", "null", '{"token":"undefined"}', '{"token":"null"}'])
def test_login_malformed_receipt_never_connects(monkeypatch, output):
    monkeypatch.setattr("idi.platform_client.subprocess.run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=output, stderr=""))
    monkeypatch.setattr("idi.platform_client.shutil.which", lambda _: "node")
    client = PlatformClient()
    try:
        with pytest.raises(DomainError) as caught:
            client.login("user-example", "password-example")
        assert caught.value.status == 502 and not client.status()["connected"]
    finally:
        client.close()


def test_login_subprocess_timeout_is_distinct_from_rejection(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 100)
    monkeypatch.setattr("idi.platform_client.subprocess.run", run)
    monkeypatch.setattr("idi.platform_client.shutil.which", lambda _: "node")
    client = PlatformClient()
    try:
        with pytest.raises(DomainError) as caught:
            client.login("user-example", "password-example")
        assert caught.value.status == 504 and "超时" in str(caught.value)
    finally:
        client.close()
