"""Explicit, administrator-managed mapping from application Open IDs to IDI people."""
from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Body

from .db import audit, now, uid
from .domain import DomainError
from .identity import current, require_admin


class FeishuIdentities:
    def __init__(self, access, followup):
        self.access, self.followup, self.db = access, followup, access.db

    def list(self):
        require_admin()
        bindings = self.db.all(
            'SELECT b.*,u.display_name,u.username,u.enabled,c.name AS connector_name '
            'FROM feishu_identity_bindings b JOIN idi_users u ON u.id=b.user_id '
            'JOIN connectors c ON c.id=b.connector_id ORDER BY b.created_at,b.id')
        groups = [{'id': c['id'], 'name': c['config'].get('chat_name') or c['name'],
                   'app_id': c['config']['app_id']}
                  for c in self.followup.connectors.list() if c['kind'] == 'feishu' and c['enabled']]
        return {'bindings': bindings, 'groups': groups}

    def save(self, data):
        require_admin()
        connector_id, open_id, user_id = (data.get(k) for k in ('connector_id', 'open_id', 'user_id'))
        if not all(isinstance(v, str) and v for v in (connector_id, open_id, user_id)):
            raise DomainError('请选择飞书群、群成员和 IDI 人员')
        if not re.fullmatch(r'ou_[A-Za-z0-9]+', open_id):
            raise DomainError('飞书人员标识无效')
        connector = self.followup.connectors.get(connector_id)
        person = self.access.principal(user_id)
        member = next((m for m in self.followup.members(connector_id) if m['open_id'] == open_id), None)
        if not member:
            raise DomainError('此人员不在所选群成员名单中，请刷新后选择')
        app_id = connector['config']['app_id']
        if self.followup.connectors.get(connector_id)['config'] != connector['config']:
            raise DomainError('飞书入口已变化，请刷新后重新绑定', 409)
        stamp = now()
        try:
            with self.db.tx() as con:
                old = con.execute('SELECT * FROM feishu_identity_bindings WHERE app_id=? AND open_id=?',
                                  (app_id, open_id)).fetchone()
                binding_id = old['id'] if old else uid()
                con.execute(
                    'INSERT INTO feishu_identity_bindings VALUES(?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(app_id,open_id) DO UPDATE SET user_id=excluded.user_id,'
                    'connector_id=excluded.connector_id,name=excluded.name,revision=excluded.revision,updated_at=excluded.updated_at',
                    (binding_id, app_id, open_id, person.id, connector_id, member['name'], uid(), stamp, stamp))
                audit(con, 'feishu_identity', binding_id, 'bind',
                      dict(old) if old else None, {'app_id': app_id, 'open_id': open_id, 'user_id': person.id},
                      actor=current().id if current() else 'internal')
        except sqlite3.IntegrityError as error:
            raise DomainError('该 IDI 人员已绑定此飞书应用中的另一账号，请先解除原绑定', 409) from error
        return self.list()

    def remove(self, binding_id):
        require_admin()
        with self.db.tx() as con:
            old = con.execute('SELECT * FROM feishu_identity_bindings WHERE id=?', (binding_id,)).fetchone()
            if not old:
                raise DomainError('人员绑定不存在', 404)
            con.execute('DELETE FROM feishu_identity_bindings WHERE id=?', (binding_id,))
            audit(con, 'feishu_identity', binding_id, 'unbind', dict(old),
                  actor=current().id if current() else 'internal')
        return self.list()

    def resolve(self, app_id, open_id, project_id, *, manage=True, revision=None):
        binding = self.db.one('SELECT * FROM feishu_identity_bindings WHERE app_id=? AND open_id=?',
                              (app_id, open_id))
        if not binding:
            raise DomainError('请先由系统管理员在“人员与权限 → 飞书人员绑定”关联您的 IDI 账号', 403)
        if revision is not None and binding['revision'] != revision:
            raise DomainError('飞书人员绑定已改变，请重新核对', 403)
        person = self.access.principal(binding['user_id'])
        if not person.allows(project_id, manage=manage):
            raise DomainError('只有本项目管理员或系统管理员可以确认提交' if manage else '没有此项目的查看权限', 403)
        return person, binding


def feishu_identity_routes(service):
    router = APIRouter(prefix='/api/access/feishu-bindings')

    @router.get('')
    def listing():
        return service.list()

    @router.put('')
    def save(data: dict = Body(...)):
        return service.save(data)

    @router.delete('/{binding_id}')
    def remove(binding_id: str):
        return service.remove(binding_id)

    @router.get('/groups/{connector_id}/members')
    def members(connector_id: str):
        require_admin()
        return service.followup.members(connector_id)

    return router
