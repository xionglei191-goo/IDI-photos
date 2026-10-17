import hashlib
import json

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from .domain import DomainError
from .storage import safe_name


def platform_routes(service):
    router = APIRouter(prefix="/api/platform")

    @router.get("/contract")
    def contract():
        return service.contract()

    @router.get('/prefill-defaults')
    def prefill_defaults():
        return service.prefill_defaults()

    @router.put('/prefill-defaults')
    def save_prefill_defaults(data: dict = Body(...)):
        return service.save_prefill_defaults(data)

    @router.get("/session")
    def session():
        return service.client.status()

    @router.post("/session")
    def connect(data: dict = Body(...)):
        return service.account.temporary_login(data.get("username"), data.get("password"))

    @router.delete("/session")
    def disconnect():
        return service.account.disconnect()

    @router.get('/account')
    def account():
        return service.account.status()

    @router.put('/account')
    def save_account(data: dict = Body(...)):
        return service.account.configure(data)

    @router.post('/account/connect')
    def connect_account():
        return service.account.request_connect()

    @router.delete('/account')
    def forget_account():
        return service.account.forget()

    @router.post("/dictionaries/refresh")
    def dictionaries():
        return service.refresh_dictionaries()

    @router.get("/remote-cases")
    def remote_cases(query: str = "", page: int = 1, scope: str = "repair"):
        return service.client.search(query, page, scope)

    @router.get("/cases/{case_id}/link")
    def link(case_id: str):
        return service.link(case_id)

    @router.put("/cases/{case_id}/link")
    def record_link(case_id: str, data: dict = Body(...)):
        return service.save_link(case_id, data)

    @router.post("/cases/{case_id}/bind")
    def bind(case_id: str, data: dict = Body(...)):
        return service.bind(case_id, data)

    @router.get("/cases/{case_id}/stages/{stage}")
    def workspace(case_id: str, stage: str):
        return service.preview(case_id, stage)

    @router.post("/cases/{case_id}/stages/{stage}/preview")
    def preview(case_id: str, stage: str, data: dict = Body(...)):
        return service.preview(case_id, stage, data)

    @router.put("/cases/{case_id}/stages/{stage}")
    def save(case_id: str, stage: str, data: dict = Body(...)):
        return service.save_draft(case_id, stage, data)

    @router.post("/cases/{case_id}/stages/{stage}/package")
    def prepare(case_id: str, stage: str, data: dict = Body(...)):
        return service.package(case_id, stage, data.get("revision"))

    @router.get("/cases/{case_id}/packages")
    def packages(case_id: str):
        return service.packages(case_id)

    @router.get("/packages/{package_id}/download")
    def download(package_id: str):
        record = service.db.one("SELECT * FROM platform_packages WHERE id=?", (package_id,))
        if not record:
            raise DomainError("本地官网资料包不存在", 404)
        path = service.storage.resolve(record["zip_path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record["zip_hash"]:
            raise DomainError("本地官网资料包缺失或内容校验失败", 409)
        snapshot = json.loads(record["snapshot"])
        filename = safe_name(snapshot["case"]["title"] + "_" + snapshot["stage_label"]) + "_本地准备.zip"
        return FileResponse(path, media_type="application/zip", filename=filename)

    return router
