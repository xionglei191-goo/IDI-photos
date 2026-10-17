from typing import Literal

from fastapi import APIRouter, Body, Query

from .feishu_history import FeishuHistory


def feishu_agent_routes(service):
    router = APIRouter(prefix='/api/feishu-agent')
    history = FeishuHistory(service)

    @router.get('')
    def status():
        return service.status()

    @router.get('/sessions')
    def sessions(q: str = Query('', max_length=160), kind: Literal['all', 'group', 'brief'] = 'group',
                 attention: bool = False, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
        return history.sessions(query=q, kind=kind, attention=attention, offset=offset, limit=limit)

    @router.get('/sessions/{session_id}')
    def transcript(session_id: str, before: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=50)):
        return history.transcript(session_id, before=before, limit=limit)

    @router.put('/bindings/{connector_id}')
    def configure(connector_id: str, data: dict = Body(...)):
        return service.save_binding(connector_id, data)

    @router.get('/tasks/{task_id}')
    def task(task_id: str):
        return service.get(task_id)

    @router.post('/tasks/{task_id}/retry')
    def retry(task_id: str):
        return service.retry_task(task_id)

    @router.post('/tasks/{task_id}/delivery/retry')
    def retry_delivery(task_id: str):
        return service.retry_delivery(task_id)

    @router.post('/tasks/{task_id}/delivery/confirm')
    def confirm(task_id: str, data: dict = Body(...)):
        return service.confirm_delivery(task_id, data.get('received'))

    @router.get('/projects/{project_id}/brief-preview')
    def preview(project_id: str):
        return service.brief_preview(project_id)

    return router
