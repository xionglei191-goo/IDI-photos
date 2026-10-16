from fastapi import APIRouter, Body


def followup_routes(service):
    router = APIRouter(prefix='/api/followup')

    @router.get('')
    def summary():
        return service.summary()

    @router.get('/issues')
    def issues(project_id: str = ''):
        return service.issues(project_id or None)

    @router.get('/members/{connector_id}')
    def members(connector_id: str):
        return service.members(connector_id)

    @router.put('/contacts/{project_id}')
    def contact(project_id: str, data: dict = Body(...)):
        return service.save_contact(project_id, data)

    @router.put('/config')
    def config(data: dict = Body(...)):
        return service.save_config(data)

    @router.post('/review')
    def review():
        return service.request_review()

    @router.post('/issues/{issue_id}/dismiss')
    def dismiss(issue_id: str, data: dict = Body(...)):
        service.dismiss(issue_id, data.get('reason'))
        return {'saved': True}

    @router.post('/issues/{issue_id}/confirm')
    def confirm_issue(issue_id: str, data: dict = Body(...)):
        return service.confirm_issue(issue_id, data)

    @router.get('/feedback')
    def feedback():
        return service.feedback()

    @router.get('/feedback/preview/{project_id}')
    def preview(project_id: str):
        return service.preview_feedback(project_id)

    @router.post('/feedback/{project_id}')
    def send(project_id: str, data: dict = Body(...)):
        return service.queue_feedback(project_id, preview_token=data.get('preview_token', ''))

    @router.post('/feedback/{delivery_id}/retry')
    def retry(delivery_id: str):
        service.retry_feedback(delivery_id)
        return {'queued': True}

    @router.post('/feedback/{delivery_id}/confirm')
    def confirm(delivery_id: str, data: dict = Body(...)):
        return service.confirm_feedback(delivery_id, data.get('received'))

    return router
