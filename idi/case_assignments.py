"""Explicit, audited worker-to-case assignments. No implied project-wide grant."""
from __future__ import annotations

import json

from fastapi import APIRouter, Body

from .db import audit, now
from .domain import DomainError
from .identity import current, require_project


def require_uploader_case(db, photo, case):
    """Queued processing must still be authorized by the original uploader."""
    user_id = photo.get('uploaded_by')
    if not user_id:
        return
    user = db.one('SELECT enabled,system_role FROM idi_users WHERE id=?', (user_id,))
    if not user or not user['enabled']:
        raise DomainError('上传人员账号已失效，照片保留，请管理员核对', 403)
    if user['system_role'] == 'admin':
        return
    member = db.one('SELECT role FROM project_members WHERE project_id=? AND user_id=?', (case['project_id'], user_id))
    if member and member['role'] in ('manager', 'member'):
        return
    if not member or member['role'] != 'worker' or not db.one(
            'SELECT 1 FROM case_worker_assignments WHERE user_id=? AND case_id=? AND project_id=?',
            (user_id, case['id'], case['project_id'])):
        raise DomainError('上传人员的案件权限已失效，照片保留，请管理员核对', 403)
    for source in photo.get('sources', []):
        metadata = source.get('metadata', {})
        if source.get('source_type') not in ('camera', 'xhey_camera') or metadata.get('uploaded_by') != user_id:
            continue
        device = db.one('SELECT project_ids FROM camera_devices WHERE id=? AND user_id=? AND revoked_at IS NULL',
                        (metadata.get('device_id'), user_id))
        if device and case['project_id'] in json.loads(device['project_ids']):
            return
    raise DomainError('工人原拍摄设备的绑定权限已失效，照片保留，请管理员核对', 403)


def assignment_changed(con, case_id):
    con.execute('UPDATE cases SET worker_assignment_revision=worker_assignment_revision+1 WHERE id=?', (case_id,))
    con.execute('UPDATE photo_pipeline SET dirty=1 WHERE photo_id IN (SELECT id FROM photos WHERE case_id=? AND uploaded_by IS NOT NULL)', (case_id,))


def assigned_workers(con, case_id):
    return [dict(row) for row in con.execute('''SELECT a.user_id,u.display_name,a.assigned_at
        FROM case_worker_assignments a JOIN idi_users u ON u.id=a.user_id
        JOIN cases c ON c.id=a.case_id AND c.project_id=a.project_id
        JOIN project_members m ON m.user_id=a.user_id AND m.project_id=a.project_id
        WHERE a.case_id=? AND m.role='worker' AND u.enabled=1
        ORDER BY u.display_name,u.id''', (case_id,))]


def revoke_ineligible(con, project_id):
    """Role removal must not resurrect old assignments if membership is restored."""
    rows = con.execute('''SELECT a.case_id,a.user_id FROM case_worker_assignments a
        WHERE a.project_id=? AND NOT EXISTS (SELECT 1 FROM project_members m
          WHERE m.project_id=a.project_id AND m.user_id=a.user_id AND m.role='worker')''',
                       (project_id,)).fetchall()
    for case_id in sorted({r['case_id'] for r in rows}):
        removed = [r['user_id'] for r in rows if r['case_id'] == case_id]
        con.executemany('DELETE FROM case_worker_assignments WHERE case_id=? AND user_id=?',
                        [(case_id, user_id) for user_id in removed])
        assignment_changed(con, case_id)
        audit(con, 'case_workers', case_id, 'membership_removed', after={'removed_user_ids': removed})


class CaseAssignments:
    def __init__(self, access):
        self.access, self.db = access, access.db

    def workers(self, project_id):
        require_project(project_id, manage=True)
        return self.db.all('''SELECT u.id,u.display_name,u.username FROM idi_users u
            JOIN project_members m ON m.user_id=u.id
            WHERE m.project_id=? AND m.role='worker' AND u.enabled=1 AND u.system_role='user'
            ORDER BY u.display_name,u.id''', (project_id,))

    def detail(self, case_id):
        self.access.resource('cases', case_id, manage=True)
        with self.db.read_snapshot() as con:
            # Database snapshots expose their connection through read helpers.
            case = self.db.one('SELECT worker_assignment_revision FROM cases WHERE id=?', (case_id,))
            workers = self.db.all('''SELECT a.user_id,u.display_name,a.assigned_at,u.enabled,m.role
                FROM case_worker_assignments a JOIN idi_users u ON u.id=a.user_id
                JOIN project_members m ON m.user_id=a.user_id AND m.project_id=a.project_id
                WHERE a.case_id=? ORDER BY u.display_name,u.id''', (case_id,))
            history = self.db.all('''SELECT a.id,a.action,a.before_json,a.after_json,a.created_at,
                COALESCE(u.display_name,a.actor) AS actor_name FROM audit a
                LEFT JOIN idi_users u ON a.actor='user:'||u.id
                WHERE a.entity_type='case_workers' AND a.entity_id=?
                ORDER BY a.created_at DESC,a.rowid DESC LIMIT 30''', (case_id,))
        for row in history:
            row['before'] = json.loads(row.pop('before_json') or 'null')
            row['after'] = json.loads(row.pop('after_json') or 'null')
        return {'case_id': case_id, 'revision': case['worker_assignment_revision'],
                'workers': workers, 'history': history}

    def save(self, data):
        case_ids, user_ids = data.get('case_ids'), data.get('user_ids')
        mode, expected = data.get('mode', 'add'), data.get('expected_revisions')
        if (not isinstance(case_ids, list) or not 1 <= len(case_ids) <= 100
                or any(not isinstance(v, str) or not v for v in case_ids)
                or len(set(case_ids)) != len(case_ids)):
            raise DomainError('请选择1至100个不同案件')
        if (not isinstance(user_ids, list) or len(user_ids) > 100
                or any(not isinstance(v, str) or not v for v in user_ids)
                or len(set(user_ids)) != len(user_ids) or mode not in ('add', 'remove', 'replace')):
            raise DomainError('工人分配格式无效')
        if not isinstance(expected, dict) or any(type(expected.get(cid)) is not int for cid in case_ids):
            raise DomainError('请刷新案件分配状态后重试', 409)
        if not user_ids and mode != 'replace':
            raise DomainError('请选择工人')
        with self.db.tx() as con:
            person = current()
            person = self.access.principal(person.id) if person else None
            cases = []
            for case_id in case_ids:
                row = con.execute('SELECT id,project_id,worker_assignment_revision FROM cases WHERE id=?', (case_id,)).fetchone()
                if not row:
                    raise DomainError('案件不存在', 404)
                if person and not person.allows(row['project_id'], manage=True):
                    raise DomainError('只能为自己管理的项目分配工人', 403)
                if row['worker_assignment_revision'] != expected[case_id]:
                    raise DomainError('案件分配已被其他管理员修改，请刷新后重试', 409)
                cases.append(dict(row))
            projects = {row['project_id'] for row in cases}
            if len(projects) != 1:
                raise DomainError('批量分配请先选择同一项目的案件')
            project_id = next(iter(projects))
            for user_id in user_ids:
                user = con.execute('''SELECT u.id FROM idi_users u JOIN project_members m ON m.user_id=u.id
                    WHERE u.id=? AND u.enabled=1 AND u.system_role='user'
                      AND m.project_id=? AND m.role='worker' ''', (user_id, project_id)).fetchone()
                if not user and mode != 'remove':
                    raise DomainError('只能分配本项目已启用的工人账号', 403)
            result = []
            for case in cases:
                cid = case['id']
                before = [dict(r) for r in con.execute('''SELECT a.user_id,u.display_name FROM case_worker_assignments a
                    JOIN idi_users u ON u.id=a.user_id WHERE a.case_id=? ORDER BY a.user_id''', (cid,))]
                old_ids = {row['user_id'] for row in before}
                new_ids = set(user_ids) if mode == 'replace' else old_ids | set(user_ids) if mode == 'add' else old_ids - set(user_ids)
                if new_ids != old_ids:
                    con.executemany('DELETE FROM case_worker_assignments WHERE case_id=? AND user_id=?',
                                    [(cid, user_id) for user_id in old_ids - new_ids])
                    con.executemany('INSERT INTO case_worker_assignments VALUES(?,?,?,?,?)',
                                    [(cid, user_id, project_id, person.id if person else None, now())
                                     for user_id in new_ids - old_ids])
                    assignment_changed(con, cid)
                    audit(con, 'case_workers', cid, mode, before=before, after=assigned_workers(con, cid))
                result.append({'case_id': cid, 'revision': case['worker_assignment_revision'] + int(new_ids != old_ids),
                               'workers': assigned_workers(con, cid)})
        return {'items': result, 'total': len(result)}


def worker_routes(access):
    router, service = APIRouter(), CaseAssignments(access)

    @router.get('/api/access/projects/{project_id}/workers')
    def workers(project_id: str):
        return service.workers(project_id)

    @router.get('/api/cases/{case_id}/workers')
    def detail(case_id: str):
        return service.detail(case_id)

    @router.post('/api/access/case-assignments')
    def save(data: dict = Body(...)):
        return service.save(data)

    return router
