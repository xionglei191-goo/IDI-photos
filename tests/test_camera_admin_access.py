import pytest
from fastapi.testclient import TestClient

from idi.api import create_app
from idi.config import Settings


@pytest.fixture
def remote_camera_app(tmp_path):
    return create_app(Settings(
        (tmp_path / 'archive').resolve(), host='0.0.0.0', worker_enabled=False,
        lan_hosts=('192.168.31.252',), lan_subnets=('192.168.31.0/24',),
        public_origins=('https://idi.example.test',), trusted_proxies=('192.168.31.246',),
    ))


def remote_client(app, entry='public'):
    if entry == 'public':
        origin, peer = 'https://idi.example.test', '192.168.31.246'
        headers = {'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '203.0.113.15'}
    else:
        origin, peer = 'http://192.168.31.252:8766', '192.168.31.80'
        headers = {}
    return TestClient(app, base_url=origin, client=(peer, 50000),
                      headers={**headers, 'X-IDI-Request': '1', 'Origin': origin})


def seed_people(app, role='member'):
    services = app.state.services
    access = services['access']
    admin = access.save_user({'username': 'camera-admin', 'display_name': '相机管理员',
                             'password': 'isolated-camera-password', 'system_role': 'admin'}, bootstrap=True)
    project = services['domain'].save_project({'name': '手机配对测试项目'})
    worker = access.save_user({'username': 'camera-worker', 'display_name': '配对人员',
                              'password': 'isolated-camera-password', 'system_role': 'user'})
    access.save_members(project['id'], {'members': [{'user_id': worker['id'], 'role': role}]})
    return admin, worker, project


def login(client, name='camera-admin'):
    result = client.post('/api/auth/login', json={'username': name, 'password': 'isolated-camera-password'})
    assert result.status_code == 200, result.text


@pytest.mark.parametrize('entry', ['public', 'lan'])
def test_remote_admin_can_open_pair_and_revoke_without_localhost(remote_camera_app, entry):
    app = remote_camera_app
    _, worker, project = seed_people(app)
    services = app.state.services
    case = services['domain'].save_case({'project_id': project['id'], 'title': '维修测试案件',
                                       'platform_report_code': 'CAMERA-TEST-001'})
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair' WHERE id=?", (case['id'],))
    with remote_client(app, entry) as client:
        login(client)
        page = client.get('/camera-setup')
        assert page.status_code == 200, page.text
        assert '连接 IDI 水印相机' in page.text
        state = client.get('/api/camera/admin')
        assert state.status_code == 200, state.text
        assert state.json()['server_urls'] == ['https://idi.example.test']
        assert 'token_hash' not in state.text and 'password_hash' not in state.text
        assert client.get('/api/camera/admin/official').status_code == 200
        pairing = client.post('/api/camera/admin/pairings', json={
            'name': '公网配对手机', 'user_id': worker['id'], 'project_ids': [project['id']],
        })
        assert pairing.status_code == 200, pairing.text
        with remote_client(app, entry) as phone:
            paired = phone.post('/api/camera/pair', json={'code': pairing.json()['code']})
            assert paired.status_code == 200, paired.text
            device = paired.json()
            assert device['user_id'] == worker['id']
            assert phone.post('/api/camera/pair', json={'code': pairing.json()['code']}).status_code == 401
            headers = {'Authorization': 'Bearer ' + device['token']}
            assert [c['id'] for c in phone.get('/api/camera/cases', headers=headers).json()['items']] == [case['id']]
            assert phone.get('/api/camera/admin', headers=headers).status_code == 401
            assert client.delete('/api/camera/admin/devices/' + device['device_id']).status_code == 200
            assert phone.get('/api/camera/cases', headers=headers).status_code == 401


@pytest.mark.parametrize('role', ['anonymous', 'member', 'manager'])
def test_remote_camera_management_requires_system_admin(remote_camera_app, role):
    app = remote_camera_app
    _, worker, project = seed_people(app, role if role != 'anonymous' else 'member')
    with remote_client(app) as client:
        if role != 'anonymous':
            login(client, 'camera-worker')
        expected = 401 if role == 'anonymous' else 403
        for path in ('/camera-setup', '/api/camera/admin', '/api/camera/admin/official'):
            assert client.get(path).status_code == expected, path
        assert client.post('/api/camera/admin/pairings', json={
            'name': '不应配对', 'user_id': worker['id'], 'project_ids': [project['id']],
        }).status_code == expected
        assert client.delete('/api/camera/admin/devices/missing').status_code == expected
        assert client.delete('/api/camera/admin/devices/missing/record').status_code == expected
        assert client.put('/api/camera/admin/official/config', json={'enabled': False}).status_code == expected
        for action in ('retry', 'report'):
            assert client.post('/api/camera/admin/official/device/capture/' + action, json={}).status_code == expected
    assert not app.state.services['db'].all('SELECT * FROM camera_pairings')


def test_delete_requires_revocation_and_preserves_disabled_credentials(client, services, project, admin_user):
    camera = services['mobile_camera']
    device = camera.pair(camera.create_pairing({
        'name': '待删除手机', 'user_id': admin_user['id'], 'project_ids': [project['id']],
    })['code'])
    path = '/api/camera/admin/devices/' + device['device_id']
    assert client.delete(path + '/record').status_code == 409
    assert client.delete(path + '/record', headers={'X-IDI-Request': ''}).status_code == 403
    assert client.delete(path).status_code == 200
    assert device['device_id'] in [d['id'] for d in client.get('/api/camera/admin').json()['devices']]
    assert client.delete(path + '/record').json() == {'deleted': True}
    assert device['device_id'] not in [d['id'] for d in client.get('/api/camera/admin').json()['devices']]
    assert client.get('/api/camera/cases', headers={'Authorization': 'Bearer ' + device['token']}).status_code == 401
    assert client.delete(path + '/record').status_code == 404
    row = services['db'].one('SELECT * FROM camera_devices WHERE id=?', (device['device_id'],))
    assert row['deleted_at'] and row['revoked_at']
    assert services['db'].one("SELECT * FROM audit WHERE entity_type='camera_device' AND entity_id=? AND action='delete'", (device['device_id'],))


def test_remote_pairing_keeps_origin_and_project_checks(remote_camera_app):
    app = remote_camera_app
    _, worker, project = seed_people(app)
    other = app.state.services['domain'].save_project({'name': '未分配项目'})
    with remote_client(app) as client:
        login(client)
        data = {'name': '测试手机', 'user_id': worker['id'], 'project_ids': [project['id']]}
        for headers in ({'Origin': 'https://other.example.test'}, {'X-IDI-Request': ''}):
            assert client.post('/api/camera/admin/pairings', json=data, headers=headers).status_code == 403
        assert client.post('/api/camera/admin/pairings', json={**data, 'project_ids': [other['id']]}).status_code == 403
    assert not app.state.services['db'].all('SELECT * FROM camera_pairings')
