"""Separate company reads and project writes; no credential swapping or fallback."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps

from fastapi import APIRouter, Body

from .db import dump, now, uid
from .domain import DomainError
from .identity import current, require_admin, require_project
from .platform_account import PlatformAccount
from .platform_client import PlatformClient, PlatformWriteError


def case_context(platform, case_id, operation=None, record=None):
    accounts = getattr(platform, 'project_accounts', None)
    return accounts.bind(case_id, operation, record) if accounts else nullcontext({})


def scoped_case(operation=None):
    def decorate(fn):
        @wraps(fn)
        def run(self, case_id, *args, **kwargs):
            with case_context(self.platform, case_id, operation) as result:
                value = fn(self, case_id, *args, **kwargs)
                result['result'] = value
                return value
        return run
    return decorate


def scoped_record(table, operation=None):
    def decorate(fn):
        @wraps(fn)
        def run(self, record_id, *args, **kwargs):
            row = self.db.one(f'SELECT * FROM {table} WHERE id=?', (record_id,))
            if not row:
                raise DomainError('操作记录不存在', 404)
            with case_context(self.platform, row['case_id'], operation, row) as result:
                value = fn(self, record_id, *args, **kwargs)
                result['result'] = value
                return value
        return run
    return decorate


class ProjectClient:
    """Reads use the bound project during write preflight/readback, company otherwise."""
    def __init__(self, accounts):
        self.accounts = accounts

    def target(self):
        bound = self.accounts.context.get()
        return bound['account'].client if bound else self.accounts.company.client

    def __getattr__(self, name):
        return getattr(self.target(), name)

    @property
    def write_enabled(self):
        bound = self.accounts.context.get()
        return bool(bound and self.accounts.write_enabled and bound['account']._config and
                    self.accounts.enabled(bound['project_id']))

    def _write(self, method, *args, **kwargs):
        bound = self.accounts.context.get()
        if not bound or not bound.get('operation'):
            raise DomainError('官网写入缺少项目及操作记录，已拒绝使用公司账号', 403)
        require_project(bound['project_id'], manage=True)
        person = current()
        access = getattr(self.accounts, 'access', None)
        if person and access and not access.principal(person.id).allows(bound['project_id'], manage=True):
            raise DomainError('人员的项目管理权限已改变，本次未继续写入', 403)
        if not self.write_enabled:
            raise DomainError('本项目官网账号尚未配置或写入已暂停，照片继续保存在 IDI', 403)
        actual = self.accounts.binding(bound['project_id'])
        if actual != bound['binding']:
            raise DomainError('项目官网账号已改变，请重新核对', 409)
        self.accounts.distinct(actual['username'], bound['project_id'])
        if bound['account'].client.status()['account'] != actual['username']:
            raise DomainError('本项目官网连接与配置账号不一致，请重新连接', 409)
        bound['attempted'] = True
        try:
            value = getattr(bound['account'].client, method)(*args, **kwargs)
            bound['remote_written'] = True
            return value
        except Exception as error:
            if not isinstance(error, PlatformWriteError) or not error.rejected:
                bound['uncertain'] = True
            raise

    def upload(self, *args, **kwargs):
        return self._write('upload', *args, **kwargs)

    def submit(self, *args, **kwargs):
        return self._write('submit', *args, **kwargs)

    def save_draft_only(self, *args, **kwargs):
        return self._write('save_draft_only', *args, **kwargs)

    def close(self):
        self.accounts.stop()
        self.accounts.company.client.close()
        for account in self.accounts.accounts.values():
            account.client.close()


class ProjectAccounts:
    def __init__(self, platform, data_dir, *, write_enabled=False, client_factory=None):
        self.platform, self.db, self.data_dir = platform, platform.db, data_dir
        self.write_enabled = write_enabled
        self.client_factory = client_factory or (lambda: PlatformClient(write_enabled=write_enabled))
        self.accounts = {}
        self.lock = threading.RLock()
        self.login_slots = threading.BoundedSemaphore(2)
        self.context = ContextVar('idi_project_website', default=None)
        self.running = False
        company_client = platform.client
        company_client.write_enabled = False
        company_client._permanently_read_only = True
        self.company = PlatformAccount(platform, data_dir, client=company_client, validate=self.distinct,
                                       login_gate=self.login_slots)
        for row in self.db.all('SELECT project_id FROM project_website_accounts'):
            self.account(row['project_id'])
        self.router = ProjectClient(self)
        with self.db.tx() as con:
            con.execute("UPDATE website_operations SET state='uncertain',error='执行期间服务中断，请回读原操作，未自动重发',finished_at=? WHERE state='running'", (now(),))

    def account(self, project_id):
        if not self.db.one('SELECT id FROM projects WHERE id=?', (project_id,)):
            raise DomainError('项目不存在', 404)
        with self.lock:
            if project_id not in self.accounts:
                self.accounts[project_id] = PlatformAccount(self.platform, self.data_dir,
                    client=self.client_factory(), key=project_id, validate=lambda username: self.distinct(username, project_id),
                    login_gate=self.login_slots)
            return self.accounts[project_id]

    def distinct(self, username, project_id=None):
        if not isinstance(username, str) or not username.strip():
            raise DomainError('请填写官网账号')
        username = username.strip().casefold()
        if project_id:
            names = {self.company._config.get('username', '').casefold(),
                     self.company.client.status().get('account', '').casefold()}
            if username in names:
                raise DomainError('项目写入账号不能使用公司读取账号', 409)
        elif any(a._config.get('username', '').casefold() == username for a in self.accounts.values()):
            raise DomainError('公司读取账号不能与已配置的项目写入账号相同', 409)

    def enabled(self, project_id):
        row = self.db.one('SELECT enabled FROM project_website_accounts WHERE project_id=?', (project_id,))
        return bool(row and row['enabled'])

    def binding(self, project_id):
        config = self.account(project_id)._config
        return {'project_id': project_id, 'username': config.get('username', ''),
                'revision': config.get('revision', ''), 'enabled': self.enabled(project_id)}

    def status(self, project_id):
        require_project(project_id, manage=True)
        account = self.account(project_id)
        return {**account.status(), 'project_id': project_id, 'enabled': self.enabled(project_id),
                'write_enabled': bool(self.write_enabled and self.enabled(project_id) and account._config)}

    def configure(self, project_id, data):
        require_admin()
        with self.platform.material_lock:
            account = self.account(project_id)
            enabled = data.get('enabled', True)
            if not isinstance(enabled, bool):
                raise DomainError('项目写入开关无效')
            account.configure({k: data.get(k) for k in ('username', 'password', 'auto_connect')})
            with self.db.tx() as con:
                con.execute('INSERT INTO project_website_accounts VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at',
                            (project_id, int(enabled), now()))
            # Activating the new project-account workflow enables incremental
            # saves once. Subsequent global pauses remain authoritative.
            if not self.db.setting('project_account_uploads_initialized', False):
                self.db.set_setting('automatic_upload_policy', {
                    **self.db.setting('automatic_upload_policy', {}), 'enabled': True, 'activated_at': now()})
                self.db.set_setting('workflow_policy', {
                    **self.db.setting('workflow_policy', {}), 'rules_enabled': True, 'auto_drafts': True})
                self.db.set_setting('project_account_uploads_initialized', True)
            if self.running:
                account.start()
        return self.status(project_id)

    def edit(self, project_id, action):
        require_admin()
        with self.platform.material_lock:
            account = self.account(project_id)
            {'connect': account.request_connect, 'disconnect': account.disconnect, 'forget': account.forget}[action]()
        return self.status(project_id)

    @contextmanager
    def bind(self, case_id, operation=None, record=None):
        case = self.platform.domain.case(case_id)
        require_project(case['project_id'], manage=bool(operation))
        previous = self.context.get()
        if previous and previous['case_id'] == case_id and (previous.get('operation') or not operation):
            yield previous
            return
        # Reuse the established business lock order. Account edits and writes
        # cannot race; distinct projects still retain distinct clients and tokens.
        with self.platform.material_lock:
            account = self.account(case['project_id'])
            binding = self.binding(case['project_id'])
            if record and operation:
                stored = json.loads(record.get('snapshot') or '{}')
                if stored.get('write_binding') != binding:
                    raise DomainError('此操作的项目官网账号已变更，请重新核对；旧操作不会自动重发', 409)
            bound = {'case_id': case_id, 'project_id': case['project_id'], 'account': account,
                     'binding': binding, 'operation': operation, 'attempted': False, 'remote_written': False}
            operation_id = None
            if operation:
                photo_ids = []
                if record:
                    stored = json.loads(record.get('snapshot') or '{}')
                    photo_ids = list({p['id'] for f in stored.get('file_fields', []) for p in f['selected']})
                    remote_ids = {str(p['id']) for f in stored.get('file_fields', [])
                                  for p in f.get('existing', []) if p.get('id')}
                    # Formal submission commonly follows an incremental save,
                    # so its selected list is empty. Carry forward the people
                    # behind files that are still present in this exact stage.
                    if remote_ids and record.get('stage'):
                        for prior in self.db.all(
                                "SELECT snapshot,result FROM platform_review_batches WHERE case_id=? AND stage=? "
                                "AND state IN ('save_verified','verified')",
                                (case_id, record['stage'])):
                            snapshot, receipt = json.loads(prior['snapshot']), json.loads(prior['result'])
                            if snapshot.get('link', {}).get('report_receiving_id') == stored.get('link', {}).get('report_receiving_id'):
                                photo_ids.extend(pid for pid, item in receipt.get('uploaded', {}).items()
                                                 if str(item.get('id')) in remote_ids)
                    photo_ids = sorted(set(photo_ids))
                person = current()
                marks = ','.join('?' for _ in photo_ids) or 'NULL'
                sources = self.db.all(
                    'SELECT DISTINCT u.id,u.display_name FROM idi_users u WHERE u.id IN ('
                    f'SELECT uploaded_by FROM photos WHERE id IN ({marks}) UNION '
                    f"SELECT json_extract(metadata,'$.uploaded_by') FROM photo_sources WHERE photo_id IN ({marks}))",
                    [*photo_ids, *photo_ids])
                operation_id = uid()
                with self.db.tx() as con:
                    con.execute('INSERT INTO website_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (operation_id, case['project_id'], case_id, operation, person.id if person else None,
                         person.display_name if person else '系统自动执行', binding['username'], binding['revision'],
                         dump(photo_ids), dump(sources), 'running', '', now(), ''))
            token = self.context.set(bound)
            error_text = ''
            try:
                yield bound
            except Exception as error:
                error_text = str(error)[:500] if isinstance(error, DomainError) else '操作未完成，请回读官网核实'
                raise
            finally:
                self.context.reset(token)
                if operation_id:
                    result = bound.get('result') or {}
                    state = result.get('state') if isinstance(result, dict) else None
                    state = state or ('uncertain' if bound.get('uncertain') else 'failed' if error_text else
                                      'written' if bound['remote_written'] else 'no_write')
                    with self.db.tx() as con:
                        action = (result.get('result') or {}).get('action') if isinstance(result, dict) else None
                        label = ('formal_submit' if action == 'submit' else 'confirmed_save') if operation == 'review_confirmation' and action else operation
                        con.execute('UPDATE website_operations SET operation=?,state=?,error=?,finished_at=? WHERE id=?',
                                    (label, state, error_text, now(), operation_id))

    def start(self):
        self.running = True
        self.company.start()
        for account in list(self.accounts.values()):
            account.start()

    def stop(self):
        self.running = False
        self.company.stop()
        for account in list(self.accounts.values()):
            account.client._closing.set()
            account.stop()


def project_account_routes(accounts):
    router = APIRouter(prefix='/api/access/project-accounts')

    @router.get('/{project_id}')
    def status(project_id: str):
        return accounts.status(project_id)

    @router.put('/{project_id}')
    def configure(project_id: str, data: dict = Body(...)):
        return accounts.configure(project_id, data)

    @router.post('/{project_id}/connect')
    def connect(project_id: str):
        return accounts.edit(project_id, 'connect')

    @router.post('/{project_id}/disconnect')
    def disconnect(project_id: str):
        return accounts.edit(project_id, 'disconnect')

    @router.delete('/{project_id}')
    def forget(project_id: str):
        return accounts.edit(project_id, 'forget')

    return router
