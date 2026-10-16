import json
from copy import deepcopy

import httpx
import pytest

from idi.domain import DomainError
from idi.platform_client import PlatformClient, PlatformWriteError
from tests.test_platform_preparation import platform, draft_data, remote_file, enter_stage
from tests.test_workflow_v2 import intake_review


def survey_review(platform, services, case, photo):
    platform.test_survey.update(buildingFileInfoList=[], houseFileInfoList=[], fileInfoList=[])
    enter_stage(platform, case['id'], 'survey')
    pics = [photo(color, name, stage='survey', role=role) for color, name, role in [
        ('red', '楼栋.png', 'building_checkin'), ('blue', '入户.png', 'door_checkin'), ('green', '问题.png', 'problem')]]
    files = dict(zip(('buildingFileInfoList', 'houseFileInfoList', 'fileInfoList'), [[p['id']] for p in pics]))
    plan = platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', files=files))
    assert plan['error_count'] == 0
    batch = services['reviews'].prepare(case['id'], 'survey', plan['draft']['revision'])
    platform.client.write_enabled = True
    return batch, pics


def mock_writes(platform, monkeypatch):
    uploaded, writes = [], []
    def upload(path, name, mime):
        uploaded.append((name, path.read_bytes()))
        return remote_file('uploaded-' + str(len(uploaded)), name)
    def write(stage, payload):
        writes.append(deepcopy(payload))
        platform.test_survey.update(deepcopy(payload))
        platform.test_row['siteSurveyState'] = str(payload['state'])
        return {'code': 0}
    monkeypatch.setattr(platform.client, 'upload', upload)
    monkeypatch.setattr(platform.client, 'submit', write)
    return uploaded, writes, upload, write


def test_save_uses_zero_keeps_stage_then_submit_reuses_remote_files(platform, services, case, photo, monkeypatch, client):
    batch, pics = survey_review(platform, services, case, photo)
    uploaded, writes, _, _ = mock_writes(platform, monkeypatch)
    draft_before = platform.draft(case['id'], 'survey')
    response = client.post(f"/api/workflow/reviews/{batch['id']}/confirm", json={'acknowledged': True, 'fingerprint': batch['fingerprint'], 'action': 'save'})
    assert response.status_code == 200
    result = response.json()
    assert result['state'] == 'save_verified' and not result['active']
    assert result['result']['action'] == 'save' and result['result']['business_submitted'] is False
    assert len(uploaded) == 3 and writes[0]['state'] == 0
    assert services['domain'].case(case['id'])['workflow_stage'] == 'survey'
    assert result['result']['mismatches'] == []
    saved = platform.draft(case['id'], 'survey')
    assert saved['values'] == draft_before['values']
    assert all(ids == [] for ids in saved['files'].values())
    for pic in pics:
        assert services['storage'].verify(services['db'].one('SELECT * FROM blobs WHERE hash=?', (pic['blob_hash'],)))
    reviews = services['reviews']
    assert reviews.confirm(batch['id'], batch['fingerprint'], action='save')['state'] == 'save_verified'
    with pytest.raises(DomainError, match='另一项'):
        reviews.confirm(batch['id'], batch['fingerprint'])
    next_batch = reviews.prepare(case['id'], 'survey', saved['revision'])
    assert reviews.confirm(next_batch['id'], next_batch['fingerprint'])['state'] == 'verified'
    assert len(uploaded) == 3 and len(writes) == 2 and writes[1]['state'] == 1
    assert services['domain'].case(case['id'])['workflow_stage'] == 'repair'


def test_partial_upload_retries_only_remaining_files(platform, services, case, photo, monkeypatch):
    batch, _ = survey_review(platform, services, case, photo)
    uploaded, writes, upload, _ = mock_writes(platform, monkeypatch)
    failed = False
    def interrupted(path, name, mime):
        nonlocal failed
        if len(uploaded) == 1 and not failed:
            failed = True
            raise DomainError('上传网络中断', 502)
        return upload(path, name, mime)
    monkeypatch.setattr(platform.client, 'upload', interrupted)
    reviews = services['reviews']
    with pytest.raises(DomainError, match='中断'):
        reviews.confirm(batch['id'], batch['fingerprint'], action='save')
    partial = reviews.get(batch['id'])
    assert partial['state'] == 'failed' and len(partial['result']['uploaded']) == 1
    assert partial['result']['business_submitted'] is False and not writes
    assert reviews.confirm(batch['id'], batch['fingerprint'], action='save')['state'] == 'save_verified'
    assert len(uploaded) == 3 and len(writes) == 1


def test_unknown_save_requires_readback_and_never_resends(platform, services, case, photo, monkeypatch):
    batch, _ = survey_review(platform, services, case, photo)
    uploaded, writes, _, write = mock_writes(platform, monkeypatch)
    def lost_response(stage, payload):
        write(stage, payload)
        raise DomainError('响应丢失，结果未知', 502)
    monkeypatch.setattr(platform.client, 'submit', lost_response)
    reviews = services['reviews']
    with pytest.raises(DomainError, match='结果未知'):
        reviews.confirm(batch['id'], batch['fingerprint'], action='save')
    assert reviews.get(batch['id'])['state'] == 'uncertain'
    with pytest.raises(DomainError):
        reviews.confirm(batch['id'], batch['fingerprint'], action='save')
    result = reviews.reconcile(batch['id'])
    assert result['state'] == 'save_verified'
    assert services['domain'].case(case['id'])['workflow_stage'] == 'survey'
    assert len(uploaded) == 3 and len(writes) == 1


def test_rejected_write_can_retry_without_uploading_again(platform, services, case, photo, monkeypatch):
    batch, _ = survey_review(platform, services, case, photo)
    uploaded, writes, _, write = mock_writes(platform, monkeypatch)
    def rejected(*_):
        raise PlatformWriteError('官网拒绝：稍后重试', 422, rejected=True)
    monkeypatch.setattr(platform.client, 'submit', rejected)
    reviews = services['reviews']
    with pytest.raises(DomainError, match='拒绝'):
        reviews.confirm(batch['id'], batch['fingerprint'], action='save')
    assert reviews.get(batch['id'])['state'] == 'failed'
    monkeypatch.setattr(platform.client, 'submit', write)
    assert reviews.confirm(batch['id'], batch['fingerprint'], action='save')['state'] == 'save_verified'
    assert len(uploaded) == 3 and len(writes) == 1


def test_save_readback_failure_keeps_confirmed_receipt(platform, services, case, photo, monkeypatch):
    batch, _ = survey_review(platform, services, case, photo)
    uploaded, writes, _, _ = mock_writes(platform, monkeypatch)
    refresh = services['workflow'].refresh_case
    calls = 0
    def failure_on_readback(case_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DomainError('临时查询失败', 502)
        return refresh(case_id)
    monkeypatch.setattr(services['workflow'], 'refresh_case', failure_on_readback)
    reviews = services['reviews']
    result = reviews.confirm(batch['id'], batch['fingerprint'], action='save')
    assert result['state'] == 'saved' and result['result']['operation_confirmed']
    assert '已保存' in result['result']['error']
    assert reviews.reconcile(batch['id'])['state'] == 'save_verified'
    assert len(uploaded) == 3 and len(writes) == 1


def test_reconcile_is_blocked_while_uploading(platform, services, case, photo, monkeypatch):
    batch, _ = survey_review(platform, services, case, photo)
    _, _, upload, _ = mock_writes(platform, monkeypatch)
    def checking(path, name, mime):
        record = services['reviews'].get(batch['id'])
        assert record['active'] and record['result']['phase'] == 'uploading'
        with pytest.raises(DomainError, match='仍在进行'):
            services['reviews'].reconcile(batch['id'])
        return upload(path, name, mime)
    monkeypatch.setattr(platform.client, 'upload', checking)
    assert services['reviews'].confirm(batch['id'], batch['fingerprint'], action='save')['state'] == 'save_verified'


def test_save_rejects_workflow_advance_even_when_fields_match(platform, services, case, photo, monkeypatch):
    batch, _ = survey_review(platform, services, case, photo)
    _, _, _, write = mock_writes(platform, monkeypatch)
    def wrong_state(stage, payload):
        response = write(stage, payload)
        platform.test_row['siteSurveyState'] = '1'
        return response
    monkeypatch.setattr(platform.client, 'submit', wrong_state)
    result = services['reviews'].confirm(batch['id'], batch['fingerprint'], action='save')
    assert result['state'] == 'saved' and 'draft_state' in result['result']['mismatches']


def test_disconnected_or_switched_account_cannot_upload(platform, services, case, photo, monkeypatch):
    batch, _ = survey_review(platform, services, case, photo)
    uploaded, writes, _, _ = mock_writes(platform, monkeypatch)
    platform.client._account = 'different-account'
    with pytest.raises(DomainError, match='账号已变化'):
        services['reviews'].confirm(batch['id'], batch['fingerprint'], action='save')
    platform.client.logout()
    with pytest.raises(DomainError, match='先连接'):
        services['reviews'].confirm(batch['id'], batch['fingerprint'], action='save')
    assert not uploaded and not writes


def test_submit_only_stage_cannot_save_and_acknowledgement_required(platform, services, case, client):
    batch = intake_review(platform, services, case)
    platform.client.write_enabled = True
    route = f"/api/workflow/reviews/{batch['id']}/confirm"
    assert client.post(route, json={'action': 'submit', 'fingerprint': batch['fingerprint']}).status_code == 400
    assert client.post(route, json={'acknowledged': True, 'action': 'save', 'fingerprint': batch['fingerprint']}).status_code == 400
    assert services['reviews'].get(batch['id'])['state'] == 'prepared'


@pytest.mark.parametrize('kind', ['multipart', 'rejection', 'expired', 'unknown'])
def test_write_transport_contract(tmp_path, kind):
    def transport(request):
        assert request.headers['authorization'] == 'Bearer fixture'
        if kind == 'multipart':
            assert request.url.path.endswith('/insurance/sysFileInfo/upload')
            assert 'multipart/form-data' in request.headers['content-type']
            assert b'name="file"' in request.content and b'original-bytes' in request.content
            assert request.extensions['timeout']['read'] == 120
            return httpx.Response(200, json={'code': 0, 'data': [remote_file('upload-1')]})
        if kind == 'rejection':
            return httpx.Response(200, json={'code': 1, 'msg': '现场查勘时间必填'})
        if kind == 'expired':
            return httpx.Response(401, json={'msg': 'expired'})
        raise httpx.ReadTimeout('lost response')
    client = PlatformClient(httpx.MockTransport(transport), write_enabled=True)
    client._token = 'fixture'
    try:
        if kind == 'multipart':
            original = tmp_path / 'original.jpg'
            original.write_bytes(b'original-bytes')
            assert client.upload(original, 'original.jpg', 'image/jpeg')['id'] == 'upload-1'
        else:
            with pytest.raises(PlatformWriteError) as caught:
                client.submit('survey', {'state': 0})
            assert caught.value.rejected == (kind != 'unknown')
            if kind == 'rejection':
                assert '现场查勘时间必填' in str(caught.value)
            if kind == 'expired':
                assert not client.status()['connected']
    finally:
        client.close()
