from __future__ import annotations

import json
import logging
import threading
from typing import Callable

from .db import Database, dump, now, uid
from .domain import DomainError
from .identity import acting_as, current

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, db: Database):
        self.db = db
        self.handlers: dict[str, Callable] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._network_thread = None
        self.access = None

    def register(self, kind: str, handler: Callable):
        self.handlers[kind] = handler

    def enqueue(self, kind: str, payload: dict, *, result: dict | None = None):
        if kind not in self.handlers:
            raise DomainError("任务类型未注册")
        job_id = uid()
        person = current()
        with self.db.tx() as con:
            con.execute("INSERT INTO jobs(id,kind,payload,created_at,user_id,state,result,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                        (job_id, kind, dump(payload), now(), person.id if person else None,
                         'completed' if result is not None else 'queued', dump(result) if result is not None else None,
                         now() if result is not None else None))
        if result is None:
            self._wake.set()
        return self.get(job_id)

    def get(self, job_id):
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise DomainError("任务不存在", 404)
        if current() and not current().admin and row['user_id'] != current().id:
            raise DomainError('没有此任务的访问权限', 403)
        row["payload"] = json.loads(row["payload"])
        row["result"] = json.loads(row["result"]) if row["result"] else None
        self.check_scope(row)
        return row

    def check_scope(self, row):
        person = current()
        if person is None or person.admin:
            return
        payload, projects = row['payload'], set()
        if payload.get('project_id'):
            projects.add(payload['project_id'])
        if payload.get('batch_id'):
            batch = self.db.one('SELECT project_id,case_id FROM import_batches WHERE id=?', (payload['batch_id'],))
            if batch:
                projects.add(batch['project_id'])
                if batch['case_id']:
                    case = self.db.one('SELECT project_id FROM cases WHERE id=?', (batch['case_id'],))
                    projects.add(case['project_id'] if case else None)
        case_ids = [g.get('case', {}).get('id') for g in payload.get('groups', [])]
        if payload.get('case_id'):
            case_ids.append(payload['case_id'])
        for case_id in case_ids:
            case = self.db.one('SELECT project_id FROM cases WHERE id=?', (case_id,))
            projects.add(case['project_id'] if case else None)
        for photo_id in payload.get('photo_ids', []):
            photo = self.db.one('SELECT project_id FROM photos WHERE id=?', (photo_id,))
            projects.add(photo['project_id'] if photo else None)
        if not projects or any(not person.allows(p) for p in projects):
            raise DomainError('任务涉及的项目权限已变化', 403)

    def list(self):
        person = current()
        own = person is not None and not person.admin
        rows = self.db.all("SELECT id FROM jobs" + (" WHERE user_id=?" if own else "") +
                           " ORDER BY created_at DESC LIMIT 100", (person.id,) if own else ())
        visible = []
        for row in rows:
            try:
                visible.append(self.get(row['id']))
            except DomainError as exc:
                if not own:
                    raise exc
        return visible

    def retry(self, job_id):
        self.get(job_id)
        with self.db.tx() as con:
            row = con.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise DomainError("任务不存在", 404)
            if row["state"] not in ("failed", "partial"):
                raise DomainError("只有失败或部分完成的任务可以重试", 409)
            con.execute("UPDATE jobs SET state='queued',error=NULL,finished_at=NULL WHERE id=?", (job_id,))
        self._wake.set()
        return self.get(job_id)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        with self.db.tx() as con:
            con.execute("UPDATE jobs SET state='queued',error='服务中断后恢复排队' WHERE state='running'")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=('local',), name="idi-worker", daemon=True)
        self._thread.start()
        self._network_thread = threading.Thread(target=self._loop, args=('network',), name='idi-platform-worker', daemon=True)
        self._network_thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._network_thread:
            self._network_thread.join(timeout=10)

    def run_one(self, lane=None):
        with self.db.tx() as con:
            restriction = " AND kind='platform_sync'" if lane == 'network' else " AND kind<>'platform_sync'" if lane == 'local' else ''
            row = con.execute("SELECT * FROM jobs WHERE state='queued'" + restriction + " ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return False
            row = dict(row)
            con.execute("UPDATE jobs SET state='running',started_at=?,attempts=attempts+1,error=NULL WHERE id=?", (now(), row["id"]))
        def progress(done, total):
            with self.db.tx() as con:
                con.execute("UPDATE jobs SET progress=?,total=? WHERE id=?", (done, total, row["id"]))
        try:
            handler = self.handlers[row["kind"]]
            person = self.access.principal(row['user_id']) if row.get('user_id') and self.access else None
            if row.get('user_id') and person is None:
                raise DomainError('任务人员身份无法恢复', 403)
            with acting_as(person):
                result = handler(json.loads(row["payload"]), progress) or {}
            state = result.get("state", "completed")
            if state not in ("completed", "failed", "partial"):
                state = "completed"
            with self.db.tx() as con:
                con.execute("UPDATE jobs SET state=?,result=?,finished_at=? WHERE id=?", (state, dump(result), now(), row["id"]))
        except Exception as error:
            log.exception("Job failed: %s %s", row["kind"], row["id"])
            with self.db.tx() as con:
                con.execute("UPDATE jobs SET state='failed',error=?,finished_at=? WHERE id=?", (str(error)[:3000], now(), row["id"]))
        return True

    def _loop(self, lane):
        while not self._stop.is_set():
            try:
                if not self.run_one(lane):
                    self._wake.wait(1)
                    self._wake.clear()
            except Exception:
                log.exception("Worker loop error")
                self._stop.wait(2)
