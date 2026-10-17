from fastapi import APIRouter, Body

from .domain import DomainError


def agent_routes(service):
    router = APIRouter(prefix='/api/agent')

    @router.get('/cases/{case_id}')
    def history(case_id: str):
        return service.list(case_id)

    @router.post('/cases/{case_id}/review')
    def request(case_id: str, data: dict = Body(default={})):
        if not isinstance(data.get('force', False), bool):
            raise DomainError('重新审阅参数无效')
        return service.request(case_id, force=data.get('force', False))

    @router.get('/runs/{run_id}')
    def get(run_id: str):
        return service.get(run_id)

    @router.post('/runs/{run_id}/cancel')
    def cancel(run_id: str):
        return service.cancel(run_id)

    @router.post('/runs/{run_id}/adopt')
    def adopt(run_id: str, data: dict = Body(...)):
        return service.adopt(run_id, data.get('finding_indexes'))

    return router
