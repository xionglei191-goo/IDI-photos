"""Only read-only photo analysis is eligible for automatic model recovery."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from contextlib import nullcontext

from .db import dump, now, uid
from .domain import DomainError
from .model_runtime import ModelUnavailable, config_key

log = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


class PhotoAI:
    def __init__(self, db, domain, storage, recognizer, followup):
        self.db, self.domain, self.storage = db, domain, storage
        self.recognizer, self.followup = recognizer, followup
        self.runtime = recognizer.runtime
        self._stop, self._wake, self._lock = threading.Event(), threading.Event(), threading.Lock()
        self._thread = None

    def enqueue(self, kind, photo, signature, *, source='followup', source_id='', context=None, force=False, connection=None):
        config = self.db.setting('recognition', {})
        context = {**(context or {}), 'photo_revision': photo['revision'], 'blob_hash': photo['blob_hash'],
                   'case_id': photo['case_id'], 'project_id': photo['project_id'], 'model': config.get('model', '')}
        key = digest([kind, photo['id'], signature, source, context.get('connector_id'), context.get('binding_revision'), config_key(config)])
        if force:
            key += ':retry:' + str(context['retry_of'])
        stamp, job_id = now(), uid()
        with (nullcontext(connection) if connection is not None else self.db.tx()) as con:
            con.execute('INSERT OR IGNORE INTO photo_ai_tasks(id,task_key,photo_id,kind,source,source_id,signature,config_key,context_json,deferred,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (job_id, key, photo['id'], kind, source, source_id, signature, config_key(config), dump(context), int(not self.runtime.status()['can_attempt']), stamp, stamp))
            row = dict(con.execute('SELECT * FROM photo_ai_tasks WHERE task_key=?', (key,)).fetchone())
        self._wake.set()
        return row

    def quality(self, photo, case, signature, *, requested=False):
        row = self.db.one("SELECT * FROM photo_ai_tasks WHERE photo_id=? AND kind='quality' AND signature=? AND config_key=? ORDER BY created_at DESC,id DESC LIMIT 1",
                          (photo['id'], signature, config_key(self.runtime.config())))
        if row and not (requested and row['state'] in ('cancelled', 'stale')):
            return row
        activation = self.db.setting('workflow_policy', {}).get('activated_at', '')
        pending = self.db.one("SELECT id FROM photo_ai_tasks WHERE photo_id=? AND state IN ('queued','waiting','running')", (photo['id'],))
        if not requested and photo['updated_at'] < activation and not pending:
            return None
        return self.enqueue('quality', photo, signature, context={'case_signature': digest(self.followup.case_evidence(case)), **({'retry_of': row['id']} if row else {})}, force=bool(row))

    def classification(self, task, photo):
        signature = digest([photo['blob_hash'], photo['revision'], task['binding_revision'], task['connector_id']])
        return self.enqueue('classification', photo, signature, source='feishu', source_id=task['id'],
                            context={'connector_id': task['connector_id'], 'binding_revision': task['binding_revision'],
                                     'allowed_project_ids': task['input']['allowed_project_ids']})

    def eligible(self, row, *, check_config=True):
        context = json.loads(row['context_json'])
        photo = self.domain.photo(row['photo_id'])
        if any(photo[k] != context[k] for k in ('blob_hash', 'case_id', 'project_id')) or photo['revision'] != context['photo_revision']:
            raise DomainError('照片归属、内容或人工字段已变化，旧分析已失效', 409)
        if check_config and config_key(self.runtime.config()) != row['config_key']:
            raise DomainError('模型配置已变化，旧分析已失效', 409)
        case = self.domain.case(photo['case_id']) if photo['case_id'] else None
        if row['source'] == 'followup':
            if not self.followup.config().get('ai_review') or not case or not case['followup_eligible']:
                raise DomainError('自动内容检查已暂停或案件不在跟进范围', 409)
            if digest(self.followup.case_evidence(case)) != context['case_signature']:
                raise DomainError('案件依据已变化，旧分析已失效', 409)
        else:
            binding = self.db.one('SELECT * FROM feishu_agent_bindings WHERE connector_id=?', (context['connector_id'],))
            connector = self.db.one('SELECT enabled,config FROM connectors WHERE id=?', (context['connector_id'],))
            task = self.db.one('SELECT state FROM feishu_agent_tasks WHERE id=?', (row['source_id'],))
            if not binding or not binding['enabled'] or not binding['images_enabled'] or not binding['ai_enabled'] or not connector or not connector['enabled'] or not task or task['state'] == 'cancelled':
                raise DomainError('原收图范围已暂停，本次不再补分析', 409)
            if binding['revision'] != context['binding_revision'] or json.loads(connector['config']).get('chat_id') != binding['chat_id']:
                raise DomainError('原群绑定已变化，本次不再补分析', 409)
            if photo['project_id'] and photo['project_id'] not in json.loads(binding['project_ids']):
                raise DomainError('照片不在原群项目范围，本次不再补分析', 409)
        if not self.storage.verify(photo):
            raise DomainError('原件缺失或内容校验失败')
        return photo, case

    def retry(self, job_id):
        with self.db.tx() as con:
            row = con.execute('SELECT * FROM photo_ai_tasks WHERE id=?', (job_id,)).fetchone()
            existing = con.execute("SELECT * FROM photo_ai_tasks WHERE json_extract(context_json,'$.retry_of')=?", (job_id,)).fetchone()
            if existing:
                return dict(existing)
            if not row or row['state'] not in ('failed', 'waiting'):
                raise DomainError('只能重新分析失败或暂缓的资料', 409)
            row = dict(row)
            if not self.runtime.config().get('vision_enabled'):
                raise DomainError('请先启用外部 AI，再重新分析资料')
            photo, _ = self.eligible(row, check_config=False)
            con.execute("UPDATE photo_ai_tasks SET state='cancelled',error='用户已另行发起重新分析',updated_at=? WHERE id=? AND state IN ('waiting','failed')", (now(), job_id))
            # Cancellation and replacement commit together, excluding a worker
            # claim between them and retaining the old task if insertion fails.
            return self.enqueue(row['kind'], photo, row['signature'], source=row['source'], source_id=row['source_id'],
                                context={**json.loads(row['context_json']), 'retry_of': job_id}, force=True, connection=con)

    def run_one(self):
        if self._stop.is_set() or not self._lock.acquire(blocking=False):
            return False
        try:
            # No per-photo HTTP probe and no new attempt while the provider is paused.
            health = self.runtime.status()
            rows = self.db.all("SELECT * FROM photo_ai_tasks WHERE state IN ('queued','waiting') AND retry_at<=? ORDER BY created_at,id LIMIT 20", (self.runtime.clock(),))
            for row in rows:
                try:
                    photo, case = self.eligible(row)
                except DomainError as exc:
                    self._finish(row['id'], 'stale', error=str(exc))
                    continue
                if row['attempts'] >= 3:
                    self._finish(row['id'], 'failed', error='已达到此资料版本的三次自动分析上限，请人工核对或重新发起')
                    continue
                if not health['can_attempt']:
                    with self.db.tx() as con:
                        con.execute("UPDATE photo_ai_tasks SET deferred=1 WHERE state IN ('queued','waiting') AND deferred=0")
                    return False
                with self.db.tx() as con:
                    changed = con.execute("UPDATE photo_ai_tasks SET state='running',updated_at=? WHERE id=? AND state IN ('queued','waiting')", (now(), row['id']))
                if not changed.rowcount:
                    continue
                self._execute(row, photo, case)
                return True
            return False
        finally:
            self._lock.release()

    def _execute(self, row, photo, case):
        def attempted():
            if self._stop.is_set():
                raise DomainError('服务正在停止，未发出模型请求', 409)
            self.eligible(row)
            with self.db.tx() as con:
                changed = con.execute("UPDATE photo_ai_tasks SET attempts=attempts+1,updated_at=? WHERE id=? AND state='running' AND attempts<3", (now(), row['id']))
                if not changed.rowcount:
                    raise DomainError('分析任务已停止或达到次数限制', 409)
        try:
            with self.runtime.background(attempted):
                result = {'findings': self.followup._vision(photo, case)} if row['kind'] == 'quality' else {
                    'fields': self.recognizer.vision(self.storage.resolve(self.db.one('SELECT stored_path FROM blobs WHERE hash=?', (photo['blob_hash'],))['stored_path']), self.runtime.config()),
                    'notice': '恢复分析仅保存建议，未覆盖人工字段、保存官网或发送群消息。'}
            if row['kind'] == 'quality' and (row['deferred'] or row['attempts']):
                for finding in result['findings']:
                    finding['recovered_analysis'] = True
            self.eligible(row)
            if self._stop.is_set():
                raise DomainError('服务正在停止，未采用迟到结果', 409)
            self._finish(row['id'], 'completed', result=result, expected=row)
            if row['kind'] == 'quality':
                self.followup.request_review(automatic=True)
        except ModelUnavailable as exc:
            current = self.db.one('SELECT attempts FROM photo_ai_tasks WHERE id=?', (row['id'],))
            state = 'waiting' if exc.retryable and current['attempts'] < 3 else 'failed'
            with self.db.tx() as con:
                con.execute('UPDATE photo_ai_tasks SET deferred=1 WHERE id=?', (row['id'],))
            self._finish(row['id'], state, error=str(exc), retry_at=exc.retry_at)
        except Exception as exc:
            self._finish(row['id'], 'stale' if isinstance(exc, DomainError) and exc.status == 409 else 'failed',
                         error=str(exc)[:600] if isinstance(exc, DomainError) else '照片分析未完成，请核对原件与模型响应')

    def _finish(self, job_id, state, *, result=None, error=None, retry_at=0, expected=None):
        with self.db.tx() as con:
            if expected:
                try:
                    self.eligible(expected)
                except DomainError as exc:
                    state, result, error = 'stale', None, str(exc)
            con.execute("UPDATE photo_ai_tasks SET state=?,result_json=?,error=?,retry_at=?,updated_at=?,finished_at=? WHERE id=? AND state NOT IN ('cancelled','stale')",
                        (state, dump(result or {}), error, retry_at, now(), now() if state not in ('queued','waiting') else None, job_id))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        with self.db.tx() as con:
            con.execute("UPDATE photo_ai_tasks SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'waiting' END,deferred=1,error='服务中断，保留次数并等待受控恢复',updated_at=? WHERE state='running'", (now(),))
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='idi-photo-ai', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self.run_one():
                    continue
            except Exception:
                log.exception('Photo AI queue failed')
            self._wake.wait(2)
            self._wake.clear()
