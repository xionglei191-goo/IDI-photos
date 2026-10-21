import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from idi.db import now


def jpeg(color):
    out = io.BytesIO()
    Image.new('RGB', (400, 600), color).save(out, 'JPEG')
    return out.getvalue()


@pytest.fixture
def camera(services, project, case, admin_user):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair', platform_report_code='ZLBH202610190000000001' WHERE id=?", (case['id'],))
    service = services['mobile_camera']
    pair = service.create_pairing({'name': '测试手机', 'project_ids': [project['id']], 'user_id': admin_user['id']})
    device = service.pair(pair['code'])
    headers = {'Authorization': 'Bearer ' + device['token']}
    metadata = {'schema_version': 1, 'capture_id': '12345678-1234-1234-1234-123456789abc', 'case_id': case['id'],
                'case_number': 'ZLBH202610190000000001', 'project_name': project['name'], 'location': '1栋 · 201室',
                'captured_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                'photo_group': 'construction', 'role': 'construction', 'part': '卫生间', 'content': '卫生间瓷砖修补'}
    return {'service': service, 'device': device, 'headers': headers, 'meta': metadata,
            'files': {'original': ('original.jpg', jpeg('navy'), 'image/jpeg'),
                      'stamped': ('stamped.jpg', jpeg('orange'), 'image/jpeg')}}


def upload(client, camera, **changes):
    return client.post('/api/camera/captures', headers=camera['headers'], files=camera['files'],
                       data={'metadata': json.dumps({**camera['meta'], **changes})})


def test_pairing_is_single_use_scoped_and_does_not_leak_credentials(client, camera, services, project):
    response = client.get('/api/camera/cases', headers=camera['headers'])
    assert response.status_code == 200
    assert response.json()['items'][0]['case_number'] == camera['meta']['case_number']
    assert 'contact_phone' not in response.text and 'policy_number' not in response.text
    assert client.get('/api/camera/cases').status_code == 401
    pair = camera['service'].create_pairing({'name': '手机2', 'project_ids': [project['id']], 'user_id': camera['device']['user_id']})
    assert client.post('/api/camera/pair', json={'code': pair['code']}).status_code == 200
    assert client.post('/api/camera/pair', json={'code': pair['code']}).status_code == 401
    admin = client.get('/api/camera/admin').json()
    assert 'token_hash' not in json.dumps(admin)
    assert 'token' not in json.dumps(admin)


def test_capture_retains_two_originals_and_does_not_advance_case(client, camera, services, case):
    before = services['domain'].case(case['id'])
    response = upload(client, camera)
    assert response.status_code == 200, response.text
    result = response.json()
    photo = services['domain'].photo(result['photo_id'])
    assert photo['case_id'] == case['id'] and photo['photo_group'] == 'construction'
    assert photo['part'] == '卫生间' and photo['process'] == '卫生间瓷砖修补'
    expected_time = datetime.fromisoformat(camera['meta']['captured_at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    assert photo['watermark_time'] == expected_time
    assert result['original_hash'] != result['stamped_hash']
    assert services['domain'].case(case['id']) == before
    source = services['db'].one('SELECT metadata FROM photo_sources WHERE photo_id=?', (photo['id'],))
    source = json.loads(source['metadata'])
    assert source['watermark_order'] == ['time', 'case_number', 'project', 'location', 'stage', 'content']
    assert source['time_basis'] == 'device_capture'
    raw = client.get('/api/camera/captures/' + camera['meta']['capture_id'] + '/original', headers=camera['headers'])
    assert raw.content == camera['files']['original'][1]


def test_retries_return_same_receipt_and_cannot_overwrite(client, camera, services):
    first = upload(client, camera).json()
    repeated = upload(client, camera)
    assert repeated.status_code == 200
    assert repeated.json()['photo_id'] == first['photo_id'] and repeated.json()['duplicate']
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 1
    assert upload(client, camera, content='另一内容').status_code == 409


def test_parallel_retries_create_one_photo(client, camera, services):
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: upload(client, camera), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert len({r.json()['photo_id'] for r in responses}) == 1
    assert services['db'].one('SELECT COUNT(*) n FROM camera_captures')['n'] == 1


def test_deleted_capture_is_not_resurrected(client, camera, services):
    photo_id = upload(client, camera).json()['photo_id']
    # Exercise the real deletion path, including its foreign key behavior.
    response = client.delete('/api/photos/' + photo_id)
    assert response.status_code == 200, response.text
    assert upload(client, camera).status_code == 410
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0


@pytest.mark.parametrize('changes,status', [
    ({'case_number': 'BYF-1-201-02'}, 409),
    ({'photo_group': 'made_up'}, 400),
    ({'captured_at': '2026-01-01T09:41:00'}, 400),
    ({'captured_at': '2099-01-01T09:41:00+08:00'}, 400),
    ({'photo_group': 'work_checkin', 'role': ''}, 400),
    ({'photo_group': 'completion', 'role': 'construction'}, 400),
])
def test_invalid_capture_is_rejected_without_photos(client, camera, services, changes, status):
    response = upload(client, camera, **changes)
    assert response.status_code == status, response.text
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0


def test_checkin_role_is_recorded_and_revocation_blocks_device(client, camera, services):
    response = upload(client, camera, photo_group='work_checkin', role='door_checkin')
    assert response.status_code == 200, response.text
    photo = services['domain'].photo(response.json()['photo_id'])
    assert photo['role'] == 'door_checkin' and photo['photo_group'] == 'work_checkin'
    assert client.delete('/api/camera/admin/devices/' + camera['device']['device_id']).status_code == 200
    assert client.get('/api/camera/cases', headers=camera['headers']).status_code == 401
    assert upload(client, camera).status_code == 401


def test_project_scope_and_expired_pairing(client, camera, services):
    other_project = services['domain'].save_project({'name': '其他项目'})
    other = services['domain'].save_case({'project_id': other_project['id'], 'title': '不应访问',
                                        'platform_report_code': 'OTHER123'})
    assert upload(client, camera, case_id=other['id'], case_number='OTHER123').status_code == 403
    assert all(row['id'] != other['id'] for row in client.get('/api/camera/cases', headers=camera['headers']).json()['items'])
    pair = camera['service'].create_pairing({'name': '过期', 'project_ids': [other_project['id']], 'user_id': camera['device']['user_id']})
    with services['db'].tx() as con:
        con.execute("UPDATE camera_pairings SET expires_at='2000-01-01'")
    assert client.post('/api/camera/pair', json={'code': pair['code']}).status_code == 401


def test_corrupt_jpeg_rejected_and_authenticated_camera_admin_allowed(client, camera, services):
    camera['files']['stamped'] = ('bad.jpg', b'not a jpeg', 'image/jpeg')
    assert upload(client, camera).status_code == 400
    assert client.get('/api/camera/admin', headers={'X-Forwarded-Proto': 'https'}).status_code == 200
    assert client.get('/camera-setup').status_code == 200
