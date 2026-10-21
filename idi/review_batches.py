from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager

from .db import dump, now, uid
from .domain import DomainError
from .platform_contract import VERSION
from .platform_client import PlatformWriteError
from .stage_access import stage_access
from .project_accounts import scoped_case, scoped_record


class ReviewBatches:
    def __init__(self, workflow):
        self.workflow = workflow
        self.db, self.platform = workflow.db, workflow.platform
        self._active = set()
        self._active_lock = threading.Lock()
        self.automatic_uploads = None

    @contextmanager
    def _execution(self, review_id):
        with self._active_lock:
            if review_id in self._active:
                raise DomainError('本次操作仍在进行，请等待结果，勿重复操作', 409)
            self._active.add(review_id)
        try:
            yield
        finally:
            with self._active_lock:
                self._active.discard(review_id)

    @scoped_case()
    def snapshot(self, case_id, stage, *, save_only=False):
        plan = self.platform.preview(case_id, stage)
        case = plan['case']
        if not plan['access']['editable']:
            raise DomainError(plan['access']['reason'] + '请先刷新案件进度。', 409)
        if save_only and stage not in ('survey', 'repair'):
            raise DomainError('当前环节不支持逐步保存官网草稿', 409)
        if self.platform.validation_errors(plan, allow_incomplete=save_only):
            raise DomainError(f"当前环节还有 {plan['error_count']} 项资料待补或待核对", 409)
        selected = []
        for field in plan['file_fields']:
            for photo in field['selected']:
                blob = self.db.one('SELECT * FROM blobs WHERE hash=?', (photo['blob_hash'],))
                if not self.platform.storage.verify(blob):
                    raise DomainError('所选原文件缺失或校验失败', 409)
                if photo['recognition_state'] == 'conflict':
                    raise DomainError('所选照片存在编号或位置冲突，请先人工确认', 409)
                selected.append({'id': photo['id'], 'case_id': photo['case_id'], 'blob_hash': photo['blob_hash'], 'revision': photo['revision']})
        identity = {k: case[k] for k in ('id', 'short_code', 'platform_report_code', 'report_number', 'policy_number', 'workflow_stage')}
        fingerprint_data = {'contract': VERSION, 'case': identity, 'stage': stage,
                            'business_fields': plan['business_fields'], 'files': selected,
                            'existing_files': {f['key']: f['existing'] for f in plan['file_fields']},
                            'draft_revision': plan['draft']['revision'], 'target': plan['link']['report_receiving_id'],
                            'link_revision': plan['link']['revision']}
        if getattr(self.platform, 'project_accounts', None):
            binding = self.platform.project_accounts.binding(case['project_id'])
            plan['write_binding'] = binding
            fingerprint_data['write_binding'] = binding
        if save_only:
            fingerprint_data['save_only'] = True
            plan = {**plan, 'actions': ['save'], 'save_only': True}
        fingerprint = hashlib.sha256(dump(fingerprint_data).encode()).hexdigest()
        return plan, fingerprint

    def prepare(self, case_id, stage, expected_revision, *, save_only=False):
        plan, fingerprint = self.snapshot(case_id, stage, save_only=save_only)
        if expected_revision != plan['draft']['revision']:
            raise DomainError('草稿已经修改，请重新打开审核预览', 409)
        review_id = uid()
        with self.db.tx() as con:
            self.platform.require_editable(case_id, stage, con)
            con.execute('INSERT INTO platform_review_batches(id,case_id,stage,fingerprint,snapshot,created_at,save_only) VALUES(?,?,?,?,?,?,?)',
                        (review_id, case_id, stage, fingerprint, dump(plan), now(), int(save_only)))
        return self.get(review_id)

    @scoped_record('platform_review_batches')
    def get(self, review_id):
        row = self.db.one('SELECT * FROM platform_review_batches WHERE id=?', (review_id,))
        if not row:
            raise DomainError('核对记录不存在', 404)
        row['snapshot'], row['result'] = json.loads(row['snapshot']), json.loads(row['result'])
        row['write_enabled'] = self.platform.client.write_enabled
        row['connected'] = self.platform.client.status()['connected']
        with self._active_lock:
            row['active'] = review_id in self._active
        row['access'] = stage_access(self.workflow.domain.case(row['case_id']), row['stage'])
        return row

    @scoped_record('platform_review_batches', 'review_confirmation')
    def confirm(self, review_id, fingerprint, local_only=False, action='submit'):
        from .identity import current
        if getattr(self.platform, 'project_accounts', None) and not local_only and action != 'save' and current() is None:
            raise DomainError('正式提交须由登录的 IDI 系统管理员或本项目管理员核对确认', 403)
        with self._execution(review_id), self.platform.material_lock:
            self._confirm(review_id, fingerprint, local_only, action)
        return self.get(review_id)

    @scoped_record('platform_review_batches', 'automatic_save')
    def save_for_agent(self, review_id, fingerprint, check_permission=None):
        with self._execution(review_id), self.platform.material_lock:
            self._confirm(review_id, fingerprint, False, 'save', draft_only=True, check_permission=check_permission)
        return self.get(review_id)

    def _verified_uploads(self, plan):
        from .checkin_copies import verified_copy_receipts
        selected = {p['id']: p['blob_hash'] for field in plan['file_fields'] for p in field['selected']}
        remote_ids = {str(f['id']) for field in plan['file_fields'] for f in field['existing'] if f.get('id')}
        receipts = {}
        for row in self.db.all("SELECT snapshot,result FROM platform_review_batches WHERE case_id=? AND stage=? AND state IN ('save_verified','verified') ORDER BY created_at", (plan['case']['id'], plan['stage'])):
            snapshot, result = json.loads(row['snapshot']), json.loads(row['result'])
            if snapshot['link']['report_receiving_id'] != plan['link']['report_receiving_id']:
                continue
            originals = {p['id']: p['blob_hash'] for field in snapshot['file_fields'] for p in field['selected']}
            for photo_id, receipt in result.get('uploaded', {}).items():
                if photo_id in selected and originals.get(photo_id) == selected[photo_id] and str(receipt.get('id')) in remote_ids:
                    receipts[photo_id] = receipt
        receipts.update({pid: receipt for pid, receipt in verified_copy_receipts(
            self.platform, plan['case']['id'], plan['stage'], plan['link']).items() if pid in selected})
        return receipts

    def _confirm(self, review_id, fingerprint, local_only, action, draft_only=False, check_permission=None):
        if check_permission:
            check_permission()
        record = self.get(review_id)
        save_only = bool(record.get('save_only'))
        if save_only and action != 'save':
            raise DomainError('本记录仅用于逐步保存草稿，请重新核对完整资料后提交', 403)
        if draft_only and (action != 'save' or record['stage'] not in ('survey', 'repair')):
            raise DomainError('Agent 没有此环节的正式提交权限', 403)
        if action not in ('save', 'submit') or action not in record['snapshot']['actions']:
            raise DomainError('此环节不支持所选官网操作')
        if record['fingerprint'] != fingerprint:
            raise DomainError('核对版本不一致，请重新打开预览', 409)
        if record['result'].get('action', action) != action and not local_only:
            raise DomainError('此记录已用于另一项官网操作，请重新打开审核预览', 409)
        if record['state'] in ('verified', 'submitted', 'saved', 'save_verified'):
            return record
        if self.automatic_uploads and action == 'submit' and not local_only:
            self.automatic_uploads.validate_submission(record)
        try:
            plan, current = self.snapshot(record['case_id'], record['stage'], save_only=save_only)
        except DomainError:
            with self.db.tx() as con:
                con.execute("UPDATE platform_review_batches SET state='stale' WHERE id=? AND state IN ('prepared','reviewed','failed')", (review_id,))
            raise
        if current != fingerprint:
            with self.db.tx() as con:
                con.execute("UPDATE platform_review_batches SET state='stale' WHERE id=? AND state IN ('prepared','reviewed','failed')", (review_id,))
            raise DomainError('核对后字段、选材或官网状态发生变化，请重新审核；本次未上传', 409)
        if not local_only and not self.platform.client.write_enabled:
            raise DomainError('官网当前为只读模式，可保存核对记录和下载资料', 403)
        if not local_only:
            self.platform.removals.require_clear(record['case_id'], record['stage'])
            session = self.platform.client.status()
            if not session['connected']:
                raise DomainError('请先连接官网，再继续保存或提交', 401)
            expected_account = (plan['write_binding']['username'] if 'write_binding' in plan
                                else plan['link']['snapshot'].get('account'))
            if expected_account and expected_account != session['account']:
                raise DomainError('官网登录账号已变化，请先刷新案件并重新审核', 409)
        with self.db.tx() as con:
            state = con.execute('SELECT state FROM platform_review_batches WHERE id=?', (review_id,)).fetchone()['state']
            if state not in ('prepared', 'reviewed', 'failed'):
                raise DomainError('此核对记录已执行或结果待核实，请回读官网，不能重复提交', 409)
            if not local_only and con.execute("SELECT id FROM platform_review_batches WHERE case_id=? AND stage=? AND id<>? AND state IN ('submitting','submitted','saved','uncertain')", (record['case_id'], record['stage'], review_id)).fetchone():
                raise DomainError('本案此环节已有提交记录待回读核实，不能再次提交', 409)
            con.execute('UPDATE platform_review_batches SET state=?,confirmed_at=? WHERE id=?',
                        ('reviewed' if local_only else 'submitting', now(), review_id))
        if local_only:
            return self.get(review_id)
        uploaded = record['result'].get('uploaded', {})
        reused = {}
        payload, response, attempted = None, None, False
        total = len({p['id'] for f in plan['file_fields'] for p in f['selected']})
        def progress(phase, **extra):
            self._result(review_id, {'action': action, 'phase': phase, 'uploaded': uploaded, 'file_total': total,
                                    'reused_uploads': list(reused), 'remote_written': bool(uploaded), 'business_submitted': False, **extra})
        try:
            progress('checking')
            # Re-read immediately before uploading. Never submit against a stale official stage.
            self.workflow.refresh_case(record['case_id'])
            fresh_plan, fresh_fingerprint = self.snapshot(record['case_id'], record['stage'], save_only=save_only)
            if fresh_fingerprint != fingerprint:
                raise DomainError('官网最新资料发生变化，请重新审核后提交', 409)
            reused = self._verified_uploads(fresh_plan)
            uploaded = {**reused, **uploaded}
            for field in plan['file_fields']:
                for photo in field['selected']:
                    if photo['id'] in uploaded:
                        continue
                    blob = self.db.one('SELECT * FROM blobs WHERE hash=?', (photo['blob_hash'],))
                    if not self.platform.storage.verify(blob):
                        raise DomainError('上传前原文件校验失败', 409)
                    if check_permission:
                        check_permission()
                    if self.automatic_uploads and action == 'submit':
                        self.automatic_uploads.validate_submission(record)
                    progress('uploading', current_file=photo['original_name'])
                    uploaded[photo['id']] = self.platform.client.upload(self.platform.storage.resolve(blob['stored_path']), photo['original_name'], blob['mime_type'])
                    progress('uploading')
            # Local edits during an upload invalidate the business submission, retaining upload receipts.
            _, check = self.snapshot(record['case_id'], record['stage'], save_only=save_only)
            if check != fingerprint:
                raise DomainError('上传期间资料发生变化，已停止业务提交；已上传文件回执保留', 409)
            payload = self.platform.build_payload(plan, action, uploaded, allow_incomplete=save_only)
            progress('saving' if action == 'save' else 'submitting', payload=payload, business_submitted='sending')
            # Freeze local edits through the final check and business request.
            with self.db.tx() as con:
                _, final_fingerprint = self.snapshot(record['case_id'], record['stage'], save_only=save_only)
                if final_fingerprint != fingerprint:
                    raise DomainError('提交前资料发生变化，请重新审核', 409)
                if check_permission:
                    check_permission()
                if self.automatic_uploads and action == 'submit':
                    self.automatic_uploads.validate_submission(record)
                attempted = True
                response = (self.platform.client.save_draft_only(record['stage'], payload) if draft_only
                            else self.platform.client.submit(record['stage'], payload))
                result = {'action': action, 'phase': 'readback', 'file_total': total, 'uploaded': uploaded, 'payload': payload,
                          'reused_uploads': list(reused),
                          'response_code': response.get('code'), 'remote_written': True, 'business_submitted': action == 'submit', 'operation_confirmed': True}
                con.execute('UPDATE platform_review_batches SET result=?,state=?,finished_at=? WHERE id=?', (dump(result), 'saved' if action == 'save' else 'submitted', now(), review_id))
        except Exception as error:
            unknown = attempted and not (isinstance(error, PlatformWriteError) and error.rejected)
            state = 'uncertain' if unknown else 'stale' if getattr(error, 'status', None) == 409 else 'failed'
            self._result(review_id, {'action': action, 'phase': 'uncertain' if unknown else 'failed', 'file_total': total,
                                    'uploaded': uploaded, 'payload': payload, 'remote_written': bool(uploaded) or unknown,
                                    'business_submitted': 'unknown' if unknown else False, 'operation_confirmed': False, 'error': str(error)}, state=state)
            raise DomainError(str(error), getattr(error, 'status', 502)) from error
        # A successful write followed by a read failure remains a confirmed write.
        try:
            return self._reconcile(review_id)
        except Exception as error:
            self._result(review_id, {**result, 'error': '官网已' + ('保存' if action == 'save' else '接收提交') + '，回读暂未完成：' + str(error)})
            return self.get(review_id)

    def _result(self, review_id, result, state=None):
        with self.db.tx() as con:
            if state:
                con.execute('UPDATE platform_review_batches SET result=?,state=?,finished_at=? WHERE id=?', (dump(result), state, now(), review_id))
            else:
                con.execute('UPDATE platform_review_batches SET result=? WHERE id=?', (dump(result), review_id))

    @scoped_record('platform_review_batches', 'readback')
    def reconcile(self, review_id):
        with self._execution(review_id), self.platform.material_lock:
            self._reconcile(review_id)
        return self.get(review_id)

    def _reconcile(self, review_id):
        record = self.get(review_id)
        if record['state'] not in ('submitting', 'submitted', 'uncertain', 'verified', 'saved', 'save_verified'):
            raise DomainError('此记录尚未执行官网提交，无需回读回执', 409)
        self.workflow.refresh_case(record['case_id'])
        payload = record['result'].get('payload')
        if not payload:
            if record['result'].get('business_submitted') is False:
                self._result(review_id, {**record['result'], 'phase': 'failed', 'error': '上次操作在业务保存或提交前中断，可继续重试；已取得的上传回执保留。'}, state='failed')
            return self.get(review_id)
        action = record['result'].get('action', 'submit')
        link = self.platform.link(record['case_id'])
        remote = self.platform._remote(link, record['stage'])
        mismatches = []
        for key, expected in payload.items():
            if key in ('state', 'reportReceivingId', 'siteSurveyId', 'repairAcceptanceId', 'communityInfoId', 'engineeringModelingParentId', 'repairUnitId'):
                continue
            actual = remote.get(key)
            if isinstance(expected, list):
                expected_ids = {str(v['id']) for v in expected if isinstance(v, dict) and v.get('id')}
                actual_ids = {str(v['id']) for v in (actual or []) if isinstance(v, dict) and v.get('id')} if isinstance(actual, list) else set()
                if not expected_ids.issubset(actual_ids):
                    mismatches.append(key)
            elif str(actual or '') != str(expected or ''):
                mismatches.append(key)
        # Stored fields alone can be a draft. The official flow must also have advanced.
        current = self.workflow.domain.case(record['case_id'])['workflow_stage']
        advanced = {'intake': current in ('insurer_review', 'survey', 'repair', 'acceptance', 'settlement', 'closed', 'direct_payment', 'noncoverage'),
                    'survey': current in ('repair', 'acceptance', 'settlement', 'closed', 'noncoverage'),
                    'repair': current in ('acceptance', 'settlement', 'closed'), 'acceptance': current in ('settlement', 'closed')}[record['stage']]
        if action == 'submit' and not advanced:
            mismatches.append('workflow_stage')
        if action == 'save':
            state_key = 'siteSurveyState' if record['stage'] == 'survey' else 'repairAcceptanceState'
            if str(link['snapshot'].get('row', {}).get(state_key)) != '0' or current != record['snapshot']['case']['workflow_stage']:
                mismatches.append('draft_state')
        response_confirmed = record['result'].get('operation_confirmed') is True or record['result'].get('business_submitted') is True or record['state'] in ('submitted', 'verified', 'saved', 'save_verified')
        state = ('save_verified' if action == 'save' else 'verified') if not mismatches else ('saved' if action == 'save' else 'submitted') if response_confirmed else 'uncertain'
        result = {**record['result'], 'phase': 'done' if not mismatches else 'readback', 'readback_at': now(), 'mismatches': mismatches}
        result.pop('error', None)
        self._result(review_id, result, state=state)
        if not mismatches and action == 'save':
            self._clear_saved_selection(record)
        return self.get(review_id)

    def _clear_saved_selection(self, record):
        # Only clear this reviewed selection; concurrent user edits stay untouched.
        with self.db.tx() as con:
            draft = con.execute('SELECT * FROM platform_drafts WHERE case_id=? AND stage=?', (record['case_id'], record['stage'])).fetchone()
            if not draft or draft['revision'] != record['snapshot']['draft']['revision']:
                return
            files = json.loads(draft['files_json'])
            uploaded = record['result'].get('uploaded', {})
            updated = {key: [pid for pid in ids if pid not in uploaded] for key, ids in files.items()}
            if updated != files:
                con.execute('UPDATE platform_drafts SET files_json=?,revision=revision+1,updated_at=? WHERE case_id=? AND stage=?',
                            (dump(updated), now(), record['case_id'], record['stage']))
