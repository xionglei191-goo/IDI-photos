import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from idi.identity import acting_as


def make_person(services, project_id, role='member', name='worker'):
    user = services['access'].save_user({'username': name, 'display_name': '人员 ' + name,
                                        'password': 'person-test-password', 'system_role': 'user'})
    memberships = services['db'].all('SELECT user_id,role FROM project_members WHERE project_id=?', (project_id,))
    services['access'].save_members(project_id, {'members': [*memberships, {'user_id': user['id'], 'role': role}]})
    return user


def login_person(app, name='worker'):
    client = TestClient(app, headers={'X-IDI-Request': '1'})
    result = client.post('/api/auth/login', json={'username': name, 'password': 'person-test-password'})
    assert result.status_code == 200, result.text
    return client


def png():
    output = io.BytesIO()
    Image.new('RGB', (40, 40), 'orange').save(output, 'PNG')
    return output.getvalue()


def test_first_setup_is_local_one_time_and_no_default_login(app):
    client = TestClient(app, headers={'X-IDI-Request': '1'})
    assert client.get('/api/auth/status').json() == {'setup_required': True, 'setup_allowed': True}
    anonymous = client.get('/api/projects')
    assert anonymous.status_code == 401 and anonymous.headers['x-idi-auth'] == 'expired'
    data = {'username': 'owner', 'display_name': '初始管理员', 'password': 'one-time-owner-password'}
    forwarded = {'X-Forwarded-Proto': 'https'}
    assert client.get('/api/auth/status', headers=forwarded).json() == {'setup_required': True, 'setup_allowed': False}
    rejected = client.post('/api/auth/setup', json=data, headers=forwarded)
    assert rejected.status_code == 403
    assert '系统管理员' in rejected.json()['detail'] and '手机' not in rejected.json()['detail']
    result = client.post('/api/auth/setup', json=data)
    assert result.status_code == 200
    assert 'HttpOnly' in result.headers['set-cookie'] and 'SameSite=strict' in result.headers['set-cookie']
    assert 'password' not in result.text
    assert client.post('/api/auth/setup', json=data).status_code == 409
    assert client.get('/api/auth/me').json()['system_role'] == 'admin'
    assert client.get('/api/auth/status').json() == {'setup_required': False, 'setup_allowed': False}


def test_anonymous_business_and_media_are_closed(app, services, case, photo):
    p = photo()
    client = TestClient(app, headers={'X-IDI-Request': '1'})
    for path in ['/api/projects', '/api/cases', '/api/summary', '/api/jobs', '/api/docs',
                 '/api/photos/' + p['id'] + '/original', '/api/photos/' + p['id'] + '/thumbnail',
                 '/api/platform/account', '/api/backups', '/api/camera/admin', '/api/workflow/board']:
        assert client.get(path).status_code == 401, path
    assert client.post('/api/projects', json={'name': 'unauthorized'}).status_code == 401
    assert client.get('/api/health').status_code == 200


def test_project_scope_filters_counts_pagination_and_prevents_idor(app, services, project, case, photo, client):
    mine = photo()
    other_project = services['domain'].save_project({'name': '无权查看项目'})
    other_case = services['domain'].save_case({'project_id': other_project['id'], 'title': '保密案件'})
    other_photo = services['importer'].add_file(png(), '保密.png', case_id=other_case['id'])['photo_id']
    make_person(services, project['id'])
    worker = login_person(app)
    assert [p['id'] for p in worker.get('/api/projects').json()] == [project['id']]
    assert [c['id'] for c in worker.get('/api/cases').json()] == [case['id']]
    page = worker.get('/api/photos?page_size=1').json()
    assert page['total'] == 1 and page['items'][0]['id'] == mine['id']
    assert worker.get('/api/summary').json()['total'] == 1
    board = worker.get('/api/workflow/board?board=intake&source=local').json()
    assert board['total'] == 1 and board['source_total'] == 1
    for path in ['/api/cases/' + other_case['id'], '/api/photos/' + other_photo,
                 '/api/photos/' + other_photo + '/original', '/api/photos/' + other_photo + '/thumbnail',
                 '/api/workflow/cases/' + other_case['id'],
                 '/api/platform/cases/' + other_case['id'] + '/stages/intake']:
        assert worker.get(path).status_code == 403, path
    assert worker.get('/api/photos?project_id=' + other_project['id']).json()['total'] == 0
    assert 'data_dir' not in worker.get('/api/system').json()
    assert worker.get('/api/access/users').status_code == 403
    assert worker.get('/api/messages').status_code == 403
    assert worker.get('/api/backups').status_code == 403
    assert worker.post('/api/workflow/sync').status_code == 403


def test_member_upload_is_owned_and_cannot_modify_or_submit(app, services, project, case, client):
    user = make_person(services, project['id'])
    worker = login_person(app)
    result = worker.post('/api/import/upload', data={'case_id': case['id'], 'project_id': project['id'], 'auto_ocr': 'false'},
                         files={'files': ('现场.png', png(), 'image/png')})
    assert result.status_code == 200, result.text
    assert result.json()['job']['user_id'] == user['id']
    assert services['jobs'].run_one()
    row = services['db'].one('SELECT * FROM photos WHERE case_id=?', (case['id'],))
    assert row['uploaded_by'] == user['id']
    source = json.loads(services['db'].one('SELECT metadata FROM photo_sources WHERE photo_id=?', (row['id'],))['metadata'])
    assert source['uploaded_by'] == user['id']
    assert worker.patch('/api/photos/' + row['id'], json={'notes': '无权编辑'}).status_code == 403
    assert worker.delete('/api/photos/' + row['id']).status_code == 403
    assert worker.post('/api/photos/delete', json={'photo_ids': [row['id']]}).status_code == 403
    assert worker.post('/api/workflow/cases/' + case['id'] + '/review/intake', json={'revision': 0}).status_code == 403
    assert worker.post('/api/import/upload', files={'files': ('无归属.png', png(), 'image/png')}).status_code == 403
    logs = services['db'].all("SELECT actor FROM audit WHERE entity_type='photo' AND entity_id=?", (row['id'],))
    assert {'actor': 'user:' + user['id']} in logs


def test_manager_can_edit_delete_only_assigned_project_and_no_cross_project_move(app, services, project, case, photo, client):
    mine = photo()
    user = make_person(services, project['id'], 'manager', 'manager')
    manager = login_person(app, 'manager')
    result = manager.patch('/api/photos/' + mine['id'], json={'notes': '本项目修改'})
    assert result.status_code == 200
    other_project = services['domain'].save_project({'name': '另一个项目'})
    other_case = services['domain'].save_case({'project_id': other_project['id'], 'title': '另案'})
    assert manager.patch('/api/photos/' + mine['id'], json={'case_id': other_case['id']}).status_code == 403
    assert manager.post('/api/photos/bulk', json={'photo_ids': [mine['id']], 'patch': {'case_id': other_case['id']}}).status_code == 403
    assert manager.patch('/api/cases/' + case['id'], json={'project_id': other_project['id']}).status_code == 403
    assert manager.delete('/api/photos/' + mine['id']).status_code == 200
    services['access'].save_members(project['id'], {'members': []})
    assert manager.get('/api/cases/' + case['id']).status_code == 403
    assert manager.get('/api/projects').json() == []


def test_disable_password_reset_sessions_and_last_admin_protection(app, services, project, client, admin_user):
    user = make_person(services, project['id'])
    worker = login_person(app)
    services['access'].save_user({'enabled': False}, user['id'])
    assert worker.get('/api/auth/me').status_code == 401
    services['access'].save_user({'enabled': True}, user['id'])
    assert worker.get('/api/auth/me').status_code == 401  # old sessions stay revoked
    worker = login_person(app)
    services['access'].save_user({'password': 'changed-test-password'}, user['id'])
    assert worker.get('/api/auth/me').status_code == 401
    for data in ({'enabled': False}, {'system_role': 'user'}):
        assert client.patch('/api/access/users/' + admin_user['id'], json=data).status_code == 409
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/projects').status_code == 401


def test_device_uses_person_and_live_project_membership(app, services, project, case, client):
    user = make_person(services, project['id'])
    camera = services['mobile_camera']
    pair = client.post('/api/camera/admin/pairings', json={'name': '成员手机', 'project_ids': [project['id']], 'user_id': user['id']})
    assert pair.status_code == 200, pair.text
    device = camera.pair(pair.json()['code'])
    assert device['user_id'] == user['id']
    mobile = TestClient(app, headers={'X-IDI-Request': '1', 'Authorization': 'Bearer ' + device['token']})
    assert mobile.get('/api/camera/cases').status_code == 200
    assert mobile.get('/api/projects').status_code == 401
    assert mobile.get('/api/camera/admin').status_code == 401
    services['access'].save_members(project['id'], {'members': []})
    assert mobile.get('/api/camera/cases').status_code == 403
    assert client.post('/api/camera/admin/pairings', json={'name': '无权限', 'project_ids': [project['id']], 'user_id': user['id']}).status_code == 403


def test_queued_job_rechecks_removed_membership(app, services, project, case, client):
    user = make_person(services, project['id'])
    worker = login_person(app)
    result = worker.post('/api/import/upload', data={'case_id': case['id'], 'auto_ocr': 'false'},
                         files={'files': ('已排队.png', png(), 'image/png')}).json()
    services['access'].save_members(project['id'], {'members': []})
    assert services['jobs'].run_one()
    assert services['jobs'].get(result['job']['id'])['state'] == 'failed'
    assert not services['db'].all('SELECT id FROM photos')


def test_login_throttles_and_cross_origin_writes_still_block(app, client):
    anonymous = TestClient(app, headers={'X-IDI-Request': '1'})
    for _ in range(8):
        assert anonymous.post('/api/auth/login', json={'username': 'missing', 'password': 'bad'}).status_code == 401
    assert anonymous.post('/api/auth/login', json={'username': 'missing', 'password': 'bad'}).status_code == 429
    assert client.post('/api/projects', json={'name': 'evil'}, headers={'Origin': 'https://evil.example'}).status_code == 403


def test_domain_scope_applies_outside_http(services, project, case, client):
    user = make_person(services, project['id'])
    other_project = services['domain'].save_project({'name': '隐藏'})
    with acting_as(services['access'].principal(user['id'])):
        assert [p['id'] for p in services['domain'].projects()] == [project['id']]
        assert all(c['project_id'] != other_project['id'] for c in services['domain'].cases())
