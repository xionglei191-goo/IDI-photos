"""Exercise the production router through camera receipt, save and confirmation."""
import io
import json
from copy import deepcopy
from datetime import datetime, timezone

import httpx
from PIL import Image

from idi.platform_client import PlatformClient
from tests.test_platform_preparation import platform, enter_stage, remote_file
from tests.test_project_access import make_person
from tests.test_automatic_uploads import tick


def test_member_camera_saves_with_project_account_before_manager_formal_submission(
        platform, services, case, project, client, monkeypatch):
    platform.test_survey['buildingFileInfoList'] = []
    enter_stage(platform, case['id'], 'survey')
    company = platform.client
    company._permanently_read_only = True
    accounts = services['project_accounts']
    accounts.company._client = company
    accounts.write_enabled = True
    accounts.client_factory = lambda: PlatformClient(company._http._transport, write_enabled=True)
    platform.project_accounts, platform.client = accounts, accounts.router
    accounts.configure(project['id'], {'username': 'project-manager-website',
        'password': 'isolated-website-password', 'auto_connect': False})
    writer = accounts.account(project['id']).client
    writer._token, writer._account = 'project-session-only', 'project-manager-website'
    calls = []

    def upload(path, name, mime):
        calls.append(('file', writer.status()['account']))
        return remote_file('project-camera-file', name)

    def write(stage, payload):
        calls.append(('business', writer.status()['account'], deepcopy(payload)))
        platform.test_survey.update(deepcopy(payload))
        platform.test_row['siteSurveyState'] = str(payload['state'])
        return {'code': 0}

    monkeypatch.setattr(writer, 'upload', upload)
    monkeypatch.setattr(writer, 'save_draft_only', write)
    monkeypatch.setattr(writer, 'submit', write)
    member = make_person(services, project['id'], 'member', 'crew-leader')
    manager = make_person(services, project['id'], 'manager', 'project-manager')
    camera = services['mobile_camera']
    device = camera.pair(camera.create_pairing({'name': '班组长手机',
        'user_id': member['id'], 'project_ids': [project['id']]})['code'])
    services['official_camera'].settings.save({'enabled': True, 'auto_verify': False,
        'sdk_app_id': 'isolated-sdk', 'sdk_secret_key': 'isolated-sdk-secret'})
    out = io.BytesIO()
    Image.new('RGB', (100, 160), 'navy').save(out, 'JPEG')
    meta = {'schema_version': 1, 'capture_id': '12345678-1234-1234-1234-123456789abc',
        'case_id': case['id'], 'case_number': 'ZLBH20260001', 'project_name': project['name'],
        'location': '1栋 · 201室', 'captured_at': datetime.now(timezone.utc).isoformat(),
        'photo_group': 'survey_checkin', 'role': 'building_checkin', 'part': '楼栋',
        'content': '查勘打卡', 'source': 'xhey_sdk', 'app_version': '0.5.2'}
    response = client.post('/api/camera/official-captures',
        headers={'Authorization': 'Bearer ' + device['token']},
        data={'metadata': json.dumps(meta)}, files={'image': ('official.jpg', out.getvalue(), 'image/jpeg')})
    assert response.status_code == 200, response.text
    photo_id = response.json()['photo_id']
    picture = services['domain'].photo(photo_id)
    assert picture['uploaded_by'] == member['id']
    assert picture['recognition_state'] == 'recognized' and picture['confirmed_fields'] == []
    auto = services['automatic_uploads']
    auto.run_once()
    row = tick(auto, case['id'])
    assert row['state'] == 'awaiting_confirmation', row
    assert len([call for call in calls if call[0] == 'file']) == 1
    assert all(call[1] == 'project-manager-website' for call in calls)
    assert [call[2]['state'] for call in calls if call[0] == 'business'] == [0]
    saved = services['reviews'].get(row['save_review_id'])
    assert saved['state'] == 'save_verified'
    operation = services['db'].one("SELECT * FROM website_operations WHERE operation='automatic_save'")
    assert json.loads(operation['source_users'])[0]['id'] == member['id']
    assert operation['actor_name'] == '系统自动执行'
    review = services['reviews'].get(row['review_id'])
    payload = {'acknowledged': True, 'fingerprint': review['fingerprint']}
    for username, expected in [('crew-leader', 403), ('project-manager', 200)]:
        assert client.post('/api/auth/login', json={'username': username,
            'password': 'person-test-password'}).status_code == 200
        if username == 'project-manager':
            writer._token = ''
            disconnected = client.post(f"/api/workflow/reviews/{review['id']}/confirm", json=payload)
            assert disconnected.status_code == 401, disconnected.text
            assert 'x-idi-auth' not in disconnected.headers
            assert client.get('/api/auth/me').status_code == 200
            writer._token = 'project-session-only'
        response = client.post(f"/api/workflow/reviews/{review['id']}/confirm", json=payload)
        assert response.status_code == expected, response.text
    assert response.json()['state'] == 'verified'
    assert [call[2]['state'] for call in calls if call[0] == 'business'] == [0, 1]
    operation = services['db'].one("SELECT * FROM website_operations WHERE operation='formal_submit'")
    assert operation['actor_id'] == manager['id']
    assert operation['website_account'] == 'project-manager-website'
    assert json.loads(operation['source_users'])[0]['id'] == member['id']
    assert company.status()['account'] == 'test-account'
    # A project readback does not make the company reader lose the ability
    # to download an existing website-only attachment.
    downloads = []
    def download(url, limit, expected_account):
        assert expected_account == company.status()['account'] == 'test-account'
        downloads.append(url)
        return out.getvalue()
    monkeypatch.setattr(company, 'download_attachment', download)
    response = client.get(f"/api/platform/cases/{case['id']}/stages/survey/files/houseFileInfoList/survey-door/original")
    assert response.status_code == 200, response.text
    assert response.content == out.getvalue() and len(downloads) == 1
