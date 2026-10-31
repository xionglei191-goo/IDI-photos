"""Bounded background scheduling signals, work slots and diagnostic counters."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class WakeSignal:
    """Local condition plus a shared file token; the database remains authoritative."""
    def __init__(self, path):
        self.path = path
        self._condition, self._generation = threading.Condition(), 0

    def token(self):
        with self._condition:
            generation = self._generation
        try:
            value = self.path.stat()
            return generation, value.st_mtime_ns
        except OSError:
            return generation, 0

    def wake(self, persist=True):
        if persist:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.touch(mode=0o600)
            except OSError:
                # Bounded fallback scans also cover external SQL writers and
                # unavailable signal files. Never fail an already committed write.
                log.debug('Background signal file unavailable', exc_info=True)
        with self._condition:
            self._generation += 1
            self._condition.notify_all()

    def wait(self, observed, timeout, stop):
        deadline = time.monotonic() + max(0, timeout)
        while not stop.is_set() and self.token() == observed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self._condition:
                # Other processes touch the same token without opening SQLite.
                self._condition.wait(min(remaining, 1))
        return not stop.is_set()


class BackgroundRuntime:
    def __init__(self, db):
        self.db = db
        self.directory = db.path.parent / '.runtime'
        self._lock, self._heavy = threading.Lock(), threading.Lock()
        self._export_lock = threading.Lock()
        self._stats, self._signals = {}, {}
        self._slow = deque(maxlen=100)
        self._stop, self._thread = threading.Event(), None
        self.started_at = time.time()
        self.role = 'main'

    def watch(self, name, tables):
        if name not in self._signals:
            signal = WakeSignal(self.directory / ('wake-' + name))
            self._signals[name] = signal
            self.db.subscribe(tables, signal.wake)
        return self._signals[name]

    def count(self, name, counter, amount=1):
        with self._lock:
            value = self._stats.setdefault(name, {})
            value[counter] = value.get(counter, 0) + amount

    def wait(self, name, signal, observed, timeout, stop):
        started = time.monotonic()
        changed = signal.wait(observed, timeout, stop)
        self.count(name, 'waits')
        self.count(name, 'wait_seconds', time.monotonic() - started)
        self.count(name, 'wakeups' if changed else 'stops' if stop.is_set() else 'timeouts')
        return changed

    def queue_delay(self, name, created_at):
        """Age at claim, including retry/restart time; never includes task contents."""
        try:
            stamp = datetime.fromisoformat(created_at)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            elapsed = max(0, time.time() - stamp.timestamp())
        except (ValueError, TypeError, OverflowError):
            return
        with self._lock:
            value = self._stats.setdefault(name, {})
            value['claims'] = value.get('claims', 0) + 1
            value['queue_age_seconds'] = value.get('queue_age_seconds', 0) + elapsed
            value['max_queue_age_seconds'] = max(value.get('max_queue_age_seconds', 0), elapsed)

    @contextmanager
    def measure(self, name, case_id=None):
        wall, cpu = time.perf_counter(), time.thread_time()
        connections = self.db.connection_count()
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            elapsed, used = time.perf_counter() - wall, time.thread_time() - cpu
            opened = self.db.connection_count() - connections
            with self._lock:
                value = self._stats.setdefault(name, {})
                for key, amount in (('runs', 1), ('errors', int(failed)), ('wall_seconds', elapsed),
                                    ('cpu_seconds', used), ('connections', opened)):
                    value[key] = value.get(key, 0) + amount
                value['max_wall_seconds'] = max(value.get('max_wall_seconds', 0), elapsed)
                value['last_at'] = time.time()
                if elapsed >= 1 or used >= .1:
                    self._slow.append({'task': name, 'case_id': case_id, 'at': value['last_at'],
                                       'wall_seconds': elapsed, 'cpu_seconds': used,
                                       'connections': opened, 'failed': failed})

    @contextmanager
    def slot(self, name):
        """Only background callers opt in; user actions never wait on this lock."""
        acquired = self._heavy.acquire(blocking=False)
        if not acquired:
            self.count(name, 'deferred')
        try:
            yield acquired
        finally:
            if acquired:
                self._heavy.release()

    def snapshot(self):
        with self._lock:
            return {'pid': os.getpid(), 'role': self.role, 'started_at': self.started_at, 'observed_at': time.time(),
                    'tasks': {k: dict(v) for k, v in self._stats.items()}, 'slow_tasks': list(self._slow)}

    def diagnostics(self):
        result = self.snapshot()
        processes = [{**result, 'status': 'live'}]
        for path in sorted(self.directory.glob('background-metrics-feishu-*.json'))[:32]:
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                child = json.loads(path.read_text(encoding='utf8'))
                age = result['observed_at'] - child['observed_at']
                if not isinstance(child.get('tasks'), dict) or not str(child.get('role', '')).startswith('feishu-'):
                    continue
                processes.append({**child, 'status': 'fresh' if 0 <= age <= 90 else 'stale'})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        result['processes'] = processes
        return result

    def flush(self):
        with self._export_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            suffix = '' if self.role == 'main' else '-' + self.role
            target = self.directory / ('background-metrics' + suffix + '.json')
            pending = self.directory / ('background-metrics-' + str(os.getpid()) + '.tmp')
            pending.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding='utf8')
            pending.chmod(0o600)
            pending.replace(target)

    def start(self, role='main'):
        if self._thread:
            return
        if not re.fullmatch(r'main|feishu-[a-zA-Z0-9_-]{1,100}', role):
            raise ValueError('Invalid diagnostic process role')
        self.role = role
        self._stop.clear()
        def export():
            while not self._stop.wait(30):
                try:
                    self.flush()
                except OSError:
                    log.exception('Background diagnostic export failed')
        self._thread = threading.Thread(target=export, name='idi-runtime-metrics', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
            self.flush()


def background_work(name, case_arg=None):
    """Reuse idle connections only for this call, never share an active transaction."""
    def decorate(function):
        @wraps(function)
        def run(self, *args, **kwargs):
            case_id = args[case_arg] if case_arg is not None and len(args) > case_arg else None
            if isinstance(case_id, dict):
                case_id = case_id.get('case_id')
            with self.db.runtime.measure(name, case_id), self.db.connection_scope():
                return function(self, *args, **kwargs)
        return run
    return decorate
