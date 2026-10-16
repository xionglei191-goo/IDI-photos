from fastapi import APIRouter, Body

from .domain import DomainError


def workflow_routes(workflow, reviews):
    router = APIRouter(prefix='/api/workflow')

    @router.get('/board')
    def board(board: str = 'intake', project_id: str = '', building: str = '', query: str = '', status: str = '',
              archived: bool = False, page: int = 1, page_size: int = 30, source: str = 'auto'):
        return workflow.board(board, project_id, building, query, status, archived, page, page_size, source)

    @router.get('/sync')
    def sync_status():
        return workflow.sync_status()

    @router.post('/sync')
    def sync(data: dict = Body(default={})):
        if not workflow.platform.client.status()['connected']:
            raise DomainError('请先连接官网再同步台账', 401)
        active = workflow.db.one("SELECT id FROM jobs WHERE kind='platform_sync' AND state IN ('queued','running')")
        if active:
            return workflow.jobs.get(active['id'])
        return workflow.jobs.enqueue('platform_sync', {'refresh_details': bool(data.get('refresh_details', False))})

    @router.get('/cases/{case_id}')
    def detail(case_id: str):
        return workflow.detail(case_id)

    @router.post('/cases/{case_id}/refresh')
    def refresh(case_id: str):
        return workflow.refresh_case(case_id)

    @router.post('/cases/{case_id}/result')
    def record_result(case_id: str, data: dict = Body(...)):
        return workflow.record_result(case_id, data)

    @router.post('/cases/{case_id}/review/{stage}')
    def prepare(case_id: str, stage: str, data: dict = Body(...)):
        return reviews.prepare(case_id, stage, data.get('revision'))

    @router.get('/reviews/{review_id}')
    def review(review_id: str):
        return reviews.get(review_id)

    @router.post('/reviews/{review_id}/confirm')
    def confirm(review_id: str, data: dict = Body(...)):
        if data.get('acknowledged') is not True:
            raise DomainError('请先核对本次全部字段和所选文件')
        return reviews.confirm(review_id, data.get('fingerprint'), data.get('local_only') is True, data.get('action', 'submit'))

    @router.post('/reviews/{review_id}/readback')
    def readback(review_id: str):
        return reviews.reconcile(review_id)

    return router
