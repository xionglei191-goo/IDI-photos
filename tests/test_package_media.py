import hashlib
import zipfile

import pytest
from fastapi.testclient import TestClient

from idi.domain import DomainError
from tests.conftest import image_bytes
from tests.test_platform_preparation import platform, remote_file, enter_stage


@pytest.fixture
def remote(platform, monkeypatch):
    colors = {'building': 'red', 'door': 'green', 'defect': 'blue', 'finished': 'yellow'}
    files = {key: {**remote_file(key, '同名照片.png'), 'fileUrl': f'/profile/{key}.png'} for key in colors}
    platform.test_survey.update(buildingFileInfoList=[files['building']], houseFileInfoList=[files['door']], fileInfoList=[files['defect']])
    platform.test_repair.update(preRepairFileInfo=[files['defect']], recoveredFileInfo=[files['finished']])
    downloads = []
    def download(url, *args):
        key = url.rsplit('/', 1)[-1].split('.')[0]
        downloads.append(key)
        return image_bytes(colors[key])
    monkeypatch.setattr(platform.client, 'download_attachment', download)
    platform.downloads, platform.remote_files = downloads, files
    return platform


def generate(client, services, case_id, **options):
    response = client.post('/api/packages', json={'case_id': case_id, **options})
    assert response.status_code == 200, response.text
    while services['jobs'].run_one():
        pass
    return services['jobs'].get(response.json()['id'])


def package(services, job):
    assert job['state'] == 'completed', job
    return services['reporter'].package(job['result']['packages'][0]['id'])


def test_download_all_official_photos_with_zero_local_and_reuse_cache(client, services, remote, case):
    result = package(services, generate(client, services, case['id']))
    photos = result['manifest']['photos']
    assert len(photos) == 4  # survey/repair defect is the same original/use
    assert set(remote.downloads) == {'building', 'door', 'defect', 'finished'}
    assert services['domain'].photos(case_id=case['id'])['total'] == 0
    assert {p['photo_group'] for p in photos} == {'survey_checkin', 'survey_problem', 'completion'}
    with zipfile.ZipFile(services['storage'].resolve(result['zip_path'])) as bundle:
        files = [n for n in bundle.namelist() if not n.endswith('/')]
        assert len(files) == len(set(files)) == 4  # same names must not overwrite
        hashes = {hashlib.sha256(bundle.read(n)).hexdigest() for n in files}
        assert hashes == {p['blob_hash'] for p in photos}
    before = list(remote.downloads)
    second = package(services, generate(client, services, case['id']))
    assert second['id'] == result['id'] and remote.downloads == before
    assert services['reporter'].packages(case['id'])[0]['photo_count'] == 4


def test_missing_local_blob_restored_and_same_content_not_duplicated(client, services, remote, case, photo):
    local = photo('blue', stage='survey', role='problem')
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (local['blob_hash'],))
    services['storage'].resolve(blob['stored_path']).unlink()
    result = package(services, generate(client, services, case['id']))
    assert services['storage'].verify(blob)
    assert len(result['manifest']['photos']) == 4
    assert sum(p['blob_hash'] == local['blob_hash'] for p in result['manifest']['photos']) == 1
    assert local['id'] in {p['id'] for p in result['manifest']['photos']}


def test_one_download_failure_creates_no_complete_package_and_retry_uses_cached_files(client, services, remote, case, monkeypatch):
    download = remote.client.download_attachment
    def fail(url, *args):
        if url.endswith('/finished.png'):
            raise DomainError('测试网络中断')
        return download(url, *args)
    monkeypatch.setattr(remote.client, 'download_attachment', fail)
    failed = generate(client, services, case['id'])
    assert failed['state'] == 'failed' and '完工照片' in failed['error'] and '未补齐' in failed['error']
    assert not services['reporter'].packages(case['id'])
    monkeypatch.setattr(remote.client, 'download_attachment', download)
    services['jobs'].retry(failed['id'])
    services['jobs'].run_one()
    result = package(services, services['jobs'].get(failed['id']))
    assert len(result['manifest']['photos']) == 4
    assert len(remote.downloads) == 4


def test_new_official_photo_prevents_reusing_old_incomplete_package(client, services, remote, case):
    first = package(services, generate(client, services, case['id']))
    before = services['storage'].resolve(first['zip_path']).read_bytes()
    remote.test_repair['buildingFileInfoList'] = [remote.remote_files['building']]
    second = package(services, generate(client, services, case['id']))
    assert second['version'] == 2 and len(second['manifest']['photos']) == 5
    assert services['storage'].resolve(first['zip_path']).read_bytes() == before
    assert len(remote.downloads) == 4  # a shared check-in has two uses, one original


def test_unread_field_blocks_and_documents_and_invalid_photos_are_excluded(client, services, remote, case):
    remote.test_survey.pop('houseFileInfoList')
    failed = generate(client, services, case['id'])
    assert failed['state'] == 'failed' and '入户' in failed['error']
    assert not remote.downloads
    remote.test_survey['houseFileInfoList'] = []
    remote.test_repair['recoveredFileInfo'] += [{**remote_file('pdf', '验收.pdf'), 'fileUrl': '/profile/acceptance.pdf'}, {**remote_file('invalid'), 'invalid': True}]
    result = package(services, generate(client, services, case['id']))
    assert len(result['manifest']['photos']) == 3 and len(remote.downloads) == 3


def test_picc_includes_official_photos_and_honors_hash_exclusions(client, services, remote, case):
    # Word excludes video; its unavailable bytes must not block a photo record.
    remote.test_repair['recoveredFileInfo'].append({**remote_file('video', '视频.mp4'), 'fileUrl': '/profile/video.mp4'})
    first = package(services, generate(client, services, case['id'], template_id='picc-record-v1'))
    from docx import Document
    assert len(Document(services['storage'].resolve(first['zip_path'])).inline_shapes) == 4
    plan = services['reporter'].picc.get(case['id'])
    assert '/api/platform/' in plan['photos'][0]['original_url']
    response = client.get(plan['photos'][0]['original_url'])
    assert response.status_code == 200
    services['reporter'].picc.save(case['id'], {**plan, 'photo_action': {'id': plan['photos'][0]['id'], 'excluded': True}})
    second = package(services, generate(client, services, case['id'], template_id='picc-record-v1'))
    assert len(second['manifest']['photos']) == 3
    assert len(remote.downloads) == 4


def test_local_upload_receipt_avoids_refetching_verified_original(client, services, remote, case, photo):
    from idi.db import dump, now
    local = photo('blue', stage='survey', role='problem')
    plan = {'link': {'report_receiving_id': remote.link(case['id'])['report_receiving_id']},
            'file_fields': [{'selected': [local]}]}
    result = {'uploaded': {local['id']: remote.remote_files['defect']}}
    # Only the verified receipt's exact file ID and URL are identity evidence.
    values = {'id': 'receipt-check', 'case_id': case['id'], 'stage': 'survey', 'fingerprint': 'test-receipt',
              'state': 'save_verified', 'snapshot': dump(plan), 'result': dump(result), 'created_at': now()}
    with services['db'].tx() as con:
        con.execute('INSERT INTO platform_review_batches(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
    generated = package(services, generate(client, services, case['id']))
    assert 'defect' not in remote.downloads
    assert len(generated['manifest']['photos']) == 4


def test_default_package_remote_only_failure_does_not_spin(client, services, remote, case, monkeypatch):
    from tests.test_default_packages import verified_submission
    remote.test_row['repairAcceptanceState'] = '1'
    enter_stage(remote, case['id'], 'acceptance')
    verified_submission(services, case['id'])
    monkeypatch.setattr(remote.client, 'download_attachment', lambda *args: (_ for _ in ()).throw(DomainError('测试断网')))
    auto = services['default_packages']
    auto.run_once()
    assert auto.get(case['id'])['state'] == 'preparing'
    services['jobs'].run_one()
    for _ in range(3):
        auto.run_once()
    assert auto.get(case['id'])['state'] == 'failed'
    assert len(services['jobs'].list()) == 1


def test_case_export_requires_project_manager(app, services, remote, case):
    user = services['access'].save_user({'username': 'package-worker', 'display_name': '项目人员', 'password': 'person-test-password', 'system_role': 'user'})
    with TestClient(app, headers={'X-IDI-Request': '1'}) as client:
        client.post('/api/auth/login', json={'username': 'package-worker', 'password': 'person-test-password'})
        for role, expected in [('member', 403), ('manager', 200)]:
            services['access'].save_members(case['project_id'], {'members': [{'user_id': user['id'], 'role': role}]})
            response = client.post('/api/packages', json={'case_id': case['id']})
            assert response.status_code == expected, response.text
        services['access'].save_members(case['project_id'], {'members': []})
        services['jobs'].run_one()
        job = services['db'].one('SELECT state,error FROM jobs')
        assert job['state'] == 'failed' and not remote.downloads


def test_official_download_uses_network_lane_without_blocking_local_imports(client, services, remote, case):
    response = client.post('/api/packages', json={'case_id': case['id']})
    assert response.status_code == 200 and response.json()['payload']['check_official_media'] is True
    assert not services['jobs'].run_one(lane='local')
    assert not remote.downloads
    assert services['jobs'].run_one(lane='network')
    assert services['jobs'].get(response.json()['id'])['state'] == 'completed'


def test_cached_default_unblocks_on_login_and_tracks_official_inventory(services, remote, case):
    from tests.test_default_packages import verified_submission
    remote.test_row['repairAcceptanceState'] = '1'
    enter_stage(remote, case['id'], 'acceptance')
    verified_submission(services, case['id'])
    remote.client._token = None
    auto = services['default_packages']
    for _ in range(3):
        auto.run_once()
    assert auto.get(case['id'])['state'] == 'blocked'
    assert not services['jobs'].list()
    remote.client._token = 'only-a-test-token'
    auto.run_once()
    assert auto.get(case['id'])['state'] == 'preparing'
    while services['jobs'].run_one():
        pass
    auto.run_once()
    first = auto.get(case['id'])['package']
    assert first and first['version'] == 1
    auto.run_once()
    remote.test_repair['buildingFileInfoList'] = [remote.remote_files['building']]
    enter_stage(remote, case['id'], 'acceptance')
    auto.run_once()
    assert auto.get(case['id'])['package'] == first
    auto.retry(case['id'])
    while services['jobs'].run_one():
        pass
    auto.run_once()
    second = auto.get(case['id'])['package']
    assert second['version'] == 2
    assert len(services['reporter'].package(second['id'])['manifest']['photos']) == 5
    assert len(remote.downloads) == 4


def test_historical_remote_only_case_generates_only_on_manual_request(client, services, remote, case):
    remote.test_row['repairAcceptanceState'] = '1'
    enter_stage(remote, case['id'], 'closed')
    auto = services['default_packages']
    auto.run_once()
    status = client.get(f"/api/cases/{case['id']}/default-package").json()
    assert status['state'] == 'manual' and status['eligible']
    assert not remote.downloads and not services['jobs'].list()
    response = client.post(f"/api/cases/{case['id']}/default-package")
    assert response.status_code == 200 and response.json()['state'] == 'preparing'
    while services['jobs'].run_one():
        pass
    auto.run_once()
    first = auto.get(case['id'])['package']
    assert first and len(remote.downloads) == 4
    remote.test_repair['buildingFileInfoList'] = [remote.remote_files['building']]
    enter_stage(remote, case['id'], 'closed')
    auto.run_once()
    assert auto.get(case['id'])['package'] == first
    assert len(services['jobs'].list()) == 1
