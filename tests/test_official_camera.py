import base64
import hashlib
import hmac
import io
import json
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest
from PIL import Image

from idi.db import now
from idi.xhey_camera import XheyClient, XheyError, XheySettings, parse_verdict, sdk_signature, signed_request


def video_container(seconds=3, video=True):
    """Small structural MP4 fixture; live SDK acceptance separately checks decoding."""
    def box(kind, payload):
        return struct.pack('>I4s', len(payload) + 8, kind) + payload
    header = bytearray(100)
    struct.pack_into('>II', header, 12, 1000, seconds * 1000)
    track = bytearray(84)
    struct.pack_into('>II', track, 76, 720 << 16, 1280 << 16)
    handler = b'\0' * 8 + (b'vide' if video else b'soun') + b'\0' * 12
    samples = box(b'stsd', b'\0' * 4 + struct.pack('>I', 1) + box(b'avc1', b'\0' * 78))
    media = box(b'mdia', box(b'hdlr', handler) + box(b'minf', box(b'stbl', samples)))
    return (box(b'ftyp', b'isom\0\0\0\0isommp42')
            + box(b'mdat', b'synthetic-container-only')
            + box(b'moov', box(b'mvhd', header) + box(b'trak', box(b'tkhd', track) + media)))


def upload_video(client, fixture, data=None, **changes):
    meta = {**fixture['meta'], 'media_type': 'video', **changes}
    return client.post('/api/camera/official-captures', headers=fixture['headers'],
                       data={'metadata': json.dumps(meta)},
                       files={'video': ('official.mp4', data if data is not None else video_container(), 'video/mp4')})


def test_video_archives_original_once_without_photo_verifier(client, official, services, case):
    original = video_container()
    before = services['domain'].completeness(case['id'])
    result = upload_video(client, official, original)
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert receipt['state'] == 'uploaded' and receipt['media_type'] == 'video'
    assert receipt['verification_supported'] is False and receipt['verification_state'] == 'not_requested'
    assert receipt['original_hash'] == hashlib.sha256(original).hexdigest()
    assert receipt['verified_at'] is None and not official['calls']
    assert not official['service'].process_one()
    photo = services['domain'].photo(receipt['photo_id'])
    assert photo['media_type'] == 'video' and photo['mime_type'] == 'video/mp4'
    assert photo['original_name'].endswith('.mp4') and photo['width'] == 720 and photo['height'] == 1280
    assert photo['watermark_time'] is None and '视频暂不支持本地验真' in photo['notes']
    assert services['domain'].completeness(case['id']) == before
    archived = client.get('/api/photos/' + receipt['photo_id'] + '/original')
    assert archived.status_code == 200 and archived.content == original
    ranged = client.get('/api/photos/' + receipt['photo_id'] + '/original', headers={'Range': 'bytes=0-31'})
    assert ranged.status_code == 206 and ranged.content == original[:32]
    repeated = upload_video(client, official, original).json()
    assert repeated['duplicate'] and repeated['photo_id'] == receipt['photo_id']
    assert upload_video(client, official, video_container(seconds=4)).status_code == 409
    assert upload(client, official).status_code == 409
    assert client.delete('/api/photos/' + receipt['photo_id']).status_code == 200
    assert upload_video(client, official, original).status_code == 410
    assert not official['calls']


@pytest.mark.parametrize('invalid', [b'not-an-mp4', video_container()[:-12],
                                    video_container(seconds=70), video_container(video=False)])
def test_video_rejects_incomplete_oversized_duration_or_audio_only(client, official, services, invalid):
    result = upload_video(client, official, invalid)
    assert result.status_code == 400, result.text
    assert not services['db'].all('SELECT * FROM camera_official_captures')
    assert not services['db'].all('SELECT * FROM photos')
    assert not official['calls']


def test_video_route_enforces_type_and_native_origin(client, official, services):
    assert upload_video(client, official, official['image']).status_code == 400
    assert upload_video(client, official, source='xhey_h5').status_code == 400
    assert upload_video(client, official, media_type='image').status_code == 400
    assert upload(client, official, media_type='video').status_code == 400
    assert not services['db'].all('SELECT * FROM camera_official_captures')


def test_video_respects_case_and_device_scope(client, official, services, case):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='closed' WHERE id=?", (case['id'],))
    assert upload_video(client, official).status_code == 409
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair' WHERE id=?", (case['id'],))
        con.execute("UPDATE camera_devices SET project_ids='[]' WHERE id=?", (official['device']['device_id'],))
    assert upload_video(client, official).status_code == 403
    assert not services['db'].all('SELECT * FROM photos')


@pytest.fixture
def official(services, case, project, admin_user):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair',platform_report_code='ZLBH202610200000000001' WHERE id=?", (case['id'],))
    camera = services['mobile_camera']
    pair = camera.pair(camera.create_pairing({'name': '官方测试手机', 'project_ids': [project['id']], 'user_id': admin_user['id']})['code'])
    out = io.BytesIO()
    Image.new('RGB', (100, 160), 'navy').save(out, 'JPEG')
    meta = {'schema_version': 1, 'capture_id': '12345678-1234-1234-1234-123456789abc', 'case_id': case['id'],
            'case_number': 'ZLBH202610200000000001', 'project_name': project['name'], 'location': '1栋 · 201室',
            'captured_at': datetime.now(timezone.utc).isoformat(), 'photo_group': 'construction',
            'role': 'construction', 'part': '卫生间', 'content': '修补', 'source': 'xhey_sdk', 'app_version': '0.2.0'}
    service = services['official_camera']
    config = {'enabled': True, 'auto_verify': True, 'sdk_app_id': 'sdk-app-test', 'sdk_secret_key': 'sdk-secret-test',
              'group_key': 'verify-group-test', 'group_secret': 'verify-secret-test', 'group_watermark_id': 'template-test'}
    service.settings.save(config)
    calls = []

    def transport(request):
        calls.append(request)
        action = request.url.path.rsplit('/', 1)[-1]
        data = {'create': {'status': 0, 'data': [{'taskID': 'task-test'}]},
                'query': passed(),
                'verify_report': {'status': 0, 'taskID': 'task-test', 'timestamp': 123,
                                  'ossUrl': 'https://net-cloud.xhey.top/authVerify/reports/test.png'}}[action]
        return httpx.Response(200, json={'code': 200, 'data': data})
    service.client_factory = lambda value: XheyClient(value, transport=httpx.MockTransport(transport))
    return {'service': service, 'headers': {'Authorization': 'Bearer ' + pair['token']}, 'device': pair,
            'meta': meta, 'image': out.getvalue(), 'config': config, 'calls': calls}


def passed(**changes):
    return {'status': 0, 'taskStatus': 5, 'list': [{'photoBase64Idx': 0, 'status': 0,
            'antiFakeCode': 'TEST1234', 'photoTime': '2026年10月20日 08:00', **changes}]}


def upload(client, fixture, **changes):
    return client.post('/api/camera/official-captures', headers=fixture['headers'],
                       data={'metadata': json.dumps({**fixture['meta'], **changes})},
                       files={'image': ('official.jpg', fixture['image'], 'image/jpeg')})


def test_sdk_only_settings_default_to_no_local_verification(tmp_path):
    settings = XheySettings(tmp_path)
    status = settings.save({'enabled': True, 'sdk_app_id': 'test-app', 'sdk_secret_key': 'test-secret'})
    assert status['ready'] and not status['auto_verify'] and not status['verification_ready']
    assert settings.h5_status()['ready']
    assert settings.h5_signature()[0]['signature']
    from idi.domain import DomainError
    with pytest.raises(DomainError, match='Group Key'):
        settings.save({'auto_verify': True})
    assert settings.status()['auto_verify'] is False
    with pytest.raises(DomainError):
        settings.save({'auto_verify': 'false'})


def test_local_archive_defaults_off_for_existing_config_and_never_calls_vendor(client, official, services, case):
    # Upgrade from 0.3.0: an absent flag must not silently keep extra verification enabled.
    from idi.connectors.secret_store import protect
    value = {k: v for k, v in official['config'].items() if k != 'auto_verify'}
    official['service'].settings.path.write_bytes(protect(json.dumps(value).encode()))
    before = services['domain'].case(case['id'])
    changes = {'source': 'xhey_h5', 'sdk_photo_time': '2026:10:20 10:11:12',
               'sdk_anti_fake_code': 'REPORTED-NOT-VERIFIED', 'captured_at': '2099-01-01T01:00:00+08:00'}
    response = upload(client, official, **changes)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['state'] == 'uploaded' and receipt['verification_state'] == 'not_requested'
    assert receipt['archived_at'] and receipt['verified_at'] is None and receipt['verification'] == {}
    assert receipt['report'] == {} and not official['calls']
    assert not official['service'].process_one()
    assert services['domain'].case(case['id']) == before
    photo = services['domain'].photo(receipt['photo_id'])
    assert photo['watermark_time'] == '2026-10-20 10:11:12'
    assert '未做本地验真' in photo['notes'] and '验真通过' not in photo['notes']
    source = json.loads(services['db'].one('SELECT metadata FROM photo_sources')['metadata'])
    assert source['time_basis'] == 'sdk_returned_unverified'
    assert source['verification_state'] == 'not_requested' and source['verified_at'] is None
    assert source['verification_task_id'] is None and source['verification'] == {}
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (receipt['original_hash'],))
    assert services['storage'].resolve(blob['stored_path']).read_bytes() == official['image']
    assert upload(client, official, **changes).json()['duplicate']
    assert upload(client, official, **{**changes, 'sdk_photo_time': '2026:10:20 10:11:13'}).status_code == 409
    assert client.post(f"/api/camera/admin/official/{official['device']['device_id']}/{official['meta']['capture_id']}/report").status_code == 409
    assert client.delete('/api/photos/' + receipt['photo_id']).status_code == 200
    assert upload(client, official, **changes).status_code == 410
    assert not official['calls']


def test_local_archive_missing_sdk_time_does_not_promote_phone_clock(client, official, services):
    official['service'].settings.save({'auto_verify': False})
    result = upload(client, official, captured_at='2099-01-01T01:00:00+08:00', sdk_photo_time='invalid')
    assert result.status_code == 200
    photo = services['domain'].photo(result.json()['photo_id'])
    assert photo['watermark_time'] is None
    source = json.loads(services['db'].one('SELECT metadata FROM photo_sources')['metadata'])
    assert source['time_basis'] == 'unavailable' and not official['calls']


def test_local_archive_parallel_delivery_is_once_and_closed_cases_stay_blocked(client, official, services, case):
    official['service'].settings.save({'auto_verify': False})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: upload(client, official), range(2)))
    assert all(r.status_code == 200 and r.json()['state'] == 'uploaded' for r in results)
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 1
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='closed' WHERE id=?", (case['id'],))
    assert upload(client, official).json()['duplicate']
    assert upload(client, official, capture_id='12345678-1234-1234-1234-123456789ddd').status_code == 409
    assert not official['calls']


def test_enabling_verification_does_not_backfill_local_archive(client, official, services):
    service = official['service']
    service.settings.save({'auto_verify': False})
    first = upload(client, official).json()
    service.settings.save({'auto_verify': True})
    assert not service.process_one() and not official['calls']
    out = io.BytesIO()
    Image.new('RGB', (100, 160), 'green').save(out, 'JPEG')
    second = {**official, 'image': out.getvalue()}
    response = upload(client, second, capture_id='12345678-1234-1234-1234-123456789ddd')
    assert response.json()['verification_state'] == 'queued'
    assert service.process_one() and service.process_one()
    assert len(official['calls']) == 2
    first_receipt = client.get('/api/camera/official-captures/' + official['meta']['capture_id'],
                               headers=official['headers']).json()
    assert first_receipt['verification_state'] == 'not_requested'
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 2


def test_disabling_optional_verification_pauses_existing_task(client, official):
    service = official['service']
    upload(client, official)
    service.settings.save({'auto_verify': False})
    assert not service.process_one() and not official['calls']
    service.settings.save({'auto_verify': True})
    assert service.process_one() and len(official['calls']) == 1
    service.settings.save({'auto_verify': False})
    assert not service.process_one() and len(official['calls']) == 1


def test_local_archive_failure_rolls_back_capture_and_photo(client, official, services, monkeypatch):
    from idi.domain import DomainError
    service = official['service']
    service.settings.save({'auto_verify': False})
    original_check = service.check_case
    checks = 0
    def changed_case(*args):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise DomainError('案件在归档前已改变', 409)
        return original_check(*args)
    monkeypatch.setattr(service, 'check_case', changed_case)
    assert upload(client, official).status_code == 409
    assert services['db'].one('SELECT COUNT(*) n FROM camera_official_captures')['n'] == 0
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    assert not official['calls']


def test_signs_exact_utf8_body():
    body, headers = signed_request('g', 'secret', {'photoBase64List': ['abc'], '字': '值'}, 123)
    assert body == '{"photoBase64List":["abc"],"字":"值"}'.encode()
    first = base64.b64encode(hmac.digest(b'secret', body, 'sha256')).decode()
    expected = base64.b64encode(hmac.digest(b'secret', f'groupKey=g&sign={first}&timestamp=123'.encode(), 'sha256')).decode()
    assert headers['Signature'] == expected and headers['Timestamp'] == '123'


def test_h5_signature_matches_published_vendor_example():
    assert sdk_signature('1ecfaaa70d389eb87731e41036560282', {
        'timestamp': 1736919106, 'noncestr': 'U5YJHgUFrN', 'appid': '123455',
    }) == 'StILozltCtcp28CQZbQX1myPwwjk4626aRt3/83R1dQ='


def h5_metadata(official, services):
    case = services['domain'].case(official['meta']['case_id'])
    from idi.mobile_camera import location
    return {**official['meta'], 'source': 'xhey_h5', 'project_name': case['project_name'], 'location': location(case)}


def test_h5_signing_is_scoped_fresh_and_does_not_release_secrets(client, official, services):
    meta = h5_metadata(official, services)
    assert client.get('/api/camera/h5-config').status_code == 401
    assert client.post('/api/camera/h5-session', json=meta).status_code == 401
    status = client.get('/api/camera/h5-config', headers=official['headers'])
    assert status.json()['ready'] and status.json()['mode'] == 'h5'
    first = client.post('/api/camera/h5-session', headers=official['headers'], json=meta)
    second = client.post('/api/camera/h5-session', headers=official['headers'], json=meta)
    assert first.status_code == second.status_code == 200
    assert first.headers['cache-control'] == 'no-store'
    options = first.json()['options']
    assert options['noncestr'] != second.json()['options']['noncestr']
    assert options['signature'] == sdk_signature(official['config']['sdk_secret_key'], {
        key: options[key] for key in ('appid', 'noncestr', 'timestamp')})
    assert options['customInputItems']['案件号'] == meta['case_number']
    assert list(options['customInputItems']) == ['案件号', '小区', '位置', '环节', '内容']
    assert '时间' not in options['customInputItems']
    assert options['watermarkId'] == 'template-test' and not options['enableCustomWatermark']
    assert options['enableOssUpload'] is False and options['enableWorkgroupUpload'] is False
    assert options['watermarkScale'] == 0.68
    for key in ('sdk_secret_key', 'group_key', 'group_secret'):
        assert official['config'][key] not in first.text + status.text
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    assert not official['calls']  # Signature generation is local and does not create a billable task.


def test_h5_default_watermark_does_not_require_team_template(client, official, services):
    value = dict(official['config'])
    value.pop('group_watermark_id')
    from idi.connectors.secret_store import protect
    official['service'].settings.path.write_bytes(protect(json.dumps(value).encode()))
    assert official['service'].settings.ready(value)
    response = client.post('/api/camera/h5-session', headers=official['headers'], json=h5_metadata(official, services))
    assert response.status_code == 200
    assert not response.json()['has_team_watermark']
    assert response.json()['options']['enableCustomWatermark']
    assert 'watermarkId' not in response.json()['options']


def test_native_signing_uses_new_contract_without_credentials_or_verification(client, official, services):
    official['service'].settings.save({'auto_verify': False})
    meta = {**h5_metadata(official, services), 'source': 'xhey_sdk'}
    assert client.get('/api/camera/native-config').status_code == 401
    assert client.post('/api/camera/native-session', json=meta).status_code == 401
    config = client.get('/api/camera/native-config', headers=official['headers'])
    assert config.json()['mode'] == 'native' and config.json()['ready']
    assert config.json()['auto_verify'] is False
    first = client.post('/api/camera/native-session', headers=official['headers'], json=meta)
    second = client.post('/api/camera/native-session', headers=official['headers'], json=meta)
    assert first.status_code == second.status_code == 200
    assert first.headers['cache-control'] == 'no-store'
    assert first.json()['capture_id'] == meta['capture_id']
    assert first.json()['sdk_version'] == '1.3.70.9b61b4a6'
    options = first.json()['options']
    assert options['noncestr'] != second.json()['options']['noncestr']
    assert options['signature'] == sdk_signature(official['config']['sdk_secret_key'], {
        key: options[key] for key in ('appid', 'noncestr', 'timestamp')})
    assert options['maxImageCount'] == 1 and options['needPhotoConfirm'] is True
    assert options['groupWatermarkId'] == 'template-test'
    assert list(options['customInputItems']) == ['案件号', '小区', '位置', '环节', '内容']
    assert options['customInputItems']['案件号'] == meta['case_number']
    assert '时间' not in options['customInputItems'] and '地点' not in options['customInputItems']
    for key in ('sdk_secret_key', 'group_key', 'group_secret'):
        assert official['config'][key] not in first.text + config.text
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    assert not official['calls']


def test_native_session_validates_case_role_and_device_scope(client, official, services, case):
    meta = {**h5_metadata(official, services), 'source': 'xhey_sdk'}
    for change in ({'case_number': 'WRONG'}, {'project_name': '旧小区'}, {'location': '旧位置'}):
        response = client.post('/api/camera/native-session', headers=official['headers'], json={**meta, **change})
        assert response.status_code == 409
    assert client.post('/api/camera/native-session', headers=official['headers'], json={**meta, 'source': 'xhey_h5'}).status_code == 400
    assert client.post('/api/camera/native-session', headers=official['headers'], json={**meta, 'photo_group': 'work_checkin', 'role': ''}).status_code == 400
    assert client.post('/api/camera/native-session', headers=official['headers'], json={**meta, 'role': 'building_checkin'}).status_code == 400
    official['service'].settings.save({'enabled': False})
    assert client.post('/api/camera/native-session', headers=official['headers'], json=meta).status_code == 409
    official['service'].settings.save({'enabled': True})
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='closed' WHERE id=?", (case['id'],))
    assert client.post('/api/camera/native-session', headers=official['headers'], json=meta).status_code == 409
    with services['db'].tx() as con:
        con.execute("UPDATE camera_devices SET project_ids='[]' WHERE id=?", (official['device']['device_id'],))
    assert client.post('/api/camera/native-session', headers=official['headers'], json=meta).status_code == 403


def test_h5_rejects_disabled_stale_closed_or_out_of_scope_case(client, official, services, case):
    meta = h5_metadata(official, services)
    for change in ({'case_number': 'WRONG'}, {'project_name': '旧小区'}, {'location': '旧位置'}):
        assert client.post('/api/camera/h5-session', headers=official['headers'], json={**meta, **change}).status_code == 409
    assert client.post('/api/camera/h5-session', headers=official['headers'], json={**meta, 'source': 'xhey_sdk'}).status_code == 400
    official['service'].settings.save({'enabled': False})
    assert client.post('/api/camera/h5-session', headers=official['headers'], json=meta).status_code == 409
    official['service'].settings.save({'enabled': True})
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='closed' WHERE id=?", (case['id'],))
    assert client.post('/api/camera/h5-session', headers=official['headers'], json=meta).status_code == 409
    with services['db'].tx() as con:
        con.execute("UPDATE camera_devices SET project_ids='[]' WHERE id=?", (official['device']['device_id'],))
    assert client.post('/api/camera/h5-session', headers=official['headers'], json=meta).status_code == 403


def test_h5_original_uses_same_quarantine_and_verification(client, official, services):
    received = upload(client, official, source='xhey_h5')
    assert received.status_code == 200 and received.json()['verification_state'] == 'queued'
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    assert official['service'].process_one() and official['service'].process_one()
    assert upload(client, official, source='xhey_h5').json()['verification_state'] == 'verified'
    assert json.loads(services['db'].one('SELECT metadata FROM photo_sources')['metadata'])['source'] == 'xhey_h5'


def test_quarantine_then_verify_exact_bytes_and_idempotent_archive(client, official, services, case):
    before = services['domain'].case(case['id'])
    received = upload(client, official)
    assert received.status_code == 200, received.text
    assert received.json()['state'] == 'received' and received.json()['photo_id'] is None
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    service = official['service']
    assert service.process_one()  # create
    assert service.process_one()  # query + archive
    result = upload(client, official).json()
    assert result['verification_state'] == 'verified' and result['state'] == 'uploaded' and result['duplicate']
    photo = services['domain'].photo(result['photo_id'])
    assert photo['watermark_time'] == '2026-10-20 08:00:00'
    assert photo['recognition_state'] == 'recognized'
    assert services['domain'].case(case['id']) == before
    assert result['original_hash'] == hashlib.sha256(official['image']).hexdigest()
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (result['original_hash'],))
    assert services['storage'].resolve(blob['stored_path']).read_bytes() == official['image']
    payload = json.loads(official['calls'][0].content)
    assert base64.b64decode(payload['photoBase64List'][0]) == official['image']
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 1
    assert upload(client, official, content='changed').status_code == 409
    source = json.loads(services['db'].one('SELECT metadata FROM photo_sources')['metadata'])
    assert source['time_basis'] == 'xhey_verification'
    assert source['case_fields_basis'] == 'user_selected_not_verified_by_xhey'


def test_parallel_ingest_has_one_record(client, official, services):
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: upload(client, official), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert services['db'].one('SELECT COUNT(*) n FROM camera_official_captures')['n'] == 1


@pytest.mark.parametrize('change', [
    {'photoBase64Idx': 1}, {'photoBase64Idx': True}, {'status': None}, {'status': False},
    {'antiFakeCode': ''}, {'photoTime': ''}, {'status': -1001}, {'status': -2300}, {'status': 999},
])
def test_incomplete_or_error_verdict_never_passes(change):
    with pytest.raises(XheyError):
        parse_verdict(passed(**change))


@pytest.mark.parametrize('value', [{'status': 0}, {'status': 0, 'taskStatus': 5, 'list': []},
                                 {'status': 0, 'taskStatus': 5, 'list': passed()['list'] * 2},
                                 {'status': 0, 'taskStatus': 99}, {'status': False, 'taskStatus': 5}])
def test_task_contract_is_fail_closed(value):
    with pytest.raises(XheyError):
        parse_verdict(value)


def test_unconfigured_does_not_call_vendor(client, official, services):
    official['service'].settings.save({'enabled': False})
    assert upload(client, official).json()['verification_state'] == 'unconfigured'
    assert not official['service'].process_one() and not official['calls']
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    assert client.get('/api/camera/sdk-config', headers=official['headers']).json()['ready'] is False


def test_secrets_encrypted_and_split_access(client, official):
    config = official['config']
    path = official['service'].settings.path
    assert config['group_secret'].encode() not in path.read_bytes()
    admin = client.get('/api/camera/admin/official')
    assert config['group_secret'] not in admin.text and config['sdk_secret_key'] not in admin.text
    assert client.get('/api/camera/sdk-config').status_code == 401
    sdk = client.get('/api/camera/sdk-config', headers=official['headers'])
    assert sdk.json()['ready'] is False
    for key in ('sdk_secret_key', 'group_secret', 'group_key'):
        assert config[key] not in sdk.text
    assert client.put('/api/camera/admin/official/config', json={'enabled': False}, headers={'X-Forwarded-Proto': 'https'}).status_code == 200
    saved = client.put('/api/camera/admin/official/config', json={'group_secret': '', 'sdk_secret_key': ''})
    assert saved.status_code == 200 and official['service'].settings.load()['group_secret'] == config['group_secret']


def test_create_timeout_requires_manual_retry_and_no_auto_billing(client, official, services):
    upload(client, official)
    def fail(request):
        raise httpx.ReadTimeout('do not expose private data')
    official['service'].client_factory = lambda c: XheyClient(c, transport=httpx.MockTransport(fail))
    assert official['service'].process_one()
    assert not official['service'].process_one()
    row = services['db'].one('SELECT * FROM camera_official_captures')
    assert row['state'] == 'error' and row['task_id'] is None and 'private' not in row['error']
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0


def test_pending_rejection_and_query_retry_reuse_task(client, official, services):
    upload(client, official)
    service = official['service']
    service.process_one()
    def error_query(request):
        return httpx.Response(200, json={'code': 200, 'data': passed(status=-1002)})
    service.client_factory = lambda c: XheyClient(c, transport=httpx.MockTransport(error_query))
    service.process_one()
    row = services['db'].one('SELECT * FROM camera_official_captures')
    assert row['state'] == 'error' and row['task_id'] == 'task-test'
    service.retry(row['device_id'], row['capture_id'])
    def rejected(request):
        assert request.url.path.endswith('/query')
        return httpx.Response(200, json={'code': 200, 'data': passed(status=-2305)})
    service.client_factory = lambda c: XheyClient(c, transport=httpx.MockTransport(rejected))
    service.process_one()
    assert services['db'].one('SELECT state FROM camera_official_captures')['state'] == 'rejected'
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0


def test_deletion_tombstone_and_report(client, official, services):
    upload(client, official)
    service = official['service']
    service.process_one()
    service.process_one()
    result = upload(client, official).json()
    report = service.report(official['device']['device_id'], official['meta']['capture_id'])
    assert report['url'].startswith('https://net-cloud.xhey.top/authVerify/reports/')
    assert client.delete('/api/photos/' + result['photo_id']).status_code == 200
    assert upload(client, official).status_code == 410
    assert client.get('/api/camera/admin/official').status_code == 200
    assert not service.process_one()


def test_revocation_and_case_changes_prevent_archive(client, official, services, case):
    upload(client, official)
    official['service'].process_one()
    client.delete('/api/camera/admin/devices/' + official['device']['device_id'])
    official['service'].process_one()
    assert services['db'].one('SELECT state FROM camera_official_captures')['state'] == 'error'
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    assert client.get('/api/camera/official-captures/' + official['meta']['capture_id'], headers=official['headers']).status_code == 401


def test_new_legacy_self_rendered_uploads_remain_conflicts(client, official, services):
    meta = dict(official['meta'])
    del meta['source']
    image = ('image.jpg', official['image'], 'image/jpeg')
    result = client.post('/api/camera/captures', headers=official['headers'], data={'metadata': json.dumps(meta)},
                         files={'original': image, 'stamped': image})
    assert result.status_code == 200
    photo = services['domain'].photo(result.json()['photo_id'])
    assert photo['recognition_state'] == 'conflict' and '未经今日官方验真' in photo['notes']
    assert upload(client, official).status_code == 409


def test_pending_and_changed_verification_account_do_not_archive(client, official, services):
    upload(client, official)
    service = official['service']
    service.process_one()
    def pending(request):
        return httpx.Response(200, json={'code': 200, 'data': {'status': 0, 'taskStatus': 2}})
    service.client_factory = lambda c: XheyClient(c, transport=httpx.MockTransport(pending))
    service.process_one()
    row = services['db'].one('SELECT * FROM camera_official_captures')
    assert row['state'] == 'processing' and row['attempts'] == 1 and row['next_attempt_at'] > now()
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    service.settings.save({'group_key': 'different-account'})
    service.update(row, next_attempt_at=now())
    service.process_one()
    row = service.row(row['device_id'], row['capture_id'])
    assert row['state'] == 'error' and '账号已改变' in row['error']
    assert row['task_id'] == 'task-test'


def test_crash_during_create_is_not_replayed(client, official, services):
    upload(client, official)
    with services['db'].tx() as con:
        con.execute("UPDATE camera_official_captures SET state='submitting'")
    official['service'].start()
    official['service'].stop()
    assert services['db'].one('SELECT state FROM camera_official_captures')['state'] == 'error'
    assert not official['calls']


def test_hash_change_after_create_cannot_archive(client, official, services):
    received = upload(client, official).json()
    official['service'].process_one()
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (received['original_hash'],))
    services['storage'].resolve(blob['stored_path']).write_bytes(b'corrupted-local-copy')
    official['service'].process_one()
    row = services['db'].one('SELECT * FROM camera_official_captures')
    assert row['state'] == 'error' and '哈希' in row['error']
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0


def test_phone_clock_cannot_override_official_time(client, official, services):
    result = upload(client, official, captured_at='2099-01-01T01:00:00+08:00')
    assert result.status_code == 200
    official['service'].process_one()
    official['service'].process_one()
    photo = services['db'].one('SELECT watermark_time FROM photos')
    assert photo['watermark_time'] == '2026-10-20 08:00:00'


def test_receipt_retry_survives_later_case_closure(client, official, services, case):
    upload(client, official)
    official['service'].process_one()
    official['service'].process_one()
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='closed' WHERE id=?", (case['id'],))
    result = upload(client, official)
    assert result.status_code == 200 and result.json()['duplicate']
    assert result.json()['state'] == 'uploaded'
    assert upload(client, official, capture_id='12345678-1234-1234-1234-123456789ddd').status_code == 409


@pytest.mark.parametrize('body', [{'code': 200, 'data': {'status': 1, 'msg': 'verify-secret-test'}},
                                {'code': 400, 'data': {}, 'msg': 'verify-secret-test'}])
def test_http_success_is_not_verification_success(body, official):
    client = XheyClient(official['config'], transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(XheyError) as error:
        client.create(official['image'])
    assert 'verify-secret-test' not in str(error.value)
