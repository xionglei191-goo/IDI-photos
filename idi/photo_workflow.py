"""Deterministic archive preparation and separately guarded official draft intents."""
from __future__ import annotations

import json
import logging
import threading

from .db import audit, dump, now, uid
from .domain import DomainError, PHOTO_GROUPS
from .draft_materials import check_material_scope, prepare_materials
from .photo_ai import digest
from .recognition import extract_fields
from .project_accounts import case_context

log = logging.getLogger(__name__)


class PhotoWorkflow:
    def __init__(self, db, domain, storage, recognizer, platform, feishu):
        self.db, self.domain, self.storage, self.recognizer = db, domain, storage, recognizer
        self.platform, self.feishu = platform, feishu
        self._stop, self._lock, self._threads = threading.Event(), threading.RLock(), []

    def policy(self):
        return self.db.setting('workflow_policy', {})

    def configure(self, data):
        if not data or set(data) - {'rules_enabled', 'auto_drafts'} or any(type(v) is not bool for v in data.values()):
            raise DomainError('规则开关格式无效')
        self.db.set_setting('workflow_policy', {**self.policy(), **data})
        return self.policy()

    def source_task(self, photo_id):
        row = self.db.one("SELECT t.id FROM feishu_agent_tasks t,json_each(t.input_json,'$.photo_ids') p WHERE p.value=? AND t.kind<>'brief' AND t.state<>'cancelled' ORDER BY t.created_at DESC,t.id DESC LIMIT 1", (photo_id,))
        if not row:
            source = self.db.one('SELECT reuse_source_id FROM photos WHERE id=?', (photo_id,))
            if source and source['reuse_source_id']:
                row = self.db.one("SELECT t.id FROM feishu_agent_tasks t,json_each(t.input_json,'$.photo_ids') p WHERE p.value=? AND t.kind<>'brief' AND t.state<>'cancelled' ORDER BY t.created_at DESC,t.id DESC LIMIT 1", (source['reuse_source_id'],))
        return self.feishu.get(row['id']) if row else None

    def task_permission(self, task, case=None, *, saving=False):
        binding = self.feishu.check_task(task)
        if not binding['images_enabled'] or saving and not binding['save_drafts']:
            raise DomainError('原群收图整理或草稿保存已关闭', 409)
        if case and case['project_id'] not in json.loads(binding['project_ids']):
            raise DomainError('照片案件不在原群授权项目内', 409)
        return binding

    def process_task(self, task):
        for photo_id in task['input'].get('photo_ids', []):
            self.process(photo_id, task)

    def purpose_conflicts(self, photo, fields, confirmed):
        conflicts = {k for k in ('stage', 'role', 'photo_group')
                     if k in confirmed and fields.get(k) not in (None, '', 'unknown', photo[k])}
        checkin_roles = ('building_checkin', 'door_checkin')
        if (photo['role'] in checkin_roles and photo['photo_group'] in ('survey_checkin', 'work_checkin')
                and fields.get('photo_group') in ('survey_checkin', 'work_checkin') and fields.get('role') == 'other'):
            # OCR's generic check-in role does not contradict a reviewed door or
            # building role. A specific opposite role remains a real conflict.
            conflicts.discard('role')
        if (photo['recognition_state'] == 'confirmed'
                and {'case_id', 'stage', 'role', 'photo_group'} <= confirmed
                and photo['stage'] == 'survey' and photo['photo_group'] == 'survey_checkin'
                and photo['role'] in checkin_roles
                and fields.get('stage') == 'work' and fields.get('photo_group') == 'work_checkin'):
            visit = self.db.one('SELECT v.* FROM visit_shared_checkins s JOIN visits v ON v.id=s.visit_id WHERE s.photo_id=?', (photo['id'],))
            date = (photo.get('watermark_time') or photo.get('exif_time') or '')[:10]
            if (visit and visit['basis'] == 'manual' and visit['same_day_repair']
                    and visit['stage'] == 'work' and visit['case_id'] == photo['case_id']
                    and date and visit['visit_date'] == date):
                # Explicit same-entry reuse preserves both the original work
                # watermark and its reviewed survey purpose.
                conflicts.difference_update(('stage', 'photo_group'))
        from .checkin_copies import copy_purpose_conflicts
        return copy_purpose_conflicts(photo, fields, conflicts)

    def process(self, photo_id, task=None, *, revisit=False):
        if not self.policy().get('rules_enabled') or self._stop.is_set():
            return None
        with self._lock:
            photo = self.domain.photo(photo_id)
            pipeline = self.db.one('SELECT * FROM photo_pipeline WHERE photo_id=?', (photo_id,))
            if pipeline and pipeline['state'] == 'historical' and not pipeline['dirty'] and not revisit:
                return pipeline
            if photo['media_type'] != 'image':
                return self.finish(photo, 'not_applicable', '非图片资料保留原件，按原流程人工选材')
            if photo.get('watermark_dedup_reason'):
                return self.finish(photo, 'needs_check', photo['watermark_dedup_reason'])
            task = task or self.source_task(photo_id)
            if task:
                try:
                    self.task_permission(task)
                except DomainError as exc:
                    return self.finish(photo, 'needs_check', str(exc))
            record = self.db.one("SELECT * FROM recognitions WHERE photo_id=? AND provider='local_ocr' ORDER BY created_at DESC,rowid DESC LIMIT 1", (photo_id,))
            local_fields = json.loads(record['suggestions']).get('fields', {}) if record and record['state'] == 'completed' else {}
            # Only original receipt text and local OCR can supply rule identifiers.
            source = '\n'.join(s.get('metadata', {}).get('context_text', '') for s in photo.get('sources', [])
                if task and s.get('metadata', {}).get('chat_id') == task['chat_id']
                and s.get('metadata', {}).get('feishu_message_id') == (task['input'].get('photo_source_message_id') or task['source_message_id']))
            text = '\n'.join([record['raw_text'] if record and record['state'] == 'completed' else '', source])
            fields = {**local_fields, **extract_fields(text)}
            confirmed = set(photo['confirmed_fields'])
            identity, matched = 'unverified', None
            app_capture = self.db.one('SELECT metadata FROM camera_official_captures WHERE photo_id=? AND archived_at IS NOT NULL', (photo_id,))
            if app_capture and photo['case_id'] and photo.get('uploaded_by') and photo['recognition_state'] != 'conflict':
                from .mobile_camera import case_number
                capture = json.loads(app_capture['metadata'])
                assigned = self.domain.case(photo['case_id'])
                if capture.get('case_id') == assigned['id'] and capture.get('case_number') == case_number(assigned):
                    # The authenticated APP supplied an explicit case and capture
                    # purpose. This is assignment evidence, not an official
                    # watermark authenticity or human-review claim.
                    identity, matched = 'app_assigned_case', assigned['id']
            if not matched and photo['case_id'] and 'case_id' in confirmed and photo['recognition_state'] != 'conflict':
                identity, matched = 'human_confirmed', photo['case_id']
            elif not matched and (fields.get('platform_report_code') or fields.get('short_code')):
                _, matched = self.recognizer.match(text, fields, photo['project_id'])
                if matched:
                    identity = 'official_identifier' if fields.get('platform_report_code') else 'short_identifier'
            if not self.storage.verify(photo):
                return self.finish(photo, 'storage_error', '原件缺失或校验失败，未准备草稿', fields=fields)
            if not matched:
                reason = '编号未唯一匹配，需核对归属' if fields.get('platform_report_code') or fields.get('short_code') or fields.get('identifier_conflict') else '缺少唯一案件编号或人工确认，仅保留候选归属'
                if not record and not source:
                    reason = '等待本地 OCR 或人工确认编号与用途'
                return self.finish(photo, 'needs_check', reason, fields=fields)
            case = self.domain.case(matched)
            if task:
                try:
                    self.task_permission(task, case)
                except DomainError as exc:
                    return self.finish(photo, 'needs_check', str(exc), fields=fields)
            if photo['case_id'] and photo['case_id'] != matched or 'case_id' in confirmed and photo['case_id'] != matched:
                return self.finish(photo, 'needs_check', '编号与已有归属不同，保留原记录等待人工核对', fields=fields)
            conflicts = self.purpose_conflicts(photo, fields, confirmed)
            if conflicts or photo['recognition_state'] == 'conflict' and photo['case_id']:
                return self.finish(photo, 'needs_check', '用途或归属存在未解决冲突，未自动选材', fields=fields)
            if not photo['case_id']:
                # A newly synced case can resolve a formerly unknown exact ID.
                # Check version and confirmations again inside the write lock.
                with self.db.tx() as con:
                    current = con.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
                    if current['revision'] != photo['revision'] or current['case_id'] or 'case_id' in json.loads(current['confirmed_fields']):
                        raise DomainError('照片已被修改，等待重新核对', 409)
                    if con.execute('SELECT id FROM photos WHERE case_id=? AND blob_hash=? AND reuse_source_id IS NULL', (matched, photo['blob_hash'])).fetchone():
                        return self.finish(photo, 'needs_check', '此案件已有相同原件，请人工合并来源', fields=fields, con=con)
                    con.execute("UPDATE photos SET case_id=?,project_id=?,recognition_state='recognized',revision=revision+1,updated_at=? WHERE id=?", (matched, case['project_id'], now(), photo_id))
                    audit(con, 'photo', photo_id, 'rule_associate', after={'case_id': matched, 'basis': identity}, actor='rule')
                photo = self.domain.photo(photo_id)
            if photo['photo_group'] not in PHOTO_GROUPS or photo['role'] in ('unknown', 'other'):
                return self.finish(photo, 'needs_check', '已归案，用途或楼栋／入户门角色仍待确认', identity=identity, fields=fields)
            if not self.policy().get('auto_drafts'):
                return self.finish(photo, 'associated', '规则归案完成，自动准备草稿已暂停', identity=identity, fields=fields)
            try:
                with self.platform.material_lock:
                    result = prepare_materials(self.platform, case, [photo], actor='rule')
                self.finish(photo, result['state'], result.get('reason', ''), identity=identity, fields=fields, draft=result)
                if task and result['state'] == 'local_draft':
                    self.intent(task, case, photo, result)
                return result
            except DomainError as exc:
                return self.finish(photo, 'needs_check', str(exc), identity=identity, fields=fields)

    def finish(self, photo, state, reason, *, identity='unverified', fields=None, draft=None, con=None):
        fields = fields or {}
        args = (photo['id'], digest([photo['revision'], state, identity]), fields.get('platform_report_code', ''), fields.get('short_code', ''),
                identity, state, reason, dump(draft or {}), photo['revision'], now())
        sql = 'INSERT INTO photo_pipeline(photo_id,dirty,signature,official_code,short_code,identity,state,reason,draft_json,photo_revision,updated_at) VALUES(?,0,?,?,?,?,?,?,?,?,?) ON CONFLICT(photo_id) DO UPDATE SET dirty=0,signature=excluded.signature,official_code=excluded.official_code,short_code=excluded.short_code,identity=excluded.identity,state=excluded.state,reason=excluded.reason,draft_json=excluded.draft_json,photo_revision=excluded.photo_revision,updated_at=excluded.updated_at'
        if con is not None:
            con.execute(sql, args)
        else:
            with self.db.tx() as con:
                if con.execute('SELECT revision FROM photos WHERE id=?', (photo['id'],)).fetchone()['revision'] != photo['revision']:
                    return {'state': 'queued', 'reason': '照片已更新，等待重新核对'}
                con.execute(sql, args)
        return {'state': state, 'identity': identity, 'reason': reason, 'draft': draft}

    def intent(self, task, case, photo, result):
        if self.db.setting('automatic_upload_policy', {}).get('enabled'):
            return  # The case-level queue owns incremental saves when enabled.
        if task['created_at'] < self.policy().get('activated_at', ''):
            return
        binding = self.task_permission(task, case)
        if not binding['save_drafts']:
            return
        signature = digest([photo['id'], photo['revision'], photo['blob_hash'], task['binding_revision'], case['workflow_stage']])
        with self.db.tx() as con:
            con.execute('INSERT OR IGNORE INTO rule_draft_intents(id,intent_key,task_id,case_id,stage,photo_ids,signature,case_signature,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (uid(), digest([task['id'], case['id'], signature]), task['id'], case['id'], case['workflow_stage'], dump([{k: photo[k] for k in ('id','revision','blob_hash','case_id')}]), signature, self.case_signature(case), dump(result), now(), now()))

    @staticmethod
    def case_signature(case):
        return digest({k: case[k] for k in ('id','project_id','platform_report_code','short_code','workflow_stage')})

    def save_one(self, task_id=None):
        if self.db.setting('automatic_upload_policy', {}).get('enabled'):
            return False
        if not self.policy().get('rules_enabled') or not self.policy().get('auto_drafts') or self._stop.is_set():
            return False
        sql = "SELECT * FROM rule_draft_intents WHERE state IN ('queued','awaiting_login','read_only')"
        row = self.db.one(sql + (' AND task_id=?' if task_id else " AND task_id NOT IN (SELECT id FROM feishu_agent_tasks WHERE state IN ('queued','running'))") + ' ORDER BY created_at LIMIT 1', (task_id,) if task_id else ())
        if not row:
            return False
        with self.platform.material_lock, case_context(self.platform, row['case_id']):
            # Single shared lock with Agent saves; persisted claim prevents replay.
            current = self.db.one('SELECT state FROM rule_draft_intents WHERE id=?', (row['id'],))
            if current['state'] not in ('queued','awaiting_login','read_only'):
                return False
            def permitted():
                if self._stop.is_set() or not all(self.policy().get(k) for k in ('rules_enabled','auto_drafts')):
                    raise DomainError('自动草稿已暂停', 409)
                task, case = self.feishu.get(row['task_id']), self.domain.case(row['case_id'])
                self.task_permission(task, case, saving=True)
                if self.case_signature(case) != row['case_signature']:
                    raise DomainError('案件编号、项目或阶段已变化，旧保存意图失效', 409)
                for expected in json.loads(row['photo_ids']):
                    photo = self.domain.photo(expected['id'])
                    if any(photo[k] != expected[k] for k in expected) or not self.storage.verify(photo):
                        raise DomainError('资料版本或原件已变化，旧保存意图失效', 409)
                valid = {p['photo_id'] for p in self.db.all(
                    "SELECT r.photo_id FROM photo_pipeline r JOIN photos p ON p.id=r.photo_id WHERE p.case_id=? AND r.dirty=0 AND r.photo_revision=p.revision AND r.identity<>'unverified' AND r.state IN ('local_draft','already_saved')", (case['id'],))}
                check_material_scope(self.platform, case, valid & set(task['input'].get('photo_ids', [])))
                return task, case
            try:
                task, case = permitted()
                previous = self.db.one('SELECT review_id FROM feishu_agent_drafts WHERE task_id=? AND case_id=?', (task['id'], case['id']))
                if previous and previous['review_id']:
                    record = self.feishu.reviews.get(previous['review_id'])
                    expected_ids = {p['id'] for p in json.loads(row['photo_ids'])}
                    covered = expected_ids <= set(record['result'].get('uploaded', {}))
                    if record['state'] in ('saved','uncertain','submitting','submitted','verified') or record['state'] == 'save_verified' and covered:
                        self.save_result(row, record['state'], record['result'], record['id'])
                        return True
                if not self.platform.client.status()['connected']:
                    self.save_result(row, 'awaiting_login', {'reason': '官网尚未连接，本地草稿已保留'})
                    return False
                if not self.platform.client.write_enabled:
                    self.save_result(row, 'read_only', {'reason': '官网写入已关闭，本地草稿已保留'})
                    return False
                self.feishu.workflow.refresh_case(case['id'])
                task, case = permitted()
                # Require all current selections and fields; do not reconstruct or
                # re-add anything after a user edits the local draft.
                plan = self.platform.preview(case['id'], row['stage'])
                selected = {p['id'] for field in plan['file_fields'] for p in field['selected']}
                if any(p['id'] not in selected for p in json.loads(row['photo_ids'])):
                    raise DomainError('本次选材已被移除，旧保存意图失效', 409)
                if plan['error_count']:
                    self.save_result(row, 'needs_fields', {'reason': '必填字段或资料不全，请人工补全后保存'})
                    return True
                record = self.feishu.reviews.prepare(case['id'], row['stage'], plan['draft']['revision'])
                self.save_result(row, 'running', {}, record['id'])
                record = self.feishu.reviews.save_for_agent(record['id'], record['fingerprint'], check_permission=permitted)
                self.save_result(row, record['state'], record['result'], record['id'])
            except Exception as exc:
                current = self.db.one('SELECT review_id FROM rule_draft_intents WHERE id=?', (row['id'],))
                record = self.feishu.reviews.get(current['review_id']) if current['review_id'] else None
                state = record['state'] if record else 'stale' if isinstance(exc, DomainError) and exc.status == 409 else 'failed'
                self.save_result(row, state, {**(record['result'] if record else {}), 'reason': str(exc)[:400] if isinstance(exc, DomainError) else '官网操作未完成，请核对原记录'}, current['review_id'])
            return True

    def save_result(self, row, state, result, review_id=None):
        result = {**result, 'state': state, 'business_submitted': False, 'human_review_required': True}
        with self.db.tx() as con:
            con.execute('UPDATE rule_draft_intents SET state=?,result_json=?,review_id=COALESCE(?,review_id),updated_at=? WHERE id=?', (state, dump(result), review_id, now(), row['id']))
            con.execute('INSERT INTO feishu_agent_drafts VALUES(?,?,?,?,?,?) ON CONFLICT(task_id,case_id) DO UPDATE SET stage=excluded.stage,review_id=COALESCE(excluded.review_id,feishu_agent_drafts.review_id),result=excluded.result,updated_at=excluded.updated_at',
                (row['task_id'], row['case_id'], row['stage'], review_id, dump(result), now()))

    def run_once(self):
        if not self.policy().get('rules_enabled'):
            return 0
        rows = self.db.all('SELECT photo_id FROM photo_pipeline WHERE dirty=1 ORDER BY updated_at LIMIT 20')
        for row in rows:
            try:
                self.process(row['photo_id'])
            except DomainError:
                continue
        return len(rows)

    def start(self):
        if self._threads:
            return
        with self.db.tx() as con:
            con.execute("UPDATE rule_draft_intents SET state='needs_check',error='服务中断，请先核对原审核批次',updated_at=? WHERE state='running'", (now(),))
        self._stop.clear()
        for saving in (False, True):
            thread = threading.Thread(target=self._loop, args=(saving,), daemon=True, name='idi-rule-save' if saving else 'idi-photo-rules')
            self._threads.append(thread)
            thread.start()

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads.clear()

    def _loop(self, saving):
        while not self._stop.is_set():
            try:
                self.save_one() if saving else self.run_once()
            except Exception:
                log.exception('Photo workflow iteration failed')
            self._stop.wait(5 if saving else 2)
