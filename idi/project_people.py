"""Project-manager administration, checked against live rows under a write lock."""
from __future__ import annotations

import json
import re

from .db import audit, dump, now, uid
from .domain import DomainError
from .identity import current

LOWER_ROLES = ('member', 'worker')


def managed_projects(con, actor_id=None):
    """None denotes system/internal administration; an empty scope never grants."""
    actor = current()
    actor_id = actor_id or (actor.id if actor else None)
    if actor_id is None:
        return None
    user = con.execute('SELECT system_role,enabled FROM idi_users WHERE id=?', (actor_id,)).fetchone()
    if not user or not user['enabled']:
        raise DomainError('管理账号已停用或不存在', 403)
    if user['system_role'] == 'admin':
        return None
    projects = {r['project_id'] for r in con.execute(
        "SELECT project_id FROM project_members WHERE user_id=? AND role='manager'", (actor_id,))}
    if not projects:
        raise DomainError('此操作需要项目管理员权限', 403)
    return projects


def membership(con, user_id):
    return {r['project_id']: r['role'] for r in con.execute(
        'SELECT project_id,role FROM project_members WHERE user_id=?', (user_id,))}


def delegated_projects(con):
    scope = managed_projects(con)
    if scope is None:
        raise DomainError('人员管理权限已变化，请刷新页面', 409)
    return scope


def invalidate_pairings(con, project_id, changed_users):
    if not changed_users:
        return
    for row in con.execute('SELECT code_hash,project_ids,user_id,issued_by FROM camera_pairings WHERE used_at IS NULL').fetchall():
        if (row['user_id'] in changed_users or row['issued_by'] in changed_users) and project_id in json.loads(row['project_ids']):
            con.execute('DELETE FROM camera_pairings WHERE code_hash=?', (row['code_hash'],))


def require_pairing_scope(con, user_id, project_ids, *, issuer_id=None):
    scope = managed_projects(con, issuer_id)
    user = con.execute('SELECT system_role,enabled FROM idi_users WHERE id=?', (user_id,)).fetchone()
    if not user or not user['enabled']:
        raise DomainError('绑定人员已停用或不存在', 403)
    roles = membership(con, user_id)
    if scope is not None:
        if (user['system_role'] != 'user' or not set(project_ids) <= scope
                or any(roles.get(p) not in LOWER_ROLES for p in project_ids)):
            raise DomainError('只能为自己管理项目的成员和工人生成配对码', 403)
    elif user['system_role'] != 'admin' and any(p not in roles for p in project_ids):
        raise DomainError('手机所选项目必须已分配给绑定人员', 403)


def can_manage_device(con, device, scope):
    if scope is None:
        return True
    ids = json.loads(device['project_ids']) if isinstance(device['project_ids'], str) else device['project_ids']
    if not ids or not set(ids) <= scope:
        return False
    user = con.execute('SELECT system_role FROM idi_users WHERE id=?', (device['user_id'],)).fetchone()
    if not user or user['system_role'] != 'user':
        return False
    roles = membership(con, device['user_id'])
    # Retain the ability to revoke a former member's project-only connection.
    return all(roles.get(p) in (*LOWER_ROLES, None) for p in ids)


def require_device_scope(con, device_id):
    scope = managed_projects(con)
    row = con.execute('SELECT * FROM camera_devices WHERE id=?', (device_id,)).fetchone()
    if scope is not None and (not row or not can_manage_device(con, row, scope)):
        raise DomainError('没有此手机连接的管理权限，跨项目连接请联系系统管理员', 403)
    return row


class ProjectPeople:
    def __init__(self, db):
        self.db = db

    def users(self):
        with self.db.read_snapshot() as con:
            scope = delegated_projects(con)
            result = []
            for raw in con.execute('''SELECT id,username,display_name,system_role,enabled,created_at,updated_at
                    FROM idi_users WHERE system_role='user' ORDER BY created_at,id'''):
                row = dict(raw)
                roles = membership(con, row['id'])
                visible = {p: role for p, role in roles.items() if p in scope and role in LOWER_ROLES}
                if not visible:
                    continue
                editable = all(p in scope and role in LOWER_ROLES for p, role in roles.items())
                row.update(enabled=bool(row['enabled']), projects=visible, can_edit_account=editable,
                           protected_project_ids=[p for p, role in roles.items() if p in scope and role == 'manager'],
                           account_restriction='' if editable else '账号涉及其他项目或管理权限，仅可调整本项目角色；姓名、密码及账号启停由系统管理员维护。')
                result.append(row)
            return result

    def save_user(self, data, user_id=None):
        from .access import password_hash
        if set(data) - {'username', 'display_name', 'password', 'system_role', 'enabled', 'project_id', 'project_role'}:
            raise DomainError('人员设置包含不支持的字段')
        if data.get('system_role', 'user') != 'user':
            raise DomainError('项目管理员只能创建或修改项目成员和工人', 403)
        with self.db.tx() as con:
            scope = delegated_projects(con)
            existing = con.execute('SELECT * FROM idi_users WHERE id=?', (user_id,)).fetchone() if user_id else None
            if user_id:
                roles = membership(con, user_id)
                if (not existing or existing['system_role'] != 'user' or not roles
                        or any(p not in scope or r not in LOWER_ROLES for p, r in roles.items())):
                    raise DomainError('此账号涉及其他项目或管理权限，请由系统管理员修改；你可以调整本项目成员角色', 403)
                if 'project_id' in data or 'project_role' in data:
                    raise DomainError('请在项目人员配置中调整角色')
            else:
                project_id, project_role = data.get('project_id'), data.get('project_role', 'worker')
                if project_id not in scope or project_role not in LOWER_ROLES:
                    raise DomainError('请选择自己管理的项目，并设置为项目成员或工人', 403)
            existing = dict(existing) if existing else {}
            username = str(data.get('username', existing.get('username', ''))).strip()
            name = str(data.get('display_name', existing.get('display_name', ''))).strip()
            enabled = data.get('enabled', bool(existing.get('enabled', 1)))
            if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}', username) or not name or len(name) > 80:
                raise DomainError('账号须为3至80位字母、数字或 _.@-，并填写人员姓名（最多80字）')
            if not isinstance(enabled, bool):
                raise DomainError('账号状态格式无效')
            encoded = password_hash(data['password']) if data.get('password') else existing.get('password_hash')
            if not encoded:
                raise DomainError('请设置初始密码')
            user_id = user_id or uid()
            stamp = now()
            con.execute('''INSERT INTO idi_users VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,display_name=excluded.display_name,password_hash=excluded.password_hash,
                enabled=excluded.enabled,updated_at=excluded.updated_at''',
                (user_id, username, name, encoded, 'user', int(enabled), existing.get('created_at', stamp), stamp))
            if not existing:
                con.execute('INSERT INTO project_members VALUES(?,?,?,?,?)', (project_id, user_id, project_role, stamp, stamp))
                audit(con, 'project_members', project_id, 'add', after={'user_id': user_id, 'role': project_role})
            elif data.get('password') or not enabled:
                con.execute('DELETE FROM idi_sessions WHERE user_id=?', (user_id,))
                con.execute('UPDATE camera_devices SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', (stamp, user_id))
                con.execute('DELETE FROM camera_pairings WHERE (user_id=? OR issued_by=?) AND used_at IS NULL', (user_id, user_id))
            result = {'id': user_id, 'username': username, 'display_name': name, 'system_role': 'user', 'enabled': enabled}
            audit(con, 'idi_user', user_id, 'update' if existing else 'create', after=result)
            return result

    def save_members(self, project_id, data):
        members, expected = data.get('members'), data.get('expected_members')
        if not isinstance(members, list) or len(members) > 500 or not isinstance(expected, dict):
            raise DomainError('请先读取当前项目人员配置，再保存修改', 409)
        with self.db.tx() as con:
            scope = delegated_projects(con)
            if project_id not in scope:
                raise DomainError('没有此项目的人员管理权限', 403)
            rows = con.execute('''SELECT pm.user_id,pm.role,u.system_role FROM project_members pm
                JOIN idi_users u ON u.id=pm.user_id WHERE pm.project_id=?''', (project_id,)).fetchall()
            before = {r['user_id']: r['role'] for r in rows if r['system_role'] == 'user' and r['role'] in LOWER_ROLES}
            protected = {r['user_id'] for r in rows if r['user_id'] not in before}
            if expected != before:
                raise DomainError('项目人员配置已变化，请刷新后重试', 409)
            desired = {}
            for item in members:
                if not isinstance(item, dict) or item.get('role') not in LOWER_ROLES or not isinstance(item.get('user_id'), str):
                    raise DomainError('只能设置项目成员或工人，不能授予管理权限', 403)
                target = item['user_id']
                user = con.execute('SELECT system_role FROM idi_users WHERE id=?', (target,)).fetchone()
                roles = membership(con, target)
                if (target in protected or not user or user['system_role'] != 'user'
                        or not any(p in scope and r in LOWER_ROLES for p, r in roles.items())):
                    raise DomainError('只能选择自己管理项目中的成员或工人', 403)
                if target in desired:
                    raise DomainError('项目人员重复')
                desired[target] = item['role']
            changed = {u for u in before.keys() | desired.keys() if before.get(u) != desired.get(u)}
            stamp = now()
            for target in before.keys() - desired.keys():
                con.execute('DELETE FROM project_members WHERE project_id=? AND user_id=?', (project_id, target))
            for target in changed & desired.keys():
                con.execute('''INSERT INTO project_members VALUES(?,?,?,?,?) ON CONFLICT(project_id,user_id)
                    DO UPDATE SET role=excluded.role,updated_at=excluded.updated_at''',
                    (project_id, target, desired[target], stamp, stamp))
            from .case_assignments import revoke_ineligible
            revoke_ineligible(con, project_id)
            invalidate_pairings(con, project_id, changed)
            audit(con, 'project_members', project_id, 'update', before=before, after=desired)
            return {'project_id': project_id, 'members': members}
