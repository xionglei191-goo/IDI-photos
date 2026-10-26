"""Live personnel scope for shared workbench conversations."""
import json

from .domain import DomainError
from .identity import Principal, current


def user_principal(db, user_id):
    row = db.one('SELECT id,username,display_name,system_role,enabled FROM idi_users WHERE id=?', (user_id,))
    if not row or not row['enabled']:
        raise DomainError('人员账号已停用，请重新登录', 403)
    projects = {r['project_id']: r['role'] for r in db.all(
        'SELECT project_id,role FROM project_members WHERE user_id=?', (user_id,))}
    return Principal(row['id'], row['username'], row['display_name'], row['system_role'], projects)


def check_thread_scope(db, thread, person=None):
    person = person or current()
    if person is None:
        return
    if thread.get('user_id') and thread['user_id'] != person.id:
        raise DomainError('对话不属于当前人员', 404)
    projects = json.loads(thread['project_ids'])
    if not person.admin and (not thread.get('user_id') or not projects or
                            any(not person.allows(p) for p in projects)):
        raise DomainError('对话的项目权限已变化，请新建对话', 403)
    if thread.get('case_id'):
        case = db.one('SELECT project_id FROM cases WHERE id=?', (thread['case_id'],))
        if not case or case['project_id'] not in projects or not person.allows(case['project_id']):
            raise DomainError('对话中的案件归属已变化，请新建对话', 403)


def all_project_scope(thread):
    # A deleted project can also leave project_id NULL. Its original label must
    # never turn that explicitly selected conversation into an all-project one.
    return (not thread.get('project_id') and not thread.get('case_id')
            and thread.get('context_label') in ('全部项目', '我的全部项目'))


def current_thread_projects(db, thread):
    """Refresh additions only after access to the saved history is revalidated."""
    check_thread_scope(db, thread)
    if not all_project_scope(thread):
        return json.loads(thread['project_ids'])
    person = current()
    return [row['id'] for row in db.all('SELECT id FROM projects ORDER BY name,id')
            if person is None or person.allows(row['id'])]
