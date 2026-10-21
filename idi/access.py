"""IDI identities. Every business HTTP request has an authenticated principal."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Request, Response

from .db import audit, dump, now, uid
from .domain import DomainError
from .identity import Principal, current, require_admin, require_project

COOKIE = 'idi_session'
SESSION_HOURS = 12


def password_hash(password):
    if not isinstance(password, str) or not 10 <= len(password) <= 128:
        raise DomainError('密码长度须为10至128个字符')
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return 'scrypt$' + salt.hex() + '$' + key.hex()


def password_matches(password, encoded):
    if not isinstance(password, str) or len(password) > 128:
        return False
    try:
        algorithm, salt, expected = encoded.split('$')
        if algorithm != 'scrypt':
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Access:
    def __init__(self, db):
        self.db = db
        # Keep missing-user and incorrect-password verification comparable.
        self.dummy_password = password_hash(secrets.token_urlsafe(24))

    def principal(self, user_id):
        row = self.db.one('SELECT * FROM idi_users WHERE id=? AND enabled=1', (user_id,))
        if not row:
            raise DomainError('人员账号已停用或不存在，请重新登录', 401)
        projects = {m['project_id']: m['role'] for m in self.db.all(
            'SELECT project_id,role FROM project_members WHERE user_id=?', (user_id,))}
        return Principal(row['id'], row['username'], row['display_name'], row['system_role'], projects)

    def session(self, request):
        token = request.cookies.get(COOKIE, '')
        if not token or len(token) > 200:
            raise DomainError('请先登录 IDI', 401)
        row = self.db.one('SELECT user_id FROM idi_sessions WHERE token_hash=? AND expires_at>?',
                          (token_hash(token), now()))
        if not row:
            raise DomainError('登录已失效，请重新登录', 401)
        return self.principal(row['user_id'])

    def issue_session(self, user_id, request, response):
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(timespec='milliseconds')
        with self.db.tx() as con:
            con.execute('DELETE FROM idi_sessions WHERE expires_at<?', (now(),))
            con.execute('INSERT INTO idi_sessions VALUES(?,?,?,?)', (token_hash(token), user_id, now(), expires))
        response.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict', path='/')
        return self.principal(user_id).public()

    def login(self, data, request, response):
        username = str(data.get('username', '')).strip()[:80]
        # Bound persisted keys even for deliberately oversized login values.
        key = token_hash((request.client.host if request.client else '') + ':' + username.casefold())
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec='milliseconds')
        with self.db.tx() as con:
            con.execute('DELETE FROM idi_login_attempts WHERE window_started_at<?', (cutoff,))
            attempt = con.execute('SELECT * FROM idi_login_attempts WHERE key=?', (key,)).fetchone()
            if attempt and attempt['failures'] >= 8:
                raise DomainError('登录尝试过多，请15分钟后重试', 429)
            con.execute('INSERT INTO idi_login_attempts VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET failures=failures+1',
                        (key, now()))
        user = self.db.one('SELECT * FROM idi_users WHERE username=? COLLATE NOCASE', (username,))
        valid = password_matches(data.get('password'), user['password_hash'] if user else self.dummy_password)
        if not valid or not user or not user['enabled']:
            raise DomainError('账号或密码错误，或账号已停用', 401)
        with self.db.tx() as con:
            con.execute('DELETE FROM idi_login_attempts WHERE key=?', (key,))
            audit(con, 'idi_user', user['id'], 'login', actor='user:' + user['id'])
        return self.issue_session(user['id'], request, response)

    def users(self):
        require_admin()
        rows = self.db.all('SELECT id,username,display_name,system_role,enabled,created_at,updated_at FROM idi_users ORDER BY created_at')
        for row in rows:
            row['enabled'] = bool(row['enabled'])
            row['projects'] = {m['project_id']: m['role'] for m in self.db.all(
                'SELECT project_id,role FROM project_members WHERE user_id=?', (row['id'],))}
        return rows

    def save_user(self, data, user_id=None, *, bootstrap=False):
        if not bootstrap:
            require_admin()
        existing = self.db.one('SELECT * FROM idi_users WHERE id=?', (user_id,)) if user_id else None
        if user_id and not existing:
            raise DomainError('人员不存在', 404)
        username = str(data.get('username', (existing or {}).get('username', ''))).strip()
        name = str(data.get('display_name', (existing or {}).get('display_name', ''))).strip()
        role = data.get('system_role', (existing or {}).get('system_role', 'user'))
        enabled = data.get('enabled', bool((existing or {}).get('enabled', 1)))
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}', username) or not name or len(name) > 80:
            raise DomainError('账号须为3至80位字母、数字或 _.@-，并填写人员姓名（最多80字）')
        if role not in ('admin', 'user') or not isinstance(enabled, bool):
            raise DomainError('账号权限格式无效')
        encoded = password_hash(data['password']) if data.get('password') else (existing or {}).get('password_hash')
        if not encoded:
            raise DomainError('请设置初始密码')
        user_id = user_id or uid()
        with self.db.tx() as con:
            if bootstrap and con.execute('SELECT 1 FROM idi_users LIMIT 1').fetchone():
                raise DomainError('管理员已经创建，请登录', 409)
            if existing and existing['system_role'] == 'admin' and existing['enabled'] and (role != 'admin' or not enabled):
                count = con.execute("SELECT COUNT(*) FROM idi_users WHERE system_role='admin' AND enabled=1").fetchone()[0]
                if count <= 1:
                    raise DomainError('必须保留至少一名启用的系统管理员', 409)
            con.execute('''INSERT INTO idi_users VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,display_name=excluded.display_name,password_hash=excluded.password_hash,
                system_role=excluded.system_role,enabled=excluded.enabled,updated_at=excluded.updated_at''',
                (user_id, username, name, encoded, role, int(enabled), (existing or {}).get('created_at', now()), now()))
            if existing and (data.get('password') or not enabled or role != existing['system_role']):
                con.execute('DELETE FROM idi_sessions WHERE user_id=?', (user_id,))
                if data.get('password') or not enabled:
                    con.execute('UPDATE camera_devices SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', (now(), user_id))
                    con.execute('DELETE FROM camera_pairings WHERE user_id=? AND used_at IS NULL', (user_id,))
            audit(con, 'idi_user', user_id, 'update' if existing else 'create',
                  after={'username': username, 'display_name': name, 'system_role': role, 'enabled': enabled})
        return {'id': user_id, 'username': username, 'display_name': name, 'system_role': role, 'enabled': enabled}

    def save_members(self, project_id, data):
        require_admin()
        if not self.db.one('SELECT id FROM projects WHERE id=?', (project_id,)):
            raise DomainError('项目不存在', 404)
        members = data.get('members')
        if not isinstance(members, list) or len(members) > 500:
            raise DomainError('项目人员列表无效')
        seen = set()
        for member in members:
            if not isinstance(member, dict) or member.get('role') not in ('manager', 'member'):
                raise DomainError('请选择项目管理员或项目成员')
            if member.get('user_id') in seen or not self.db.one('SELECT id FROM idi_users WHERE id=?', (member.get('user_id'),)):
                raise DomainError('项目人员重复或不存在')
            seen.add(member['user_id'])
        with self.db.tx() as con:
            con.execute('DELETE FROM project_members WHERE project_id=?', (project_id,))
            con.executemany('INSERT INTO project_members VALUES(?,?,?,?,?)',
                            [(project_id, m['user_id'], m['role'], now(), now()) for m in members])
            audit(con, 'project_members', project_id, 'replace', after=members)
        return {'project_id': project_id, 'members': members}

    def resource(self, table, resource_id, *, manage=False):
        # table is selected only by fixed route declarations below.
        row = self.db.one(f'SELECT project_id FROM {table} WHERE id=?', (resource_id,))
        if not row:
            raise DomainError('资料不存在', 404)
        require_project(row['project_id'], manage=manage)
        return row['project_id']

    def upload_scope(self, project_id, case_id):
        person = current()
        if case_id:
            actual = self.resource('cases', case_id)
            if project_id and project_id != actual:
                raise DomainError('上传项目与案件不一致', 409)
            project_id = actual
        if person and not person.admin and not case_id:
            raise DomainError('请先选择本项目案件再上传照片', 403)
        if project_id:
            require_project(project_id)
        return project_id

    def _body_scope(self, data, manage):
        if not isinstance(data, dict):
            raise DomainError('请求格式无效')
        for key, table in (('case_id', 'cases'), ('photo_id', 'photos')):
            if data.get(key):
                self.resource(table, data[key], manage=manage)
        if data.get('project_id'):
            require_project(data['project_id'], manage=manage)
        if 'photo_ids' in data:
            if not isinstance(data['photo_ids'], list) or len(data['photo_ids']) > 500:
                raise DomainError('照片列表无效')
            for photo_id in data['photo_ids']:
                self.resource('photos', photo_id, manage=manage)
        if isinstance(data.get('patch'), dict):
            self._body_scope(data['patch'], manage)

    async def authorize(self, request):
        """Fail closed. Adding an HTTP route does not silently grant access."""
        path, method = request.url.path.rstrip('/'), request.method
        if path in ('/api/health', '/api/auth/status', '/api/auth/login', '/api/auth/setup', '/api/camera/pair'):
            return None
        camera_device_paths = ('/api/camera/cases', '/api/camera/captures', '/api/camera/official-captures',
                               '/api/camera/sdk-config', '/api/camera/native-config', '/api/camera/native-session',
                               '/api/camera/h5-config', '/api/camera/h5-session')
        if any(path == prefix or path.startswith(prefix + '/') for prefix in camera_device_paths):
            device = request.app.state.services['mobile_camera'].authenticate(request)
            request.state.camera_device = device
            return self.principal(device['user_id'])
        if not path.startswith('/api/'):
            if path == '/camera-setup':
                person = self.session(request)
                if not person.admin:
                    raise DomainError('手机配对管理需要系统管理员权限', 403)
                return person
            return None
        return self.session(request)

    async def check_route(self, request):
        person = current()
        if person is None or person.admin:
            return
        path, method = request.url.path.rstrip('/'), request.method
        read = method in ('GET', 'HEAD')
        if path.startswith('/api/camera/') and hasattr(request.state, 'camera_device'):
            return
        if path in ('/api/auth/me', '/api/auth/logout', '/api/auth/password'):
            return
        if read and path in ('/api/system', '/api/projects', '/api/cases', '/api/photos', '/api/summary',
                             '/api/jobs', '/api/workflow/board', '/api/workflow/sync', '/api/platform/session',
                             '/api/platform/contract', '/api/templates', '/api/access/website-operations',
                             '/api/workflow/uploads'):
            return
        if path == '/api/import/upload' and method == 'POST':
            return  # multipart scope is checked before staging any files
        if path == '/api/audit' and read:
            table = {'photo': 'photos', 'case': 'cases'}.get(request.query_params.get('entity_type'))
            if not table or not request.query_params.get('entity_id'):
                raise DomainError('请指定本项目照片或案件的记录', 403)
            self.resource(table, request.query_params['entity_id'], manage=True)
            return
        if path == '/api/recognize' and method == 'POST':
            data = await request.json()
            if data.get('use_vision'):
                raise DomainError('外部模型识别须由系统管理员操作', 403)
            if not data.get('photo_ids'):
                raise DomainError('请选择本项目照片', 403)
            self._body_scope(data, True)
            return
        match = re.fullmatch(r'/api/platform/packages/([^/]+)/download', path)
        if match and read:
            row = self.db.one('SELECT case_id FROM platform_packages WHERE id=?', (match[1],))
            if not row:
                raise DomainError('资料包不存在', 404)
            self.resource('cases', row['case_id'], manage=True)
            return
        match = re.fullmatch(r'/api/jobs/([^/]+)(/retry)?', path)
        if match and (read or method == 'POST' and match[2]):
            row = self.db.one('SELECT user_id FROM jobs WHERE id=?', (match[1],))
            if not row or row['user_id'] != person.id:
                raise DomainError('没有此任务的访问权限', 403)
            return
        match = re.fullmatch(r'/api/(?:workflow/|platform/)?cases/([^/]+)(/.*)?', path)
        if match:
            readback = method == 'POST' and path == f'/api/workflow/cases/{match[1]}/refresh'
            self.resource('cases', match[1], manage=not (read or readback))
            if path.startswith('/api/platform/') and match[2] in ('/link', '/bind') and not read:
                raise DomainError('官网案件关联须由系统管理员配置', 403)
        else:
            match = re.fullmatch(r'/api/photos/([^/]+)(/original|/thumbnail|/reuse-checkin)?', path)
            if match and match[1] not in ('bulk', 'delete-preview', 'delete'):
                self.resource('photos', match[1], manage=not read)
            elif path in ('/api/photos/bulk', '/api/photos/delete-preview', '/api/photos/delete') and method == 'POST':
                pass
            elif path == '/api/cases' and method == 'POST':
                pass
            else:
                match = re.fullmatch(r'/api/(workflow/reviews|platform/attachment-removals)/([^/]+)(/.*)?', path)
                if match:
                    table = 'platform_review_batches' if match[1] == 'workflow/reviews' else 'platform_attachment_removals'
                    row = self.db.one(f'SELECT case_id FROM {table} WHERE id=?', (match[2],))
                    if not row:
                        raise DomainError('复核记录不存在', 404)
                    self.resource('cases', row['case_id'], manage=True)
                else:
                    match = re.fullmatch(r'/api/workflow/uploads/cases/([^/]+)', path)
                    if match:
                        self.resource('cases', match[1], manage=not read)
                    else:
                        match = re.fullmatch(r'/api/access/project-accounts/([^/]+)', path)
                        if match and read:
                            require_project(match[1], manage=True)
                        else:
                            raise DomainError('此操作需要系统管理员权限', 403)
        if not read and request.headers.get('content-type', '').split(';')[0] == 'application/json':
            data = await request.json()
            self._body_scope(data, manage=True)
            if path == '/api/cases' and not data.get('project_id'):
                raise DomainError('请选择有管理权限的项目', 403)


def access_routes(service):
    router = APIRouter()

    @router.get('/api/auth/status')
    def status(request: Request):
        from .mobile_camera import is_local_admin_request
        setup_required = not bool(service.db.one('SELECT id FROM idi_users LIMIT 1'))
        return {'setup_required': setup_required,
                'setup_allowed': setup_required and is_local_admin_request(request)}

    @router.post('/api/auth/setup')
    def setup(request: Request, response: Response, data: dict = Body(...)):
        from .mobile_camera import is_local_admin_request
        if not is_local_admin_request(request):
            raise DomainError('首位系统管理员只能通过服务器本机管理入口创建。远程部署请使用已配置的 SSH 转发入口，完成后可在当前地址登录。', 403)
        user = service.save_user({**data, 'system_role': 'admin', 'enabled': True}, bootstrap=True)
        return service.issue_session(user['id'], request, response)

    @router.post('/api/auth/login')
    def login(request: Request, response: Response, data: dict = Body(...)):
        return service.login(data, request, response)

    @router.get('/api/auth/me')
    def me():
        return current().public()

    @router.post('/api/auth/logout')
    def logout(request: Request, response: Response):
        with service.db.tx() as con:
            con.execute('DELETE FROM idi_sessions WHERE token_hash=?', (token_hash(request.cookies.get(COOKIE, '')),))
        response.delete_cookie(COOKIE, path='/')
        return {'ok': True}

    @router.post('/api/auth/password')
    def password(request: Request, response: Response, data: dict = Body(...)):
        person = current()
        row = service.db.one('SELECT password_hash FROM idi_users WHERE id=?', (person.id,))
        if not password_matches(data.get('current_password'), row['password_hash']):
            raise DomainError('当前密码不正确', 403)
        encoded = password_hash(data.get('password'))
        with service.db.tx() as con:
            con.execute('UPDATE idi_users SET password_hash=?,updated_at=? WHERE id=?', (encoded, now(), person.id))
            con.execute('DELETE FROM idi_sessions WHERE user_id=?', (person.id,))
            con.execute('UPDATE camera_devices SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', (now(), person.id))
            con.execute('DELETE FROM camera_pairings WHERE user_id=? AND used_at IS NULL', (person.id,))
            audit(con, 'idi_user', person.id, 'change_password')
        return service.issue_session(person.id, request, response)

    @router.get('/api/access/users')
    def users():
        return service.users()

    @router.post('/api/access/users')
    def create_user(data: dict = Body(...)):
        return service.save_user(data)

    @router.patch('/api/access/users/{user_id}')
    def update_user(user_id: str, data: dict = Body(...)):
        return service.save_user(data, user_id)

    @router.put('/api/access/projects/{project_id}/members')
    def members(project_id: str, data: dict = Body(...)):
        return service.save_members(project_id, data)

    @router.get('/api/access/website-operations')
    def operations(project_id: str | None = None):
        from .identity import project_filter
        scope, args = project_filter('w.project_id')
        if project_id:
            require_project(project_id)
            scope += ' AND w.project_id=?'
            args.append(project_id)
        rows = service.db.all('SELECT w.*,pr.name AS project_name,c.title AS case_title FROM website_operations w '
                              'JOIN projects pr ON pr.id=w.project_id JOIN cases c ON c.id=w.case_id WHERE ' +
                              scope + ' ORDER BY w.created_at DESC LIMIT 200', args)
        for row in rows:
            for key in ('photo_ids', 'source_users'):
                row[key] = json.loads(row[key])
        return rows

    return router
