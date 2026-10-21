from copy import deepcopy

import pytest

from idi.domain import DomainError
from idi.draft_materials import prepare_materials
from tests.conftest import image_bytes
from tests.test_platform_preparation import draft_data, enter_stage, platform, remote_file
from tests.test_platform_writes import mock_writes, survey_review
from tests.test_project_access import login_person, make_person


def files(plan, key='fileInfoList'):
    return next(field for field in plan['file_fields'] if field['key'] == key)


@pytest.mark.parametrize('deletion', ['absent', 'invalid'])
def test_refresh_reconciles_deleted_files_keeps_local_original_and_draft(platform, services, case, photo, client, monkeypatch, deletion):
    removed = remote_file('removed', '官网后来删除.png')
    platform.test_survey['fileInfoList'] = [remote_file('defect'), removed]
    enter_stage(platform, case['id'], 'survey')
    local = photo('green', '尚未提交.png', stage='survey', role='problem')
    platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', files={'fileInfoList': [local['id']]}))
    before_draft = deepcopy(platform.draft(case['id'], 'survey'))
    monkeypatch.setattr(platform.client, 'download_attachment', lambda *_: image_bytes('orange'))
    blob, _ = platform.media.ensure(platform.preview(case['id'], 'survey'), 'fileInfoList', 'removed')
    platform.test_survey['fileInfoList'] = [remote_file('defect')] + ([{**removed, 'invalid': True}] if deletion == 'invalid' else [])
    response = client.post(f"/api/workflow/cases/{case['id']}/refresh")
    assert response.status_code == 200, response.text
    field = files(platform.preview(case['id'], 'survey'))
    assert [item['id'] for item in field['existing']] == ['defect']
    assert [item['id'] for item in field['removed']] == ['removed']
    assert field['count'] == 2  # one current official file and one local draft file
    assert platform.draft(case['id'], 'survey') == before_draft
    assert services['storage'].verify(blob)
    assert services['domain'].photo(local['id'])['blob_hash'] == local['blob_hash']
    assert response.json()['official_read_at'] == platform.link(case['id'])['verified_at']
    first_removal = deepcopy(field['removed'])
    services['workflow'].refresh_case(case['id'])
    assert files(platform.preview(case['id'], 'survey'))['removed'] == first_removal
    platform.test_survey['fileInfoList'] = [remote_file('defect'), removed]
    services['workflow'].refresh_case(case['id'])
    assert files(platform.preview(case['id'], 'survey'))['removed'] == []


def test_failed_readback_and_unread_fields_never_invent_deletions(platform, services, case, monkeypatch):
    enter_stage(platform, case['id'], 'survey')
    before = deepcopy(platform.link(case['id']))
    def fail(*_):
        raise DomainError('官网连接失败', 502)
    monkeypatch.setattr(platform.client, 'snapshot', fail)
    with pytest.raises(DomainError, match='官网连接失败'):
        services['workflow'].refresh_case(case['id'])
    assert platform.link(case['id']) == before
    partial = deepcopy(before['snapshot'])
    partial.pop('fetched_at', None)
    partial['survey'].pop('fileInfoList')
    services['workflow'].apply_snapshot(partial, case['id'])
    assert files(platform.preview(case['id'], 'survey'))['removed'] == []
    list_only = deepcopy(platform.link(case['id'])['snapshot'])
    list_only['survey'] = {'fileInfoList': []}
    list_only.pop('fetched_at', None)
    services['workflow'].apply_snapshot(list_only, case['id'], details_read=False)
    assert files(platform.preview(case['id'], 'survey'))['removed'] == []


def test_member_can_readback_own_case_but_not_write_or_read_other_project(platform, services, app, client, case, project):
    make_person(services, project['id'])
    other = services['domain'].save_project({'name': '其他项目'})
    other_case = services['domain'].save_case({'project_id': other['id'], 'title': '其他案件'})
    member = login_person(app)
    platform.test_requests.clear()
    assert member.post(f"/api/workflow/cases/{other_case['id']}/refresh").status_code == 403
    assert platform.test_requests == []
    assert member.post(f"/api/workflow/cases/{case['id']}/refresh").status_code == 200
    assert member.post(f"/api/workflow/cases/{case['id']}/review/survey", json={'revision': 0}).status_code == 403
    assert member.put(f"/api/platform/cases/{case['id']}/stages/survey", json={}).status_code == 403


def test_permission_revoked_during_readback_cannot_publish_snapshot(platform, services, app, client, case, project, monkeypatch):
    make_person(services, project['id'])
    member = login_person(app)
    before = deepcopy(platform.link(case['id']))
    original = platform.client.snapshot
    def snapshot(*args):
        result = original(*args)
        services['access'].save_members(project['id'], {'members': []})
        return result
    monkeypatch.setattr(platform.client, 'snapshot', snapshot)
    assert member.post(f"/api/workflow/cases/{case['id']}/refresh").status_code == 403
    assert platform.link(case['id']) == before


def test_deleted_uploaded_original_is_not_automatically_selected_again(platform, services, case, photo, monkeypatch):
    batch, pics = survey_review(platform, services, case, photo)
    mock_writes(platform, monkeypatch)
    assert services['reviews'].confirm(batch['id'], batch['fingerprint'], action='save')['state'] == 'save_verified'
    platform.test_survey['fileInfoList'] = []
    services['workflow'].refresh_case(case['id'])
    incoming = next(services['domain'].photo(pic['id']) for pic in pics if pic['role'] == 'problem')
    result = prepare_materials(platform, services['domain'].case(case['id']), [incoming], actor='rule')
    assert result['state'] == 'excluded'
    assert '官网删除' in result['reason']
    assert not files(platform.preview(case['id'], 'survey'))['selected']
    assert services['storage'].verify(incoming)
