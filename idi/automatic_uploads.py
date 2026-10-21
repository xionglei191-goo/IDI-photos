"""Incremental official draft saves with durable, human-confirmed proposals."""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import nullcontext

from fastapi import APIRouter, Body

from .db import audit, dump, now
from .domain import DomainError
from .photo_ai import digest
from .platform_prefill import PREFILL_RULES_VERSION, canonical_value

log = logging.getLogger(__name__)
UNRESOLVED = ('submitting', 'saved', 'submitted', 'uncertain')


class AutomaticUploads:
    def __init__(self, workflow, reviews, photo_workflow):
        self.workflow, self.reviews, self.rules = workflow, reviews, photo_workflow
        self.db, self.platform, self.domain = workflow.db, workflow.platform, workflow.domain
        self._stop, self._lock, self._thread = threading.Event(), threading.Lock(), None
        self.notifications = None

    def policy(self):
        return {'enabled': False, 'activated_at': '', 'notify_review_ready': False,
                **self.db.setting('automatic_upload_policy', {})}

    def configure(self, data):
        if not data or set(data) - {'enabled', 'notify_review_ready'} or any(type(v) is not bool for v in data.values()):
            raise DomainError('请提供有效的自动上传或飞书核对提醒开关')
        with self.notifications._lock if self.notifications else nullcontext():
            old = self.policy()
            value = {**old, **data}
            value['activated_at'] = old.get('activated_at') or now() if value['enabled'] else old.get('activated_at', '')
            self.db.set_setting('automatic_upload_policy', value)
        return self.status()

    def enroll(self, case_id, enabled=True):
        case = self.domain.case(case_id)
        if not case['platform_linked']:
            raise DomainError('请先关联官网案件，再启用自动上传', 409)
        if type(enabled) is not bool:
            raise DomainError('案件自动上传开关无效')
        with self.db.tx() as con:
            con.execute("INSERT INTO automatic_upload_cases(case_id,enabled,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET enabled=excluded.enabled,signature='',retry_at=0,attempts=0,updated_at=excluded.updated_at",
                        (case_id, int(enabled), now(), now()))
            audit(con, 'case', case_id, 'automatic_uploads', after={'enabled': enabled})
        return self.get(case_id)

    def get(self, case_id):
        self.domain.case(case_id)
        row = self.db.one('SELECT u.*,c.project_id,c.short_code,c.platform_report_code,c.title,c.workflow_stage,p.name AS project_name FROM automatic_upload_cases u JOIN cases c ON c.id=u.case_id JOIN projects p ON p.id=c.project_id WHERE case_id=?', (case_id,))
        if row:
            row['detail'] = json.loads(row['detail'])
            row['notification'] = self.notifications.status_for(row) if self.notifications else None
        return row

    def status(self):
        from .identity import current, project_filter
        scope, args = project_filter('c.project_id')
        rows = self.db.all("SELECT case_id FROM automatic_upload_cases u JOIN cases c ON c.id=u.case_id WHERE " + scope + " ORDER BY CASE WHEN u.state='awaiting_confirmation' THEN 0 ELSE 1 END,u.updated_at DESC LIMIT 200", args)
        items = [self.get(r['case_id']) for r in rows]
        counts = {r['state']: r['n'] for r in self.db.all('SELECT u.state,COUNT(*) AS n FROM automatic_upload_cases u JOIN cases c ON c.id=u.case_id WHERE u.enabled=1 AND ' + scope + ' GROUP BY u.state', args)}
        connected, enabled = self.platform.client.status()['connected'], self.platform.client.write_enabled
        accounts = getattr(self.platform, 'project_accounts', None)
        company_connected = connected
        project_connections = []
        if accounts:
            projects = self.domain.projects()
            connected = any(accounts.account(p['id']).client.status()['connected'] for p in projects)
            enabled = accounts.write_enabled and any(accounts.enabled(p['id']) and accounts.account(p['id'])._config for p in projects)
            company_connected = accounts.company.client.status()['connected']
            # Only surface accounts needed by this person's active saves. A
            # connected account for another project cannot unblock these cases.
            needed = {item['project_id'] for item in items if item['enabled'] and (
                item['workflow_stage'] in ('survey', 'repair') or
                item['state'] in ('saving', 'readback_pending', 'awaiting_confirmation'))}
            for project in projects:
                if project['id'] not in needed:
                    continue
                account = accounts.account(project['id']).status()
                state = account['state']
                if account['configured']:
                    if not accounts.enabled(project['id']):
                        state = 'write_paused'
                    elif not accounts.write_enabled:
                        state = 'server_read_only'
                project_connections.append({'project_id': project['id'], 'project_name': project['name'],
                                            'state': state, 'connected': account['session']['connected']})
        person = current()
        return {'policy': self.policy(), 'items': items, 'counts': counts,
                'connected': bool(connected), 'write_enabled': bool(enabled),
                'account_scope': 'project' if accounts else 'company',
                'company_connected': bool(company_connected), 'project_connections': project_connections,
                'can_configure_project_accounts': person is None or person.admin}

    def signature(self, case_id, *, proposal=False):
        from .project_accounts import case_context
        with case_context(self.platform, case_id):
            return self._signature(case_id, proposal=proposal)

    def _signature(self, case_id, *, proposal=False):
        case = self.domain.case(case_id)
        photos = self.db.all('SELECT p.id,p.revision,p.blob_hash,p.stage,p.role,p.photo_group,p.recognition_state,sc.visit_id AS shared_checkin_visit_id,r.dirty,r.identity,r.state,r.photo_revision FROM photos p LEFT JOIN photo_pipeline r ON r.photo_id=p.id LEFT JOIN visit_shared_checkins sc ON sc.photo_id=p.id WHERE p.case_id=? ORDER BY p.id', (case_id,))
        data = [PREFILL_RULES_VERSION, self.rules.case_signature(case), photos, self.platform.prefill_defaults(),
                self.db.all('SELECT stage,revision,target_report_id FROM platform_drafts WHERE case_id=? ORDER BY stage', (case_id,)),
                self.db.all('SELECT stage,field,photo_id,photo_revision,blob_hash,target_report_id FROM platform_manual_file_selections WHERE case_id=? ORDER BY stage,field,photo_id', (case_id,)),
                self.platform.link(case_id)['revision']]
        if getattr(self.platform, 'project_accounts', None):
            data.append(self.platform.project_accounts.binding(case['project_id']))
        if not proposal:
            session = self.platform.client.status()
            data += [self.policy().get('enabled'), self.rules.policy(), session['connected'], session.get('account'), self.platform.client.write_enabled,
                     self.db.all('SELECT connector_id,revision,enabled,images_enabled,save_drafts FROM feishu_agent_bindings ORDER BY connector_id')]
        return digest(data)

    def validate_submission(self, record):
        row = self.get(record['case_id'])
        if row and row['review_id'] == record['id'] and row['review_guard'] != self.signature(record['case_id'], proposal=True):
            raise DomainError('拟提交后收到新资料或资料已修改，请等待重新保存并核对新的拟提交信息', 409)

    def _invalidate(self, row):
        if row.get('review_id'):
            with self.db.tx() as con:
                con.execute("UPDATE platform_review_batches SET state='stale' WHERE id=? AND state IN ('prepared','reviewed','failed')", (row['review_id'],))

    def _record(self, case_id, state, detail, *, review_id=None, guard='', save_id=None, retry=0, attempts=0):
        with self.db.tx() as con:
            if state in ('awaiting_confirmation', 'needs_fields') and guard and guard != self.signature(case_id, proposal=True):
                con.execute("UPDATE platform_review_batches SET state='stale' WHERE id=? AND state='prepared'", (review_id,))
                state, review_id, guard, retry = 'queued', None, '', 1
                detail = {'reason': '生成拟提交信息时资料发生变化，正在重新核对'}
            con.execute('UPDATE automatic_upload_cases SET stage=?,state=?,signature=?,detail=?,review_id=?,review_guard=?,save_review_id=COALESCE(?,save_review_id),retry_at=?,attempts=?,updated_at=? WHERE case_id=?',
                (self.domain.case(case_id)['workflow_stage'], state, self.signature(case_id), dump(detail), review_id, guard,
                 save_id, time.time() + retry if retry else 0, attempts, now(), case_id))
        return self.get(case_id)

    def _permitted(self, case_id, selected):
        row = self.get(case_id)
        if self._stop.is_set() or not self.policy().get('enabled') or not row or not row['enabled']:
            raise DomainError('自动上传已暂停', 409)
        if not all(self.rules.policy().get(k) for k in ('rules_enabled', 'auto_drafts')):
            raise DomainError('照片规则或草稿整理已暂停', 409)
        case = self.domain.case(case_id)
        files = self.platform.draft(case_id, case['workflow_stage'])['files']
        manual = self.platform.manual_file_selections(case_id, case['workflow_stage'])
        for photo_id in selected:
            photo = self.domain.photo(photo_id)
            if photo.get('uploaded_by') and getattr(self.platform, 'project_accounts', None):
                # Reload enabled state and membership for queued uploads too.
                user = self.db.one('SELECT enabled,system_role FROM idi_users WHERE id=?', (photo['uploaded_by'],))
                member = self.db.one('SELECT role FROM project_members WHERE project_id=? AND user_id=?',
                                     (case['project_id'], photo['uploaded_by']))
                if not user or not user['enabled'] or user['system_role'] != 'admin' and not member:
                    raise DomainError('上传人员的项目权限已失效，照片保留，请管理员核对', 403)
            pipeline = self.db.one('SELECT * FROM photo_pipeline WHERE photo_id=?', (photo_id,))
            if photo['case_id'] != case_id or not pipeline or pipeline['dirty'] or pipeline['photo_revision'] != photo['revision'] or pipeline['identity'] == 'unverified':
                raise DomainError('选材含未核对或已变化的照片，请先核对归属与用途', 409)
            if pipeline['state'] == 'stage_mismatch':
                fields = [key for key, ids in files.items() if photo_id in ids]
                if not fields or any((field, photo_id) not in manual for field in fields):
                    raise DomainError('照片原分类与当前环节不同，或人工选材后照片已变化，请在对应附件栏重新选择并保存本地草稿', 409)
            elif pipeline['state'] not in ('local_draft', 'already_saved'):
                raise DomainError('选材含未核对或已变化的照片，请先核对归属与用途', 409)
            if not self.platform.storage.verify(photo):
                raise DomainError('原件校验失败，请核对归档文件', 409)
            task = self.rules.source_task(photo_id)
            if task:
                self.rules.task_permission(task, case, saving=True)
            elif any(s.get('metadata', {}).get('chat_id') for s in photo.get('sources', [])):
                raise DomainError('群照片缺少有效的原收图任务授权，请人工核对后保存', 409)
        sources = self.platform.field_sources(case_id, case['workflow_stage'])
        evidence = {pid for s in sources.values() if s['origin'] == 'automatic' for pid in s['photo_ids']}
        if evidence and not evidence <= self._prefill_photo_ids(case):
            raise DomainError('自动填写使用的照片依据或原收图授权已变化，请重新核对', 409)

    def _prefill_photo_ids(self, case):
        allowed = set()
        excluded = {p['photo_id'] for p in self.db.all('SELECT photo_id FROM platform_draft_exclusions WHERE case_id=? AND stage=?', (case['id'], case['workflow_stage']))}
        blocked = {p['blob_hash'] for p in self.db.all('SELECT blob_hash FROM platform_attachment_blocks WHERE case_id=? AND stage=?', (case['id'], case['workflow_stage']))}
        for row in self.db.all("SELECT p.id FROM photos p JOIN photo_pipeline r ON r.photo_id=p.id JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id=? AND b.media_type='image' AND r.dirty=0 AND r.photo_revision=p.revision AND r.identity<>'unverified' AND p.recognition_state IN ('recognized','confirmed')", (case['id'],)):
            if row['id'] in excluded:
                continue
            photo = self.domain.photo(row['id'])
            if photo['blob_hash'] in blocked:
                continue
            if not self.platform.storage.verify(photo):
                continue
            task = self.rules.source_task(photo['id'])
            if task:
                try:
                    self.rules.task_permission(task, case, saving=True)
                except DomainError:
                    continue
            elif any(s.get('metadata', {}).get('chat_id') for s in photo.get('sources', [])):
                continue
            allowed.add(photo['id'])
        return allowed

    @staticmethod
    def _filled_fields(plan):
        fields = {f['key']: f for f in plan['fields']}
        def display(key, source):
            field = fields[key]
            if field['kind'] == 'personnel':
                return str(plan['values'].get('repairPersonName') or source['value'])
            if field.get('name_key') and plan['values'].get(field['name_key']):
                return str(plan['values'][field['name_key']])
            return next((o['name'] for o in field.get('options', []) if o['code'] == source['value']), source['value'])
        return [{'field': key, 'label': fields[key]['label'],
                 'value': display(key, source),
                 'source': source['source'], 'needs_review': source['needs_review']}
                for key, source in plan.get('field_sources', {}).items()
                if key in fields and source['origin'] == 'automatic' and source.get('current') and not source.get('stale')]

    @staticmethod
    def _unsaved_fields(plan):
        fields = {f['key']: f for f in plan['fields']}
        return any(source['origin'] == 'automatic' and source.get('current') and not source.get('stale')
                   and canonical_value(fields[key], source['value']) != canonical_value(fields[key], plan['remote_values'].get(key))
                   for key, source in plan.get('field_sources', {}).items() if key in fields)

    def _detail(self, plan, notes=None):
        notes = notes if notes is not None else plan.get('field_suggestion_notes', {})
        labels = {f['key']: f['label'] for f in plan['fields']}
        return {'issues': [i['message'] for i in plan['issues'] if i['severity'] == 'error'],
                'filled_fields': self._filled_fields(plan),
                'field_notes': [{'label': labels.get(i['field'], i['field']), 'reason': notes[i['field']]}
                                for i in plan['issues'] if i['severity'] == 'error' and notes.get(i['field'])],
                'other_stage_count': sum(p['photo_group'] in ('work_checkin', 'construction', 'completion') for p in plan['photos']) if plan['stage'] == 'survey' else 0,
                'saved_count': sum(len(f['existing']) for f in plan['file_fields'] if not f.get('readonly')),
                'files': [{'label': f['label'], 'count': len(f['existing'])} for f in plan['file_fields'] if not f.get('readonly')]}

    def _discover(self):
        # Enabling the feature does not bulk-submit the historical archive.
        for row in self.db.all('SELECT DISTINCT p.case_id FROM photos p JOIN platform_links l ON l.case_id=p.case_id WHERE (p.created_at>=? OR p.uploaded_by IS NOT NULL) AND l.report_receiving_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM automatic_upload_cases u WHERE u.case_id=p.case_id) LIMIT 200', (self.policy().get('activated_at') or now(),)):
            self.enroll(row['case_id'])

    def run_once(self):
        if self._stop.is_set() or not self.policy().get('enabled') or not self._lock.acquire(blocking=False):
            return 0
        processed = 0
        try:
            self._discover()
            for small in self.db.all("SELECT u.case_id FROM automatic_upload_cases u WHERE u.enabled=1 ORDER BY EXISTS(SELECT 1 FROM photos p WHERE p.case_id=u.case_id AND p.photo_group IN ('survey_checkin','work_checkin') AND p.updated_at>=u.updated_at) DESC,u.updated_at"):
                if self._stop.is_set() or not self.policy().get('enabled'):
                    break
                row = self.get(small['case_id'])
                changed = row['signature'] != self.signature(row['case_id'])
                if not changed and (not row['retry_at'] or row['retry_at'] > time.time()):
                    continue
                try:
                    self.process(row)
                except Exception as exc:
                    current = self.get(row['case_id'])
                    attempts = current['attempts'] + 1
                    record = self.reviews.get(current['save_review_id']) if current['save_review_id'] else None
                    unresolved = record and record['state'] in UNRESOLVED
                    reason = str(exc)[:400] if isinstance(exc, DomainError) else '处理未完成，请查看任务日志后重试'
                    if not isinstance(exc, DomainError):
                        log.exception('Automatic upload failed for %s', row['case_id'])
                    self._record(row['case_id'], 'readback_pending' if unresolved else 'needs_check' if getattr(exc, 'status', 0) in (403, 409) else 'retry_wait',
                        {'reason': reason}, save_id=record['id'] if record else None,
                        retry=min(300, 15 * 2 ** min(attempts, 4)) if attempts < 5 else 0, attempts=attempts)
                processed += 1
                if processed >= 10:
                    break
            return processed
        finally:
            self._lock.release()

    def process(self, row):
        case_id = row['case_id']
        self.platform.removals.require_clear(case_id, self.domain.case(case_id)['workflow_stage'])
        self._invalidate(row)
        if not all(self.rules.policy().get(k) for k in ('rules_enabled', 'auto_drafts')):
            return self._record(case_id, 'paused', {'reason': '请在系统状态中启用照片规则和草稿整理'})
        case = self.domain.case(case_id)
        if case['workflow_stage'] not in ('survey', 'repair'):
            return self._record(case_id, 'complete' if case['workflow_stage'] == 'closed' else 'waiting_stage',
                {'reason': '修复成果已提交；业主验收仍需实际验收日期和签字 PDF' if case['workflow_stage'] == 'acceptance' else '等待进入查勘或修复成果环节'})
        # Revisit only enrolled cases, including next-stage photos received earlier.
        for p in self.db.all("SELECT p.id FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id=? AND b.media_type='image' ORDER BY p.created_at,p.id", (case_id,)):
            self.rules.process(p['id'], revisit=True)
        from .project_accounts import case_context
        with self.platform.material_lock, case_context(self.platform, case_id):
            self.platform.removals.require_clear(case_id, case['workflow_stage'])
            prefill = self.platform.prefill_draft(case_id, case['workflow_stage'], self._prefill_photo_ids(case))
            if not self.platform.client.status()['connected']:
                return self._record(case_id, 'awaiting_login', {**self._detail(prefill['plan']),
                    'reason': '照片与自动填写的本地草稿已保留；连接官网后继续保存'})
            if not self.platform.client.write_enabled:
                return self._record(case_id, 'read_only', {**self._detail(prefill['plan']), 'reason': '官网写入已关闭，照片和草稿保留在本机'})
            previous = self.reviews.get(row['save_review_id']) if row['save_review_id'] else None
            if previous and previous['state'] in UNRESOLVED:
                previous = self.reviews.reconcile(previous['id'])
                if previous['state'] in UNRESOLVED:
                    attempts = row['attempts'] + 1
                    return self._record(case_id, 'readback_pending', {'reason': '原保存结果仍待回读核对，未重复发送', 'mismatches': previous['result'].get('mismatches', [])},
                        save_id=previous['id'], retry=min(300, 15 * 2 ** min(attempts, 4)) if attempts < 5 else 0, attempts=attempts)
            self.workflow.refresh_case(case_id)
            case = self.domain.case(case_id)
            if case['workflow_stage'] != row['stage'] and row['stage']:
                return self._record(case_id, 'queued', {'reason': '官网环节已更新，正在检查此环节已有照片'}, retry=1)
            stage = case['workflow_stage']
            if stage not in ('survey', 'repair'):
                return self._record(case_id, 'waiting_stage', {'reason': '官网环节已更新'})
            prefill = self.platform.prefill_draft(case_id, stage, self._prefill_photo_ids(case))
            plan = prefill['plan']
            selected = {p['id'] for f in plan['file_fields'] for p in f['selected']}
            self._permitted(case_id, selected)
            if selected or self._unsaved_fields(plan):
                _, fingerprint = self.reviews.snapshot(case_id, stage, save_only=True)
                record = previous if previous and previous['state'] == 'failed' and previous['fingerprint'] == fingerprint else self.reviews.prepare(case_id, stage, plan['draft']['revision'], save_only=True)
                self._record(case_id, 'saving', {'reason': f'正在上传并保存 {len(selected)} 张照片和已填写字段' if selected else '正在保存已自动填写的官网草稿字段',
                    'selected_count': len(selected), 'filled_fields': self._filled_fields(plan)}, save_id=record['id'])
                record = self.reviews.save_for_agent(record['id'], record['fingerprint'], check_permission=lambda: self._permitted(case_id, selected))
                if record['state'] != 'save_verified':
                    return self._record(case_id, 'readback_pending', {'reason': '官网已响应保存，正在回读核对'}, save_id=record['id'], retry=15)
                # Clear only selections covered by verified receipts and refresh counts.
                plan = self.platform.preview(case_id, stage)
            guard = self.signature(case_id, proposal=True)
            plan = self.platform.preview(case_id, stage)
            pending = self.db.all("SELECT p.id,r.reason FROM photos p JOIN blobs b ON b.hash=p.blob_hash LEFT JOIN photo_pipeline r ON r.photo_id=p.id WHERE p.case_id=? AND b.media_type='image' AND (p.stage=? OR p.stage='unknown' OR (?='repair' AND p.stage='acceptance')) AND (r.identity IS NULL OR r.identity='unverified' OR r.state IN ('needs_check','storage_error') OR r.dirty=1 OR r.photo_revision<>p.revision)", (case_id, 'survey' if stage == 'survey' else 'work', stage))
            issues = [i['message'] for i in plan['issues'] if i['severity'] == 'error']
            detail = {**self._detail(plan, prefill['plan'].get('field_suggestion_notes', {})), 'pending_count': len(pending)}
            if issues or pending:
                reason = '已保存可用照片；补齐资料后生成拟提交信息' if detail['saved_count'] else '已自动填写可确定字段；等待补齐本环节资料' if detail['filled_fields'] else '等待本环节照片及必要资料'
                return self._record(case_id, 'needs_fields', {**detail, 'reason': reason,
                    'pending_reasons': list(dict.fromkeys(p.get('reason') or '照片用途待核对' for p in pending))}, guard=guard)
            proposal = self.reviews.prepare(case_id, stage, plan['draft']['revision'])
            return self._record(case_id, 'awaiting_confirmation', {**detail, 'reason': '资料已齐，请核对拟提交信息；确认后正式提交'},
                review_id=proposal['id'], guard=guard)

    def start(self):
        if self._thread:
            return
        self._stop.clear()
        with self.db.tx() as con:
            con.execute("UPDATE automatic_upload_cases SET signature='',retry_at=0 WHERE state='saving'")
        self._thread = threading.Thread(target=self._loop, daemon=True, name='idi-automatic-uploads')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception('Automatic upload queue failed')
            self._stop.wait(2)


def automatic_upload_routes(service):
    router = APIRouter(prefix='/api/workflow/uploads')

    @router.get('')
    def status():
        return service.status()

    @router.put('/policy')
    def configure(data: dict = Body(...)):
        return service.configure(data)

    @router.get('/cases/{case_id}')
    def get(case_id: str):
        service.domain.case(case_id)
        return service.get(case_id)

    @router.post('/cases/{case_id}')
    def enroll(case_id: str, data: dict = Body(default={})):
        return service.enroll(case_id, data.get('enabled', True))

    @router.post('/notifications/{delivery_id}/retry')
    def retry_notification(delivery_id: str):
        return service.notifications.retry(delivery_id)

    @router.post('/notifications/{delivery_id}/confirm')
    def confirm_notification(delivery_id: str, data: dict = Body(...)):
        return service.notifications.confirm(delivery_id, data.get('received'))

    return router
