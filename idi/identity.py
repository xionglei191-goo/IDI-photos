"""Request identity and project scope shared by HTTP, jobs and audit records.

An absent identity is an internal service call, never an anonymous HTTP grant.
The HTTP access layer must authenticate before entering business handlers.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    id: str
    username: str
    display_name: str
    system_role: str
    projects: dict[str, str] = field(default_factory=dict)
    assigned_cases: frozenset[str] = field(default_factory=frozenset)

    @property
    def admin(self):
        return self.system_role == 'admin'

    def allows(self, project_id, manage=False):
        return self.admin or bool(project_id and self.projects.get(project_id) in
                                  (('manager',) if manage else ('manager', 'member')))

    def camera_project(self, project_id):
        return self.admin or self.projects.get(project_id) in ('manager', 'member', 'worker')

    def allows_case(self, project_id, case_id):
        return self.allows(project_id) or (
            self.projects.get(project_id) == 'worker' and case_id in self.assigned_cases)

    def public(self):
        return {'id': self.id, 'username': self.username, 'display_name': self.display_name,
                'system_role': self.system_role, 'projects': self.projects}


_identity: ContextVar[Principal | None] = ContextVar('idi_identity', default=None)


def current() -> Principal | None:
    return _identity.get()


@contextmanager
def acting_as(principal):
    token = _identity.set(principal)
    try:
        yield principal
    finally:
        _identity.reset(token)


def require_project(project_id, *, manage=False):
    person = current()
    if person is not None and not person.allows(project_id, manage):
        from .domain import DomainError
        raise DomainError('没有此项目的管理权限' if manage else '没有此项目的访问权限', 403)


def require_admin():
    person = current()
    if person is not None and not person.admin:
        from .domain import DomainError
        raise DomainError('此操作需要系统管理员权限', 403)


def project_filter(column, *, manage=False):
    """Parameterized SQL condition; callers supply a constant column name."""
    person = current()
    if person is None or person.admin:
        return '1=1', []
    ids = [pid for pid, role in person.projects.items()
           if role in (('manager',) if manage else ('manager', 'member'))]
    if not ids:
        return '0=1', []
    return column + ' IN (' + ','.join('?' for _ in ids) + ')', ids


def require_case(project_id, case_id):
    person = current()
    if person is not None and not person.allows_case(project_id, case_id):
        from .domain import DomainError
        raise DomainError('没有此案件的访问权限，请联系管理员分配', 403)


def case_filter(project_column='c.project_id', case_column='c.id'):
    """Full-project members plus explicit, still-valid worker assignments."""
    scope, args = project_filter(project_column)
    person = current()
    if person is None or person.admin or 'worker' not in person.projects.values():
        return scope, args
    return (f'({scope} OR EXISTS (SELECT 1 FROM case_worker_assignments wa '
            'JOIN project_members wm ON wm.project_id=wa.project_id AND wm.user_id=wa.user_id '
            'JOIN idi_users wu ON wu.id=wa.user_id '
            f'WHERE wa.case_id={case_column} AND wa.project_id={project_column} '
            "AND wa.user_id=? AND wm.role='worker' AND wu.enabled=1))", [*args, person.id])
