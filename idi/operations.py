"""Read-only operational projection; source records remain the authority."""
import json
import shutil

from fastapi import APIRouter, Body

from .db import now
from .domain import DomainError


class Operations:
    def __init__(self, services):
        self.services = services
        self.db = services['db']

    def status(self):
        from .identity import current
        if current() and not current().admin:
            return self.project_status()
        s, db = self.services, self.db
        storage = {'state': 'available', 'detail': '数据库可读；原件在处理和报送前逐件校验'}
        try:
            storage['free_bytes'] = shutil.disk_usage(s['settings'].data_dir).free
            if storage['free_bytes'] < 128 * 1024 * 1024:
                storage.update(state='low_space', detail='磁盘可用空间不足 128 MB，请先释放空间')
        except OSError:
            storage.update(state='unavailable', detail='无法读取资料磁盘状态')
        recognition = s['recognizer'].status()
        local = db.one("SELECT state,error,created_at FROM recognitions WHERE provider='local_ocr' ORDER BY created_at DESC,rowid DESC LIMIT 1")
        channels = [{'id': c['id'], 'name': c['name'], 'kind': c['kind'], 'enabled': c['enabled'], 'state': c['status'],
                     'error': c.get('error'), 'counts': c['counts'], 'capture': c.get('capture'),
                     'last_saved': db.one("SELECT MAX(received_at) value FROM messages WHERE connector_id=? AND media_state='saved'", (c['id'],))['value']} for c in s['connectors'].list()]
        queue = {r['state']: r['n'] for r in db.all('SELECT state,COUNT(*) n FROM photo_ai_tasks GROUP BY state')}
        return {'at': now(), 'worker_enabled': s['settings'].worker_enabled, 'storage': storage,
                'model': recognition['model_health'], 'policy': s['photo_workflow'].policy(),
                'ocr': {'enabled': recognition.get('local_ocr', True), 'installed': recognition['local_ocr_available'],
                        'latest': local, 'pending': db.one("SELECT COUNT(*) n FROM photos p JOIN blobs b ON p.blob_hash=b.hash WHERE b.media_type='image' AND NOT EXISTS(SELECT 1 FROM recognitions r WHERE r.photo_id=p.id AND r.provider='local_ocr' AND r.state='completed')")['n']},
                'platform': s['platform'].client.status(), 'sync': s['workflow'].synchronizer.status(), 'channels': channels,
                'attachments_pending': db.one("SELECT COUNT(*) n FROM messages WHERE media_state IN ('pending','failed')")['n'],
                'rule_states': {r['state']: r['n'] for r in db.all('SELECT state,COUNT(*) n FROM photo_pipeline GROUP BY state')},
                'ai_queue': queue, 'draft_states': {r['state']: r['n'] for r in db.all('SELECT state,COUNT(*) n FROM rule_draft_intents GROUP BY state')},
                'ai_tasks': db.all("SELECT a.id,a.photo_id,a.kind,a.source,a.state,a.attempts,a.retry_at,a.error,a.updated_at,c.short_code FROM photo_ai_tasks a JOIN photos p ON p.id=a.photo_id LEFT JOIN cases c ON c.id=p.case_id ORDER BY a.updated_at DESC,a.id DESC LIMIT 30")}

    def project_status(self):
        from .identity import project_filter
        s, db = self.services, self.db
        scope, args = project_filter('p.project_id')
        recognition = s['recognizer'].status()
        tasks = db.all('SELECT a.id,a.photo_id,a.kind,a.state,a.attempts,a.error,a.updated_at,c.short_code FROM photo_ai_tasks a JOIN photos p ON p.id=a.photo_id LEFT JOIN cases c ON c.id=p.case_id WHERE ' + scope + ' ORDER BY a.updated_at DESC,a.id DESC LIMIT 30', args)
        queue = {r['state']: r['n'] for r in db.all('SELECT a.state,COUNT(*) n FROM photo_ai_tasks a JOIN photos p ON p.id=a.photo_id WHERE ' + scope + ' GROUP BY a.state', args)}
        local = db.one("SELECT r.state,r.error,r.created_at FROM recognitions r JOIN photos p ON p.id=r.photo_id WHERE r.provider='local_ocr' AND " + scope + ' ORDER BY r.created_at DESC LIMIT 1', args)
        pending = db.one("SELECT COUNT(*) n FROM photos p JOIN blobs b ON p.blob_hash=b.hash WHERE b.media_type='image' AND " + scope + " AND NOT EXISTS(SELECT 1 FROM recognitions r WHERE r.photo_id=p.id AND r.provider='local_ocr' AND r.state='completed')", args)['n']
        model = {k: recognition['model_health'].get(k) for k in ('state', 'retry_at', 'last_transport_ok', 'last_response_ok')}
        return {'at': now(), 'worker_enabled': s['settings'].worker_enabled,
                'storage': {'state': 'available', 'detail': '当前项目档案可读取'},
                'model': {**model, 'error': '', 'calls': 0, 'active': 0},
                'policy': s['photo_workflow'].policy(),
                'ocr': {'enabled': recognition.get('local_ocr', True), 'installed': recognition['local_ocr_available'], 'latest': local, 'pending': pending},
                'platform': {'connected': s['platform'].client.status()['connected']},
                'sync': s['workflow'].synchronizer.status(), 'channels': [], 'attachments_pending': 0,
                'ai_queue': queue, 'ai_tasks': tasks, 'rule_states': {}, 'draft_states': {}, 'scope': 'assigned_projects'}

    def photos(self, ids):
        if not ids:
            return {}
        marks = ','.join('?' for _ in ids)
        pipelines = {r['photo_id']: r for r in self.db.all(f'SELECT * FROM photo_pipeline WHERE photo_id IN ({marks})', ids)}
        rules = {r['photo_id']: r for r in self.db.all(f'SELECT photo_id,state,error,reviewed_at FROM photo_rule_reviews WHERE photo_id IN ({marks})', ids)}
        ai = {r['photo_id']: r for r in self.db.all(f"SELECT a.id,a.photo_id,a.kind,a.state,a.attempts,a.error,a.result_json,a.context_json,a.updated_at,p.revision current_revision,p.case_id current_case FROM photo_ai_tasks a JOIN photos p ON p.id=a.photo_id WHERE a.photo_id IN ({marks}) AND a.rowid=(SELECT b.rowid FROM photo_ai_tasks b WHERE b.photo_id=a.photo_id ORDER BY b.created_at DESC,b.rowid DESC LIMIT 1)", ids)}
        result = {}
        for photo_id in ids:
            pipeline, job = pipelines.get(photo_id, {}), ai.get(photo_id)
            if job:
                job['result'] = json.loads(job.pop('result_json'))
                context = json.loads(job.pop('context_json'))
                job['model'] = context.get('model')
                revision, case_id = job.pop('current_revision'), job.pop('current_case')
                if (revision != context.get('photo_revision') or case_id != context.get('case_id')) and job['state'] not in ('cancelled','stale'):
                    job.update(state='stale', result={}, error='照片或归属已变化，此分析仅保留历史记录')
            if pipeline.get('dirty'):
                pipeline = {**pipeline, 'state': 'queued', 'reason': '资料已更新，等待规则重新核对'}
            result[photo_id] = {'receipt': 'archived', 'pipeline': {k: pipeline.get(k) for k in ('state','identity','reason','updated_at')},
                'draft': json.loads(pipeline.get('draft_json', '{}')), 'rule': rules.get(photo_id), 'ai': job}
        return result


def operations_routes(service):
    router = APIRouter(prefix='/api/operations')

    @router.get('')
    def status():
        return service.status()

    @router.put('/policy')
    def policy(data: dict = Body(...)):
        service.services['photo_workflow'].configure(data)
        return service.status()

    @router.post('/model/retry')
    def retry_model():
        return service.services['model_runtime'].allow_retry()

    @router.post('/ai/{job_id}/retry')
    def retry_photo(job_id: str):
        row = service.services['photo_ai'].retry(job_id)
        return {'id': row['id'], 'state': row['state']}

    @router.post('/ocr/retry')
    def retry_ocr():
        services = service.services
        if not services['recognizer'].status().get('local_ocr', True):
            raise DomainError('请先启用本地 OCR')
        existing = service.db.one("SELECT id,state FROM jobs WHERE kind='recognize' AND state IN ('queued','running') ORDER BY created_at LIMIT 1")
        if existing:
            return existing
        photos = service.db.all("SELECT p.id FROM photos p JOIN blobs b ON p.blob_hash=b.hash WHERE b.media_type='image' AND p.recognition_state='failed' AND NOT EXISTS(SELECT 1 FROM recognitions r WHERE r.photo_id=p.id AND r.provider='local_ocr' AND r.state='completed') LIMIT 100")
        ids = [p['id'] for p in photos if services['storage'].verify(services['domain'].photo(p['id']))]
        if not ids:
            raise DomainError('没有可重试的未完成本地识别；已确认与已成功识别的资料不重复处理')
        return services['jobs'].enqueue('recognize', {'photo_ids': ids})

    return router
