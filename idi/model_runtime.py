"""Shared, durable model circuit state. Never retries a tool or business action."""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .db import dump
from .domain import DomainError


def config_key(config):
    # No secret or request content is stored in health records.
    keys = ('base_url', 'model', 'key_env', 'credential_updated_at')
    return hashlib.sha256(dump({k: config.get(k) for k in keys}).encode()).hexdigest()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


class ModelUnavailable(DomainError):
    def __init__(self, message, *, retryable=False, retry_at=0, attempted=False):
        super().__init__(message, 503)
        self.retryable, self.retry_at, self.attempted = retryable, retry_at, attempted


class ModelRuntime:
    def __init__(self, db):
        self.db = db
        self.clock = time.time
        self._condition = threading.Condition(threading.RLock())
        self._active = self._background_active = 0
        self._foreground_waiting = 0
        self._local = threading.local()

    def config(self):
        return self.db.setting('recognition', {})

    @contextmanager
    def background(self, on_attempt=None):
        before = (getattr(self._local, 'background', False), getattr(self._local, 'on_attempt', None))
        self._local.background, self._local.on_attempt = True, on_attempt
        try:
            yield
        finally:
            self._local.background, self._local.on_attempt = before

    def status(self):
        config, current = self.config(), self.clock()
        row = self.db.one('SELECT * FROM model_health WHERE config_key=?', (config_key(config),)) or {}
        state = row.get('state', 'unverified')
        configured = bool(config.get('base_url') and config.get('model'))
        if not configured:
            state = 'unconfigured'
        elif not config.get('vision_enabled'):
            state = 'disabled'
        elif state == 'paused' and row.get('retry_at', 0) <= current:
            state = 'probing' if row.get('probe_until', 0) > current else 'recovering'
        return {'state': state, 'configured': configured, 'enabled': bool(config.get('vision_enabled')),
                'can_attempt': state not in ('unconfigured', 'disabled', 'configuration_error', 'paused', 'probing'),
                'retry_at': iso(row.get('retry_at')) if state in ('paused', 'recovering', 'probing') else None,
                'error': row.get('error', ''), 'failures': row.get('failures', 0), 'calls': row.get('calls', 0),
                'last_attempt': iso(row.get('last_attempt')), 'last_transport_ok': iso(row.get('last_transport_ok')),
                'last_response_ok': iso(row.get('last_response_ok')), 'capabilities': json.loads(row.get('capabilities', '{}')),
                'active': self._active, 'background_active': self._background_active, 'max_concurrency': 2}

    def allow_retry(self):
        """Explicit operator action; does not enable a disabled provider or replay work."""
        config = self.config()
        with self.db.tx() as con:
            row = con.execute('SELECT server_retry_at FROM model_health WHERE config_key=?', (config_key(config),)).fetchone()
            if row and row['server_retry_at'] > self.clock():
                raise ModelUnavailable('模型服务仍要求等待，请在恢复时间后重新尝试', retryable=True, retry_at=row['server_retry_at'])
            con.execute("UPDATE model_health SET state='unverified',retry_at=0,probe_until=0,failures=0,failure_window=0,error='',epoch=epoch+1 WHERE config_key=?", (config_key(config),))
        return self.status()

    def _claim(self, config, timeout):
        current = self.clock()
        if not config.get('vision_enabled') or not config.get('base_url') or not config.get('model'):
            raise ModelUnavailable('外部 AI 未配置或已停用；基础资料处理继续运行')
        if config_key(self.config()) != config_key(config) or not self.config().get('vision_enabled'):
            raise ModelUnavailable('模型配置已变化，请重新发起当前任务')
        with self.db.tx() as con:
            key = config_key(config)
            con.execute('INSERT OR IGNORE INTO model_health(config_key) VALUES(?)', (key,))
            row = dict(con.execute('SELECT * FROM model_health WHERE config_key=?', (key,)).fetchone())
            if row['state'] == 'configuration_error':
                raise ModelUnavailable(row['error'])
            if row['state'] == 'paused':
                if max(row['retry_at'], row['probe_until']) > current:
                    raise ModelUnavailable('AI 暂缓，基础资料处理继续；请等待恢复时间', retryable=True,
                                           retry_at=max(row['retry_at'], row['probe_until']))
                if not getattr(self._local, 'background', False):
                    raise ModelUnavailable('AI 等待只读资料分析试运行；可在运行状态中手动允许新调用', retryable=True, retry_at=current + 30)
                con.execute('UPDATE model_health SET probe_until=? WHERE config_key=?', (current + timeout + 5, key))
            con.execute('UPDATE model_health SET calls=calls+1,last_attempt=? WHERE config_key=?', (current, key))
        return key, row['epoch']

    @staticmethod
    def _retry_after(response, current):
        raw = response.headers.get('Retry-After', '') if response is not None else ''
        try:
            value = float(raw)
            if math.isfinite(value) and value >= 0:
                return current + value
        except (ValueError, TypeError):
            try:
                value = parsedate_to_datetime(raw).timestamp()
                if math.isfinite(value):
                    return max(current, value)
            except (ValueError, TypeError, OverflowError):
                pass
        return current + 60

    def _failure(self, key, error, capability):
        current = self.clock()
        code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        transient = isinstance(error, (httpx.TimeoutException, httpx.NetworkError)) or (code is not None and (code == 429 or code >= 500))
        configuration = code in (400, 401, 403, 404)
        reason = (f'模型接口 HTTP {code}，请检查配置或权限' if configuration else
                  '模型请求受限，等待服务恢复' if code == 429 else
                  '模型连接暂不可用，资料已保留' if transient else
                  '模型响应无效或未完成，需核对当前任务')
        with self.db.tx() as con:
            row = dict(con.execute('SELECT * FROM model_health WHERE config_key=?', (key,)).fetchone())
            failures = row['failures'] + 1 if row['failure_window'] and current - row['failure_window'] <= 300 else 1
            start = row['failure_window'] if row['failure_window'] and current - row['failure_window'] <= 300 else current
            retry_at, cooldown, state = 0, row['cooldown'], 'degraded'
            server_retry_at = max(row['server_retry_at'], self._retry_after(error.response, current) if code == 429 else 0)
            if configuration:
                state = 'configuration_error'
            elif transient and (code == 429 or failures >= 3 or row['state'] == 'paused'):
                cooldown = min(300, 60 if not cooldown else cooldown * 2)
                retry_at = max(current + cooldown, server_retry_at)
                state = 'paused'
            caps = json.loads(row['capabilities'])
            caps[capability] = {'state': 'failed', 'at': iso(current), 'error': reason}
            if row['state'] == 'configuration_error' or row['state'] == 'paused' and not transient and not configuration:
                state, retry_at, reason = row['state'], row['retry_at'], row['error']
            con.execute('UPDATE model_health SET state=?,failures=?,failure_window=?,retry_at=?,cooldown=?,probe_until=0,error=?,capabilities=?,server_retry_at=?,epoch=epoch+1 WHERE config_key=?',
                        (state, failures if transient else 0, start if transient else 0, retry_at, cooldown, reason, dump(caps), server_retry_at, key))
        return ModelUnavailable(reason, retryable=transient, retry_at=retry_at or current + 30, attempted=True)

    def _success(self, key, capability, epoch):
        current = self.clock()
        with self.db.tx() as con:
            row = con.execute('SELECT capabilities,epoch FROM model_health WHERE config_key=?', (key,)).fetchone()
            caps = json.loads(row['capabilities'])
            caps[capability] = {'state': 'response_valid', 'at': iso(current)}
            con.execute('UPDATE model_health SET last_response_ok=?,capabilities=? WHERE config_key=?', (current, dump(caps), key))
            if row['epoch'] == epoch:
                con.execute("UPDATE model_health SET state='healthy',failures=0,failure_window=0,retry_at=0,server_retry_at=0,cooldown=0,probe_until=0,error='' WHERE config_key=?", (key,))

    def request(self, config, key, payload, timeout, *, capability='tools', parse=None):
        background = getattr(self._local, 'background', False)
        deadline = time.monotonic() + (0 if background else min(5, max(0, timeout)))
        with self._condition:
            self._foreground_waiting += int(not background)
            try:
                while self._active >= 2 or background and (self._background_active >= 1 or self._foreground_waiting):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise ModelUnavailable('AI 正在处理其他资料，稍后继续', retryable=True, retry_at=self.clock() + 5)
                    self._condition.wait(min(left, .2))
                self._active += 1
                self._background_active += int(background)
            finally:
                self._foreground_waiting -= int(not background)
        try:
            fingerprint, epoch = self._claim(config, timeout)
            on_attempt = getattr(self._local, 'on_attempt', None)
            if on_attempt:
                try:
                    on_attempt()
                except Exception:
                    with self.db.tx() as con:
                        con.execute('UPDATE model_health SET calls=MAX(0,calls-1),probe_until=CASE WHEN epoch=? THEN 0 ELSE probe_until END WHERE config_key=?', (epoch, fingerprint))
                    raise
            try:
                with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                    response = client.post(config['base_url'].rstrip('/') + '/chat/completions',
                                           headers={'Authorization': 'Bearer ' + key} if key else {}, json=payload)
                    response.raise_for_status()
                    with self.db.tx() as con:
                        con.execute('UPDATE model_health SET last_transport_ok=? WHERE config_key=?', (self.clock(), fingerprint))
                    value = response.json()
                choice = value['choices'][0]
                if not isinstance(choice.get('message'), dict) or choice.get('finish_reason') in ('length', 'content_filter'):
                    raise ValueError('incomplete model response')
                if not choice['message'].get('tool_calls') and not choice['message'].get('content'):
                    raise ValueError('empty model response')
                calls = choice['message'].get('tool_calls')
                if calls:
                    if not isinstance(calls, list):
                        raise ValueError('invalid tool response')
                    for call in calls:
                        if not isinstance(call, dict) or not isinstance(call.get('id'), str):
                            raise ValueError('invalid tool call')
                        function = call.get('function', {})
                        if not isinstance(function.get('name'), str) or not isinstance(function.get('arguments'), str) or not isinstance(json.loads(function['arguments']), dict):
                            raise ValueError('invalid tool arguments')
                result = parse(value) if parse else value
            except Exception as exc:
                if isinstance(exc, ModelUnavailable):
                    raise
                raise self._failure(fingerprint, exc, capability) from None
            if config_key(self.config()) != fingerprint or not self.config().get('vision_enabled'):
                raise ModelUnavailable('模型配置在处理期间变化，未采用旧结果')
            self._success(fingerprint, capability, epoch)
            return result
        finally:
            with self._condition:
                self._active -= 1
                self._background_active -= int(background)
                self._condition.notify_all()
