import json

import pytest
from fastapi.testclient import TestClient

from idi.db import dump, now
from idi.domain import DomainError
from idi.identity import acting_as
from tests.test_project_access import make_person, login_person
from tests.test_official_camera import official, upload
from tests.test_camera_admin_access import remote_camera_app, remote_client, seed_people, login


@pytest.fixture
def people(app, services, project, client):
    manager = make_person(services, project['id'], 'manager', 'project-manager')
    worker = make_person(services, project['id'], 'worker', 'local-worker')
    other = services['domain'].save_project({'name': '另一个管理区域'})
    outsider = make_person(services, other['id'], 'worker', 'other-worker')
    web = login_person(app, manager['username'])
    yield {'manager': manager, 'worker': worker, 'other': other, 'outsider': outsider, 'web': web}
    web.close()


def member_state(web, project_id):
    rows = web.get('/api/access/users').json()
    return {u['id']: u['projects'][project_id] for u in rows if project_id in u['projects']}


def change_members(web, project_id, desired, expected=None):
    return web.put(f'/api/access/projects/{project_id}/members', json={
        'expected_members': member_state(web, project_id) if expected is None else expected,
        'members': [{'user_id': u, 'role': r} for u, r in desired.items()]})


@pytest.mark.parametrize('role', ['worker', 'member'])
def test_manager_creates_edits_and_disables_own_lower_account(app, services, project, people, role):
    web = people['web']
    response = web.post('/api/access/users', json={'username': 'new-staff', 'display_name': '新人员',
        'password': 'person-test-password', 'project_id': project['id'], 'project_role': role})
    assert response.status_code == 200, response.text
    user = response.json()
    assert services['access'].principal(user['id']).projects == {project['id']: role}
    assert web.patch('/api/access/users/' + user['id'], json={'display_name': '新姓名'}).status_code == 200
    logged = login_person(app, 'new-staff')
    paired = web.post('/api/camera/admin/pairings', json={'name': '新手机', 'project_ids': [project['id']], 'user_id': user['id']})
    assert paired.status_code == 200, paired.text
    phone = services['mobile_camera'].pair(paired.json()['code'])
    assert web.patch('/api/access/users/' + user['id'], json={'password': 'changed-staff-password'}).status_code == 200
    assert logged.get('/api/auth/me').status_code == 401
    assert services['db'].one('SELECT revoked_at FROM camera_devices WHERE id=?', (phone['device_id'],))['revoked_at']
    assert web.patch('/api/access/users/' + user['id'], json={'enabled': False}).status_code == 200
    assert not services['db'].one('SELECT enabled FROM idi_users WHERE id=?', (user['id'],))['enabled']
    assert web.patch('/api/access/users/' + user['id'], json={'enabled': True}).status_code == 200
    assert services['db'].one("SELECT actor FROM audit WHERE entity_type='idi_user' AND entity_id=? AND action='create'", (user['id'],))['actor'] == 'user:' + people['manager']['id']
    logged.close()


@pytest.mark.parametrize('change', [
    {'system_role': 'admin'}, {'project_role': 'manager'}, {'project_role': 'owner'},
    {'projects': {'outside': 'manager'}}, {'password': ''},
])
def test_manager_cannot_create_privileged_or_invalid_account(services, project, people, change):
    before = services['db'].all('SELECT * FROM idi_users')
    response = people['web'].post('/api/access/users', json={'username': 'attempt-account', 'display_name': '越权',
        'password': 'person-test-password', 'project_id': project['id'], 'project_role': 'worker', **change})
    assert response.status_code in (400, 403)
    assert services['db'].all('SELECT * FROM idi_users') == before


def test_scope_lists_no_other_accounts_and_cannot_escalate_or_target_other_projects(services, project, admin_user, people):
    web = people['web']
    rows = web.get('/api/access/users')
    assert rows.status_code == 200
    assert [u['id'] for u in rows.json()] == [people['worker']['id']]
    assert 'password_hash' not in rows.text and people['other']['id'] not in rows.text
    assert web.post('/api/access/users', json={'username': 'cross-account', 'display_name': '越权',
        'password': 'person-test-password', 'project_id': people['other']['id'], 'project_role': 'worker'}).status_code == 403
    for target in (people['manager'], people['outsider'], admin_user):
        assert web.patch('/api/access/users/' + target['id'], json={'display_name': '篡改', 'password': 'password-changed'}).status_code == 403
    assert web.patch('/api/access/users/' + people['worker']['id'], json={'system_role': 'admin'}).status_code == 403
    assert change_members(web, project['id'], {people['worker']['id']: 'manager'}).status_code == 403
    assert change_members(web, project['id'], {people['manager']['id']: 'worker'}).status_code == 403
    assert change_members(web, project['id'], {people['outsider']['id']: 'worker'}).status_code == 403
    assert change_members(web, people['other']['id'], {}, {}).status_code == 403
    for path in ('/api/access/project-accounts/' + project['id'], '/api/camera/admin/official/config'):
        assert web.put(path, json={'enabled': False}).status_code == 403


def test_shared_account_local_role_edit_preserves_other_project_and_managers(services, project, client, people):
    worker, web = people['worker'], people['web']
    services['access'].save_members(people['other']['id'], {'members': [
        {'user_id': worker['id'], 'role': 'member'}, {'user_id': people['outsider']['id'], 'role': 'worker'}]})
    row = web.get('/api/access/users').json()[0]
    assert row['projects'] == {project['id']: 'worker'} and not row['can_edit_account']
    before = services['db'].one('SELECT * FROM idi_users WHERE id=?', (worker['id'],))
    for data in ({'display_name': '改名'}, {'password': 'shared-new-password'}, {'enabled': False}):
        assert web.patch('/api/access/users/' + worker['id'], json=data).status_code == 403
    assert services['db'].one('SELECT * FROM idi_users WHERE id=?', (worker['id'],)) == before
    other_before = services['db'].all('SELECT * FROM project_members WHERE project_id=?', (people['other']['id'],))
    case = services['domain'].save_case({'project_id': project['id'], 'title': '工人案件'})
    assert client.post('/api/access/case-assignments', json={'case_ids': [case['id']], 'user_ids': [worker['id']],
        'mode': 'replace', 'expected_revisions': {case['id']: 0}}).status_code == 200
    response = change_members(web, project['id'], {worker['id']: 'member'})
    assert response.status_code == 200, response.text
    assert not services['db'].all('SELECT * FROM case_worker_assignments')
    assert services['db'].all('SELECT * FROM project_members WHERE project_id=?', (people['other']['id'],)) == other_before
    assert services['access'].principal(people['manager']['id']).projects[project['id']] == 'manager'
    assert change_members(web, project['id'], {}).status_code == 200
    assert worker['id'] not in [u['id'] for u in web.get('/api/access/users').json()]


def test_membership_conflict_and_live_manager_revocation_are_atomic(services, project, people):
    web = people['web']
    expected = member_state(web, project['id'])
    make_person(services, project['id'], 'member', 'concurrent-person')
    before = services['db'].all('SELECT * FROM project_members')
    assert change_members(web, project['id'], {}, expected).status_code == 409
    assert services['db'].all('SELECT * FROM project_members') == before
    stale = services['access'].principal(people['manager']['id'])
    services['access'].save_members(project['id'], {'members': [{'user_id': people['worker']['id'], 'role': 'worker'}]})
    with acting_as(stale), pytest.raises(DomainError) as failure:
        services['access'].save_user({'display_name': '过期权限'}, people['worker']['id'])
    assert failure.value.status == 403
    assert web.get('/api/access/users').status_code == 403


def test_manager_pairing_limits_device_and_worker_case_scope(app, services, project, case, client, people):
    web, worker = people['web'], people['worker']
    state = web.get('/api/camera/admin')
    assert state.status_code == 200, state.text
    assert [p['id'] for p in state.json()['projects']] == [project['id']]
    assert [u['id'] for u in state.json()['users']] == [worker['id']]
    assert not state.json()['can_configure']
    data = {'name': '项目工人手机', 'project_ids': [project['id']], 'user_id': worker['id']}
    code = web.post('/api/camera/admin/pairings', json=data)
    assert code.status_code == 200, code.text
    with TestClient(app, headers={'X-IDI-Request': '1'}) as phone:
        response = phone.post('/api/camera/pair', json={'code': code.json()['code']})
        assert response.status_code == 200, response.text
        device = response.json()
        headers = {'Authorization': 'Bearer ' + device['token']}
        assert phone.get('/api/camera/cases', headers=headers).json()['items'] == []
        with services['db'].tx() as con:
            con.execute("UPDATE cases SET workflow_stage='repair',platform_report_code='PAIR-CASE-1' WHERE id=?", (case['id'],))
        assert client.post('/api/access/case-assignments', json={'case_ids': [case['id']], 'user_ids': [worker['id']],
            'mode': 'replace', 'expected_revisions': {case['id']: 0}}).status_code == 200
        assert [c['id'] for c in phone.get('/api/camera/cases', headers=headers).json()['items']] == [case['id']]
        assert web.delete('/api/camera/admin/devices/' + device['device_id'] + '/record').status_code == 409
        assert web.delete('/api/camera/admin/devices/' + device['device_id']).status_code == 200
        assert phone.get('/api/camera/cases', headers=headers).status_code == 401
        assert web.delete('/api/camera/admin/devices/' + device['device_id'] + '/record').status_code == 200
        assert services['db'].one('SELECT deleted_at FROM camera_devices WHERE id=?', (device['device_id'],))['deleted_at']
    for data in ({**data, 'user_id': people['manager']['id']}, {**data, 'user_id': people['outsider']['id']},
                 {**data, 'project_ids': [project['id'], people['other']['id']]}):
        assert web.post('/api/camera/admin/pairings', json=data).status_code in (400, 403)


@pytest.mark.parametrize('change', ['issuer_removed', 'issuer_disabled', 'target_promoted', 'target_removed'])
def test_unused_pairing_rechecks_issuer_and_target(services, project, people, change):
    web, worker, manager = people['web'], people['worker'], people['manager']
    code = web.post('/api/camera/admin/pairings', json={'name': '待兑换', 'project_ids': [project['id']], 'user_id': worker['id']}).json()['code']
    if change == 'issuer_disabled':
        services['access'].save_user({'enabled': False}, manager['id'])
    else:
        desired = [{'user_id': manager['id'], 'role': 'manager'}, {'user_id': worker['id'], 'role': 'worker'}]
        if change == 'issuer_removed':
            desired.pop(0)
        elif change == 'target_removed':
            desired.pop(1)
        else:
            desired[1]['role'] = 'manager'
        services['access'].save_members(project['id'], {'members': desired})
    with pytest.raises(DomainError) as failure:
        services['mobile_camera'].pair(code)
    assert failure.value.status in (401, 403)
    assert not services['db'].all('SELECT * FROM camera_devices')


def test_cross_project_and_manager_devices_are_not_exposed_or_revoked(services, project, admin_user, people):
    worker, web, other = people['worker'], people['web'], people['other']
    services['access'].save_members(other['id'], {'members': [{'user_id': worker['id'], 'role': 'member'}]})
    camera = services['mobile_camera']
    devices = [camera.pair(camera.create_pairing({'name': '受保护设备', 'project_ids': ids, 'user_id': owner})['code'])
        for owner, ids in ((worker['id'], [project['id'], other['id']]), (admin_user['id'], [project['id']]),
                           (people['manager']['id'], [project['id']]))]
    assert web.get('/api/camera/admin').json()['devices'] == []
    for d in devices:
        assert web.delete('/api/camera/admin/devices/' + d['device_id']).status_code == 403
        assert web.delete('/api/camera/admin/devices/' + d['device_id'] + '/record').status_code == 403
        assert not services['db'].one('SELECT revoked_at FROM camera_devices WHERE id=?', (d['device_id'],))['revoked_at']
    code = web.post('/api/camera/admin/pairings', json={'name': '仅本项目手机', 'project_ids': [project['id']], 'user_id': worker['id']})
    assert code.status_code == 200  # shared personnel can receive a project-limited device


def test_official_history_is_filtered_before_limit_and_hides_configuration(client, official, services, project, people):
    result = upload(client, official)
    assert result.status_code == 200, result.text
    other_case = services['domain'].save_case({'project_id': people['other']['id'], 'title': '其他区域案件'})
    row = services['db'].one('SELECT * FROM camera_official_captures')
    with services['db'].tx() as con:
        for n in range(101):
            item = dict(row)
            item.update(capture_id=f'other-capture-{n:03}', photo_id=None, archived_at=None,
                        created_at='2099-01-01T00:00:00+00:00', state='queued',
                        metadata=dump({**json.loads(row['metadata']), 'case_id': other_case['id'], 'case_number': 'HIDDEN-CASE'}))
            con.execute('INSERT INTO camera_official_captures(' + ','.join(item) + ') VALUES(' + ','.join('?' for _ in item) + ')', list(item.values()))
    response = people['web'].get('/api/camera/admin/official')
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data['captures']) == 1 and 'HIDDEN-CASE' not in response.text
    assert set(data['config']) == {'enabled', 'auto_verify', 'ready'} and not data['can_configure']
    assert len(client.get('/api/camera/admin/official').json()['captures']) == 100
    for action in ('retry', 'report'):
        assert people['web'].post(f"/api/camera/admin/official/{row['device_id']}/{row['capture_id']}/{action}", json={}).status_code == 403


@pytest.mark.parametrize('entry', ['public', 'lan'])
def test_project_manager_camera_access_on_configured_entries(remote_camera_app, entry):
    _, manager, project = seed_people(remote_camera_app, 'manager')
    services = remote_camera_app.state.services
    worker = make_person(services, project['id'], 'worker', 'remote-worker')
    with remote_client(remote_camera_app, entry) as client:
        login(client, 'camera-worker')
        assert client.get('/camera-setup').status_code == 200
        assert client.get('/api/camera/admin').status_code == 200
        assert client.get('/api/camera/admin/official').status_code == 200
        data = {'name': '远程管理配对', 'project_ids': [project['id']], 'user_id': worker['id']}
        assert client.post('/api/camera/admin/pairings', json=data, headers={'X-IDI-Request': ''}).status_code == 403
        assert client.post('/api/camera/admin/pairings', json=data, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/camera/admin/pairings', json=data).status_code == 200
