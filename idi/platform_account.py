"""Encrypted website account and bounded reconnects; never replay business writes."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .connectors.secret_store import protect
from .db import uid
from .domain import DomainError
from .storage import Storage


class PlatformAccountStore:
    def __init__(self, data_dir):
        self.path = data_dir / 'secrets' / 'platform-account.bin'

    def load(self):
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(protect(self.path.read_bytes(), decrypt=True))
            if (not isinstance(value, dict) or not isinstance(value.get('username'), str)
                    or not value['username'].strip() or not isinstance(value.get('password'), str)
                    or not value['password'] or type(value.get('auto_connect')) is not bool
                    or not isinstance(value.get('revision'), str)):
                raise ValueError()
            return value
        except (OSError, ValueError, DomainError) as error:
            raise DomainError('系统账号无法解密，请在保存账号时的 Windows 用户下重新配置。') from error

    def save(self, value):
        # Encrypt before creating even a temporary file. The archive backup excludes secrets/.
        try:
            encrypted = protect(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            Storage._atomic(self.path, encrypted)
        except OSError as error:
            raise DomainError('系统账号未能保存，请检查本机凭据目录。', 503) from error

    def delete(self):
        self.path.unlink(missing_ok=True)


class PlatformAccount:
    def __init__(self, platform, data_dir):
        self.platform, self.db = platform, platform.db
        self.store = PlatformAccountStore(data_dir)
        self.clock = lambda: datetime.now(timezone.utc)
        self._gate = threading.Lock()
        self._meta = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = self._attempt_thread = None
        self._load_error = ''
        try:
            self._config = self.store.load()
        except DomainError as error:
            self._config, self._load_error = {}, str(error)
        previous = self.db.setting('platform_account_state', {})
        self._state = previous if previous.get('revision') == self._config.get('revision') else {}
        self._connecting = False

    @property
    def client(self):
        return self.platform.client

    def _persist(self):
        self.db.set_setting('platform_account_state', {**self._state, 'revision': self._config.get('revision')})

    def status(self):
        session = self.client.status()
        with self._meta:
            configured = bool(self._config)
            enabled = bool(self._config.get('auto_connect'))
            if self._load_error:
                state = 'needs_attention'
            elif self._connecting:
                state = 'connecting'
            elif session['connected'] and session['account'] == self._config.get('username'):
                state = 'connected'
            elif not configured:
                state = 'unconfigured'
            elif not enabled:
                state = 'disabled'
            elif self._state.get('blocked'):
                state = 'needs_attention'
            elif self._state.get('next_retry_at'):
                state = 'retry_wait'
            else:
                state = 'pending'
            return {'configured': configured, 'username': self._config.get('username', ''),
                    'password_stored': configured, 'auto_connect': enabled, 'state': state,
                    'last_attempt_at': self._state.get('last_attempt_at'),
                    'last_success_at': self._state.get('last_success_at'),
                    'next_retry_at': self._state.get('next_retry_at') if enabled else None,
                    'error': self._load_error or self._state.get('error', ''), 'session': session}

    @contextmanager
    def _editing(self):
        if not self._gate.acquire(blocking=False):
            raise DomainError('正在连接官网，请等待本次连接结束后再修改设置。', 409)
        try:
            yield
        finally:
            self._gate.release()

    def configure(self, data):
        if not isinstance(data, dict) or set(data) != {'username', 'password', 'auto_connect'}:
            raise DomainError('请提供系统账号、密码和自动连接开关。')
        username, password, enabled = data['username'], data['password'], data['auto_connect']
        if (not isinstance(username, str) or not username.strip() or len(username) > 160
                or not isinstance(password, str) or len(password) > 512 or type(enabled) is not bool):
            raise DomainError('系统账号、密码或自动连接设置无效。')
        username = username.strip()
        with self._editing(), self._meta:
            if not password:
                if username != self._config.get('username') or not self._config.get('password'):
                    raise DomainError('首次设置或更换系统账号时，请填写对应的官网密码。')
                password = self._config['password']
            changed = (username, password) != (self._config.get('username'), self._config.get('password'))
            value = dict(username=username, password=password, auto_connect=enabled, revision=uid())
            self.store.save(value)
            self._config, self._load_error, self._state = value, '', {}
            self._persist()
            if changed or self.client.status()['account'] not in ('', username):
                self.client.logout()
        if enabled:
            self.request_connect(force=False)
        return self.status()

    def forget(self):
        with self._editing(), self._meta:
            self.store.delete()
            self._config, self._state, self._load_error = {}, {}, ''
            self._persist()
            self.client.logout()
        return self.status()

    def disconnect(self):
        with self._editing(), self._meta:
            if self._config:
                value = {**self._config, 'auto_connect': False}
                self.store.save(value)
                self._config = value
            self._state = {}
            self._persist()
            self.client.logout()
        return self.client.status()

    def temporary_login(self, username, password):
        # An explicit temporary account takes priority over the saved account until re-enabled.
        with self._editing():
            with self._meta:
                if self._config and self._config['auto_connect']:
                    value = {**self._config, 'auto_connect': False}
                    self.store.save(value)
                    self._config = value
                self._state = {}
                self._persist()
                self._connecting = True
            try:
                return self.client.login(username, password)
            finally:
                with self._meta:
                    self._connecting = False

    def _begin(self, force):
        if not self._gate.acquire(blocking=False):
            return False
        keep = False
        try:
            with self._meta:
                if self._stop.is_set():
                    return False
                if not self._config:
                    if force:
                        raise DomainError('请先保存系统账号。')
                    return False
                session = self.client.status()
                if not force:
                    if not self._config['auto_connect'] or self._state.get('blocked'):
                        return False
                    if session['connected']:
                        return False
                    retry = self._state.get('next_retry_at')
                    if retry and datetime.fromisoformat(retry) > self.clock():
                        return False
                self._connecting = True
                self._state.update(last_attempt_at=self.clock().isoformat(), next_retry_at=None, error='')
                self._persist()
                keep = True
                return True
        finally:
            if not keep:
                self._gate.release()

    def _attempt(self):
        try:
            self.client.login(self._config['username'], self._config['password'])
            with self._meta:
                self._state.update(last_success_at=self.clock().isoformat(), failures=0,
                                   blocked=False, error='', next_retry_at=None)
                self._persist()
        except Exception as error:
            with self._meta:
                failures = self._state.get('failures', 0) + 1
                blocked = isinstance(error, DomainError) and (error.status in (400, 401, 403) or getattr(error, 'retryable', True) is False)
                # Only fixed messages are persisted: no upstream body, password, or token.
                detail = ('官网登录未完成，请核对账号密码、官网验证或本机浏览器后手动重试。' if blocked
                          else '官网暂时无法连接，将按重试时间再次尝试。' if self._config['auto_connect']
                          else '官网暂时无法连接，请稍后手动重试。')
                retry = (self.clock() + timedelta(seconds=min(1800, 60 * 2 ** min(failures - 1, 5)))).isoformat()
                self._state.update(failures=failures, blocked=blocked, error=detail,
                                   next_retry_at=None if blocked else retry)
                self._persist()
        finally:
            with self._meta:
                self._connecting = False
            self._gate.release()

    def request_connect(self, force=True):
        if self._begin(force):
            self._attempt_thread = threading.Thread(target=self._attempt, name='idi-platform-login', daemon=True)
            self._attempt_thread.start()
        return self.status()

    def tick(self):
        if self._begin(False):
            self._attempt()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='idi-platform-account', daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(5)
            self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()
        for thread in (self._thread, self._attempt_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)
