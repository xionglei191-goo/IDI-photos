import json

import pytest
from fastapi.testclient import TestClient

from idi.identity import acting_as


def add_jobs(services, count, project_id, *, owner=None, prefix='job'):
    rows = [(f'{prefix}-{index:04d}', 'export', json.dumps({'project_id': project_id}),
             'completed', json.dumps({'record': index}), '2026-10-26T08:00:00+00:00', owner)
            for index in range(count)]
    with services['db'].tx() as con:
        con.executemany('INSERT INTO jobs(id,kind,payload,state,result,created_at,user_id) VALUES(?,?,?,?,?,?,?)', rows)
    return [row[0] for row in rows]


def add_operations(services, count, case, *, prefix='operation'):
    rows = [(f'{prefix}-{index:04d}', case['project_id'], case['id'], 'readback',
             'isolated-account', 'isolated-revision', '[{"id":"source","display_name":"上传人员"}]',
             'verified', '2026-10-26T08:00:00+00:00', '') for index in range(count)]
    with services['db'].tx() as con:
        con.executemany('INSERT INTO website_operations(id,project_id,case_id,operation,website_account,'
                        'account_revision,source_users,state,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)', rows)
    return [row[0] for row in rows]


def person(services, app, project_id, username='records-user'):
    user = services['access'].save_user({'username': username, 'display_name': '记录测试人员',
                                        'password': 'isolated-records-password', 'system_role': 'user'})
    services['access'].save_members(project_id, {'members': [{'user_id': user['id'], 'role': 'manager'}]})
    client = TestClient(app, headers={'X-IDI-Request': '1'})
    response = client.post('/api/auth/login', json={'username': username, 'password': 'isolated-records-password'})
    assert response.status_code == 200
    return user, client


@pytest.mark.parametrize('path,legacy_limit', [('/api/jobs', 100), ('/api/access/website-operations', 200)])
def test_pages_include_history_beyond_legacy_limits_in_stable_order(client, services, case, path, legacy_limit):
    ids = add_jobs(services, 235, case['project_id']) if path == '/api/jobs' else add_operations(services, 235, case)
    legacy = client.get(path).json()
    assert isinstance(legacy, list) and len(legacy) == legacy_limit
    combined = []
    for number, expected_size in [(1, 100), (2, 100), (3, 35)]:
        response = client.get(path, params={'page': number, 'page_size': 100})
        assert response.status_code == 200, response.text
        data = response.json()
        assert (data['total'], data['page'], data['pages'], data['page_size']) == (235, number, 3, 100)
        assert len(data['items']) == expected_size
        combined.extend(row['id'] for row in data['items'])
    assert combined == sorted(ids, reverse=True)
    assert len(set(combined)) == 235
    last = client.get(path, params={'page': 999, 'page_size': 100}).json()
    assert last['page'] == 3 and [row['id'] for row in last['items']] == combined[200:]
    smaller = client.get(path, params={'page': 1, 'page_size': 10}).json()
    assert smaller['pages'] == 24 and [row['id'] for row in smaller['items']] == combined[:10]
    if path.endswith('website-operations'):
        assert smaller['items'][0]['source_users'][0]['display_name'] == '上传人员'


@pytest.mark.parametrize('path', ['/api/jobs', '/api/access/website-operations'])
def test_empty_page_and_invalid_pagination_arguments(client, path):
    empty = client.get(path, params={'page': 8, 'page_size': 20}).json()
    assert empty == {'items': [], 'total': 0, 'page': 1, 'page_size': 20, 'pages': 1}
    for query in ('page=0', 'page=-1', 'page=abc', 'page=1&page_size=0', 'page=1&page_size=101', 'page=1&page_size=abc'):
        assert client.get(path + '?' + query).status_code == 422, query


def test_operation_totals_and_pages_follow_project_permissions(app, client, services, case):
    add_operations(services, 27, case)
    other_project = services['domain'].save_project({'name': '另一项目'})
    other_case = services['domain'].save_case({'project_id': other_project['id'], 'title': '不可见案件'})
    add_operations(services, 210, other_case, prefix='hidden')
    _, member = person(services, app, case['project_id'])
    data = member.get('/api/access/website-operations?page=2&page_size=20').json()
    assert (data['total'], data['pages'], len(data['items'])) == (27, 2, 7)
    assert all(row['case_id'] == case['id'] for row in data['items'])
    forbidden = member.get('/api/access/website-operations', params={'project_id': other_project['id'], 'page': 1})
    assert forbidden.status_code == 403
    filtered = client.get('/api/access/website-operations', params={'project_id': other_project['id'], 'page': 2, 'page_size': 100}).json()
    assert filtered['total'] == 210 and len(filtered['items']) == 100
    services['access'].save_members(case['project_id'], {'members': []})
    removed = member.get('/api/access/website-operations?page=2&page_size=20').json()
    assert removed['total'] == 0 and removed['items'] == [] and removed['page'] == 1


def test_job_pages_count_only_owned_tasks_with_current_project_access(app, client, services, case):
    user, member = person(services, app, case['project_id'])
    allowed = add_jobs(services, 45, case['project_id'], owner=user['id'], prefix='a-visible')
    other = services['domain'].save_project({'name': '未分配项目'})
    add_jobs(services, 135, other['id'], owner=user['id'], prefix='z-revoked')
    add_jobs(services, 7, case['project_id'], prefix='z-system')
    with services['db'].tx() as con:
        con.execute("INSERT INTO jobs(id,kind,payload,created_at,user_id) VALUES('z-no-scope','export','{}',?,?)",
                    ('2026-10-27T08:00:00+00:00', user['id']))
    data = member.get('/api/jobs?page=3&page_size=20').json()
    assert (data['total'], data['pages'], data['page']) == (45, 3, 3)
    assert [row['id'] for row in data['items']] == sorted(allowed, reverse=True)[40:]
    assert member.get('/api/jobs/z-revoked-0000').status_code == 403
    assert member.get('/api/jobs/z-system-0000').status_code == 403
    services['access'].save_members(case['project_id'], {'members': []})
    empty = member.get('/api/jobs?page=3&page_size=20').json()
    assert empty['items'] == [] and empty['total'] == 0 and empty['page'] == 1


def test_job_scope_scan_and_page_share_one_read_snapshot(app, client, services, case, monkeypatch):
    user, _ = person(services, app, case['project_id'])
    add_jobs(services, 125, case['project_id'], owner=user['id'])
    principal = services['access'].principal(user['id'])
    connect = services['db'].connect
    calls = []
    def observed_connect():
        calls.append(True)
        return connect()
    monkeypatch.setattr(services['db'], 'connect', observed_connect)
    with acting_as(principal):
        data = services['jobs'].list_page(2, 20)
    assert data['total'] == 125 and len(data['items']) == 20
    assert len(calls) == 1
