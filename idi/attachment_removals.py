"""Withdraw an attachment from an editable official draft, retaining originals."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from fastapi.responses import FileResponse

from .db import audit, dump, now, uid
from .domain import DomainError
from .platform_client import PlatformWriteError
from .platform_contract import STAGES
from .project_accounts import scoped_case, scoped_record


PENDING = ('removing', 'saved', 'uncertain')


class AttachmentRemovals:
    def __init__(self, platform):
        self.platform, self.db = platform, platform.db
        with self.db.tx() as con:
            con.execute("UPDATE platform_attachment_removals SET state='uncertain',"
                        "error='上次撤回中断，请回读官网核实；不会自动重发' WHERE state='removing'")

    def get(self, removal_id):
        row = self.db.one('SELECT * FROM platform_attachment_removals WHERE id=?', (removal_id,))
        if not row:
            raise DomainError('附件撤回记录不存在', 404)
        snapshot = json.loads(row.pop('snapshot'))
        return {**row, 'file': snapshot['file'], 'label': snapshot['label'],
                'stage_label': STAGES[row['stage']]['label'], 'case_title': snapshot['case_title'],
                'short_code': snapshot['short_code'], 'original_retained': True}

    def pending(self, case_id, stage):
        return [self.get(r['id']) for r in self.db.all(
            "SELECT id FROM platform_attachment_removals WHERE case_id=? AND stage=? "
            "AND state IN ('removing','saved','uncertain') ORDER BY created_at", (case_id, stage))]

    def response(self, removal_id, variant):
        self.get(removal_id)
        if variant not in ('thumbnail', 'original'):
            raise DomainError('未知附件预览方式', 404)
        snapshot = json.loads(self.db.one('SELECT snapshot FROM platform_attachment_removals WHERE id=?',
                                         (removal_id,))['snapshot'])
        blob = self.db.one('SELECT * FROM blobs WHERE hash=?', (snapshot['blob_hash'],))
        if not blob or not self.platform.storage.verify(blob):
            raise DomainError('本地保留原件缺失或校验失败', 409)
        path = blob['thumbnail_path'] if variant == 'thumbnail' else blob['stored_path']
        if not path:
            raise DomainError('此附件没有图片缩略图，请查看原件', 404)
        return FileResponse(self.platform.storage.resolve(path),
                            media_type='image/jpeg' if variant == 'thumbnail' else blob['mime_type'],
                            content_disposition_type='inline',
                            headers={'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})

    def require_clear(self, case_id, stage, except_id=''):
        if any(r['id'] != except_id for r in self.pending(case_id, stage)):
            raise DomainError('本环节有照片撤回结果待核实，请先回读撤回结果', 409)

    def _editable(self, case_id, stage):
        self.platform.require_editable(case_id, stage)
        if stage not in ('survey', 'repair'):
            raise DomainError('此环节没有独立保存功能，不能通过撤回附件改变已提交资料', 409)
        session = self.platform.client.status()
        if not session['connected']:
            raise DomainError('请先连接官网，再撤回已上传照片', 401)
        if not self.platform.client.write_enabled:
            raise DomainError('官网当前为只读模式，不能撤回照片', 403)
        link = self.platform.link(case_id)
        accounts = getattr(self.platform, 'project_accounts', None)
        expected = accounts.binding(self.platform.domain.case(case_id)['project_id'])['username'] if accounts else link['snapshot'].get('account')
        if expected != session['account']:
            raise DomainError('官网登录账号已变化，请刷新案件后重新核对', 409)
        key = 'siteSurveyState' if stage == 'survey' else 'repairAcceptanceState'
        if str(link['snapshot'].get('row', {}).get(key)) != '0':
            raise DomainError('官网本环节已提交或保存状态不明，请先在官网核实', 409)
        if self.db.one("SELECT id FROM platform_review_batches WHERE case_id=? AND stage=? "
                       "AND state IN ('submitting','submitted','saved','uncertain')", (case_id, stage)):
            raise DomainError('本环节还有保存或提交结果待核实，请先回读官网结果', 409)

    def _snapshot(self, case_id, stage, field_key, file_id):
        self._editable(case_id, stage)
        plan = self.platform.preview(case_id, stage)
        field = next((f for f in plan['file_fields'] if f['key'] == field_key), None)
        if not field or field.get('readonly'):
            raise DomainError('此附件栏由其他环节带入，不能在本环节删除', 409)
        matches = [f for f in field['existing'] if str(f.get('id', '')) == file_id]
        if len(matches) != 1:
            raise DomainError('此照片已变化或不在当前栏位，请刷新后重新核对', 409)
        # Use only official values. A withdrawal must not save unrelated local edits.
        remote = self.platform._remote(plan['link'], stage)
        keys = {f['key'] for f in STAGES[stage]['fields']}
        keys.update(f['name_key'] for f in STAGES[stage]['fields'] if f.get('name_key'))
        keys.update(('reportReceivingId', 'repairPersonName', 'repairPersonPhone'))
        record_key = 'siteSurveyId' if stage == 'survey' else 'repairAcceptanceId'
        keys.add(record_key)
        if stage == 'survey':
            keys.update(('communityInfoId', 'engineeringModelingParentId', 'repairUnitId'))
        before = {k: deepcopy(remote[k]) for k in keys if k in remote}
        if stage == 'survey':
            context = {**plan['link']['snapshot'].get('claim', {}), **remote}
            for key in ('communityInfoId', 'engineeringModelingParentId', 'repairUnitId'):
                if key in context:
                    before[key] = context[key]
        before['reportReceivingId'] = plan['link']['report_receiving_id']
        before[record_key] = plan['link']['snapshot']['row'].get(record_key)
        if not before[record_key]:
            raise DomainError('官网缺少当前环节记录编号，未撤回照片', 409)
        before['state'] = 0
        identity = {'target': before['reportReceivingId'], 'account': plan['link']['snapshot']['account'],
                    'stage': stage, 'field': field_key, 'file_id': file_id, 'payload': before}
        if getattr(self.platform, 'project_accounts', None):
            identity['write_binding'] = self.platform.project_accounts.binding(plan['case']['project_id'])
        fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return plan, field, matches[0], before, fingerprint

    @scoped_case()
    def prepare(self, case_id, stage, field_key, file_id):
        with self.platform.material_lock:
            self.require_clear(case_id, stage)
            self._editable(case_id, stage)
            self.platform.workflow.refresh_case(case_id)
            plan, field, file, before, fingerprint = self._snapshot(case_id, stage, field_key, file_id)
            blob, _ = self.platform.media.ensure(plan, field_key, file_id)
            snapshot = {'before': before, 'file': file, 'label': field['label'], 'blob_hash': blob['hash'],
                        'case_title': plan['case']['title'], 'short_code': plan['case']['short_code'],
                        'target': plan['link']['report_receiving_id'], 'account': plan['link']['snapshot']['account']}
            if getattr(self.platform, 'project_accounts', None):
                snapshot['write_binding'] = self.platform.project_accounts.binding(plan['case']['project_id'])
            removal_id, stamp = uid(), now()
            with self.db.tx() as con:
                con.execute('INSERT INTO platform_attachment_removals'
                            '(id,case_id,stage,field_key,file_id,fingerprint,snapshot,created_at,updated_at)'
                            ' VALUES(?,?,?,?,?,?,?,?,?)',
                            (removal_id, case_id, stage, field_key, file_id, fingerprint, dump(snapshot), stamp, stamp))
            return self.get(removal_id)

    def _exclude(self, con, case_id, stage, field_key, blob_hash):
        stamp = now()
        con.execute('INSERT OR IGNORE INTO platform_attachment_blocks VALUES(?,?,?,?,?)',
                    (case_id, stage, field_key, blob_hash, stamp))
        ids = {r['id'] for r in con.execute('SELECT id FROM photos WHERE case_id=? AND blob_hash=?',
                                            (case_id, blob_hash))}
        for photo_id in ids:
            con.execute('INSERT OR IGNORE INTO platform_draft_exclusions VALUES(?,?,?,?,?)',
                        (case_id, stage, field_key, photo_id, stamp))
        draft = self.platform.draft(case_id, stage)
        files = deepcopy(draft['files'])
        files[field_key] = [p for p in files.get(field_key, []) if p not in ids]
        con.execute("INSERT INTO platform_drafts VALUES(?,?,?,?,?,?,?) ON CONFLICT(case_id,stage) "
                    "DO UPDATE SET files_json=excluded.files_json,revision=platform_drafts.revision+1,updated_at=excluded.updated_at",
                    (case_id, stage, dump(draft['values']), dump(files), draft['target_report_id'],
                     draft['revision'] + 1, stamp))
        self._invalidate(con, case_id, stage)

    @staticmethod
    def _invalidate(con, case_id, stage):
        stamp = now()
        con.execute("UPDATE platform_review_batches SET state='stale' WHERE case_id=? AND stage=? "
                    "AND state IN ('prepared','reviewed','failed')", (case_id, stage))
        con.execute("UPDATE automatic_upload_cases SET signature='',review_guard='',review_id=NULL,"
                    "state='queued',retry_at=0,updated_at=? WHERE case_id=?", (stamp, case_id))
        con.execute("UPDATE review_notifications SET state='cancelled',error='附件已修改，等待重新核对' "
                    "WHERE case_id=? AND state IN ('queued','failed')", (case_id,))

    def remove_local(self, case_id, stage, field_key, photo_id, revision):
        with self.platform.material_lock, self.db.tx() as con:
            self.platform.require_editable(case_id, stage, con)
            self.require_clear(case_id, stage)
            plan = self.platform.preview(case_id, stage)
            field = next((f for f in plan['file_fields'] if f['key'] == field_key and not f.get('readonly')), None)
            photo = next((p for p in (field or {}).get('selected', []) if p['id'] == photo_id), None)
            if not photo or type(revision) is not int or revision != plan['draft']['revision']:
                raise DomainError('照片选材已变化，请刷新后重新核对', 409)
            self._exclude(con, case_id, stage, field_key, photo['blob_hash'])
            audit(con, 'platform_attachment', photo_id, 'remove_local_selection',
                  after={'case_id': case_id, 'stage': stage, 'field': field_key, 'original_retained': True})
        return self.platform.preview(case_id, stage)

    def _state(self, removal_id, state, error=''):
        with self.db.tx() as con:
            con.execute('UPDATE platform_attachment_removals SET state=?,error=?,updated_at=? WHERE id=?',
                        (state, error, now(), removal_id))
        return self.get(removal_id)

    @scoped_record('platform_attachment_removals', 'withdraw_attachment')
    def confirm(self, removal_id, fingerprint):
        with self.platform.material_lock:
            record = self.get(removal_id)
            if fingerprint != record['fingerprint']:
                raise DomainError('删除确认版本不一致，请重新核对', 409)
            if record['state'] == 'verified':
                return record
            if record['state'] != 'prepared':
                raise DomainError('本次撤回已执行或结果待核实，请回读结果，勿重复发送', 409)
            case_id, stage = record['case_id'], record['stage']
            self.require_clear(case_id, stage)
            try:
                self._editable(case_id, stage)
                self.platform.workflow.refresh_case(case_id)
                _, _, file, before, current = self._snapshot(case_id, stage, record['field_key'], record['file_id'])
                if current != fingerprint:
                    raise DomainError('官网资料已变化，本次未删除，请重新核对照片', 409)
                payload = deepcopy(before)
                payload[record['field_key']] = [f for f in payload[record['field_key']]
                                                if not isinstance(f, dict) or str(f.get('id', '')) != record['file_id']]
                stored = json.loads(self.db.one('SELECT snapshot FROM platform_attachment_removals WHERE id=?',
                                               (removal_id,))['snapshot'])
                blob = self.db.one('SELECT * FROM blobs WHERE hash=?', (stored['blob_hash'],))
                if not blob or not self.platform.storage.verify(blob):
                    raise DomainError('保留原件缺失或校验失败，本次未撤回', 409)
                with self.db.tx() as con:
                    self._exclude(con, case_id, stage, record['field_key'], stored['blob_hash'])
                    con.execute("UPDATE platform_attachment_removals SET state='removing',updated_at=? WHERE id=?",
                                (now(), removal_id))
                    audit(con, 'platform_attachment', removal_id, 'withdraw_requested',
                          before={'file': file, 'field': record['field_key'], 'case_id': case_id, 'stage': stage},
                          after={'original_retained': True, 'payload': payload})
            except DomainError as error:
                self._state(removal_id, 'stale', str(error))
                raise
            attempted = False
            try:
                # Keep local snapshot/account edits out of the final write window.
                with self.db.tx() as con, self.platform.client._lock:
                    self._editable(case_id, stage)
                    _, _, _, _, current = self._snapshot(case_id, stage, record['field_key'], record['file_id'])
                    if current != fingerprint:
                        raise DomainError('撤回前官网资料已变化，请重新核对', 409)
                    attempted = True
                    self.platform.client.save_draft_only(stage, payload)
                    con.execute("UPDATE platform_attachment_removals SET state='saved',updated_at=? WHERE id=?",
                                (now(), removal_id))
            except Exception as error:
                rejected = not attempted or isinstance(error, PlatformWriteError) and error.rejected
                return self._state(removal_id, 'failed' if rejected else 'uncertain', str(error))
            return self.reconcile(removal_id)

    @scoped_record('platform_attachment_removals', 'withdraw_readback')
    def reconcile(self, removal_id):
        with self.platform.material_lock:
            record = self.get(removal_id)
            if record['state'] == 'verified':
                return record
            if record['state'] not in PENDING:
                raise DomainError('此记录尚未执行官网撤回', 409)
            snapshot = json.loads(self.db.one('SELECT snapshot FROM platform_attachment_removals WHERE id=?',
                                             (removal_id,))['snapshot'])
            case_id, stage = record['case_id'], record['stage']
            try:
                session = self.platform.client.status()
                if not session['connected'] or session['account'] != snapshot['account']:
                    raise DomainError('请使用原官网账号连接后回读撤回结果', 409)
                if self.platform.link(case_id)['report_receiving_id'] != snapshot['target']:
                    raise DomainError('案件官网关联已变化，请核对原案件撤回结果', 409)
                self.platform.workflow.refresh_case(case_id)
                link = self.platform.link(case_id)
                remote = self.platform._remote(link, stage)
                if stage == 'survey':
                    for key in ('communityInfoId', 'engineeringModelingParentId', 'repairUnitId'):
                        if key not in remote and key in link['snapshot'].get('claim', {}):
                            remote[key] = link['snapshot']['claim'][key]
                expected = deepcopy(snapshot['before'])
                expected[record['field_key']] = [f for f in expected[record['field_key']]
                                                 if not isinstance(f, dict) or str(f.get('id', '')) != record['file_id']]
                mismatches = []
                for key, value in expected.items():
                    if key == 'state':
                        continue
                    actual = remote.get(key)
                    if isinstance(value, list):
                        def identities(items):
                            return sorted((str(f['id']), str(f.get('fileUrl', ''))) for f in (items or [])
                                          if isinstance(f, dict) and f.get('id')) if isinstance(items, list) else []
                        if identities(actual) != identities(value):
                            mismatches.append(key)
                    elif str(actual or '') != str(value or ''):
                        mismatches.append(key)
                state_key = 'siteSurveyState' if stage == 'survey' else 'repairAcceptanceState'
                if (self.platform.domain.case(case_id)['workflow_stage'] != stage
                        or str(link['snapshot']['row'].get(state_key)) != '0'):
                    mismatches.append('办理进度')
                if mismatches:
                    labels = {f['key']: f['label'] for f in STAGES[stage]['fields']}
                    return self._state(removal_id, record['state'],
                                       '官网回读尚未确认撤回一致，请继续回读或在官网核对：'
                                       + '、'.join(dict.fromkeys(labels.get(k, '办理进度' if k == '办理进度' else '案件资料') for k in mismatches)))
            except Exception as error:
                return self._state(removal_id, record['state'], '撤回结果待核实：' + str(error))
            with self.db.tx() as con:
                con.execute("UPDATE platform_attachment_removals SET state='verified',error='',updated_at=? WHERE id=?",
                            (now(), removal_id))
                self._invalidate(con, case_id, stage)
                audit(con, 'platform_attachment', removal_id, 'withdraw_verified',
                      after={'case_id': case_id, 'stage': stage, 'file_id': record['file_id'], 'original_retained': True})
            return self.get(removal_id)
