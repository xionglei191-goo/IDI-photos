from __future__ import annotations

import json
import logging
import threading
from typing import Callable

from .db import Database, dump, now, uid
from .domain import DomainError

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, db: Database):
        self.db = db
        self.handlers: dict[str, Callable] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._network_thread = None

    def register(self, kind: str, handler: Callable):
        self.handlers[kind] = handler

    def enqueue(self, kind: str, payload: dict):
        if kind not in self.handlers:
            raise DomainError("任务类型未注册")
        job_id = uid()
        with self.db.tx() as con:
            con.execute("INSERT INTO jobs(id,kind,payload,created_at) VALUES(?,?,?,?)", (job_id, kind, dump(payload), now()))
        self._wake.set()
        return self.get(job_id)

    def get(self, job_id):
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise DomainError("任务不存在", 404)
        row["payload"] = json.loads(row["payload"])
        row["result"] = json.loads(row["result"]) if row["result"] else None
        return row

    def list(self):
        return [self.get(row["id"]) for row in self.db.all("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 100")]

    def retry(self, job_id):
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
