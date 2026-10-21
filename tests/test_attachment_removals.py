from copy import deepcopy

import pytest

from idi.attachment_removals import AttachmentRemovals
from idi.domain import DomainError
from idi.draft_materials import prepare_materials
from idi.platform_client import PlatformWriteError
from tests.conftest import image_bytes
from tests.test_platform_preparation import platform, enter_stage, remote_file, draft_data
from tests.test_automatic_uploads import setup_flow, ready, tick


@pytest.fixture
def removal_setup(platform, case, monkeypatch):
    enter_stage(platform, case['id'], 'survey')
    platform.client.write_enabled = True
    platform.test_survey['fileInfoList'].append(remote_file('keep', '保留照片.png'))
    platform.workflow.refresh_case(case['id'])
    monkeypatch.setattr(platform.client, 'download_attachment', lambda *args: image_bytes('orange'))
    writes = []

    def write(stage, payload):
        assert stage == 'survey'
        writes.append(deepcopy(payload))
        platform.test_survey.update(deepcopy(payload))
        platform.test_row['siteSurveyState'] = str(payload['state'])
        return {'code': 0}

    monkeypatch.setattr(platform.client, 'save_draft_only', write)
    monkeypatch.setattr(platform.client, 'upload', lambda *args: pytest.fail('Removal must not upload files'))
    monkeypatch.setattr(platform.client, 'submit', lambda *args: pytest.fail('Removal must not submit the stage'))
    return platform.removals, writes


def prepared(service, case):
    return service.prepare(case['id'], 'survey', 'fileInfoList', 'defect')


def test_withdraw_preserves_other_files_fields_and_original(removal_setup, platform, case, services):
    service, writes = removal_setup
    # Unsaved local edits must not leak into the official withdrawal.
    platform.save_draft(case['id'], 'survey', draft_data(
        platform, case['id'], 'survey', values={'defectDesc': '尚未提交的本地编辑'}))
    before = deepcopy(platform.test_survey)
    record = prepared(service, case)
    assert writes == []
    result = service.confirm(record['id'], record['fingerprint'])
    assert result['state'] == 'verified'
    assert [f['id'] for f in writes[0]['fileInfoList']] == ['keep']
    assert writes[0]['state'] == 0
    for key in before.keys() - {'fileInfoList'}:
        assert writes[0][key] == before[key]
    assert platform.draft(case['id'], 'survey')['values']['defectDesc'] == '尚未提交的本地编辑'
    assert services['domain'].case(case['id'])['workflow_stage'] == 'survey'
    cached = services['db'].one('SELECT blob_hash FROM platform_media WHERE file_id=?', ('defect',))
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (cached['blob_hash'],))
    assert services['storage'].verify(blob)
    assert service.confirm(record['id'], record['fingerprint'])['state'] == 'verified'
    assert len(writes) == 1


def test_last_required_photo_can_be_withdrawn_without_advancing(removal_setup, platform, case):
    service, writes = removal_setup
    record = service.prepare(case['id'], 'survey', 'buildingFileInfoList', 'survey-building')
    assert service.confirm(record['id'], record['fingerprint'])['state'] == 'verified'
    assert writes[0]['buildingFileInfoList'] == []
    plan = platform.preview(case['id'], 'survey')
    assert any(i['field'] == 'buildingFileInfoList' for i in plan['issues'])


def test_stale_remote_fields_prevent_overwrite(removal_setup, platform, case):
    service, writes = removal_setup
    record = prepared(service, case)
    platform.test_survey['defectDesc'] = '官网另一个用户已修改'
    with pytest.raises(DomainError, match='官网资料已变化'):
        service.confirm(record['id'], record['fingerprint'])
    assert writes == []
    assert service.get(record['id'])['state'] == 'stale'


@pytest.mark.parametrize('change', ['stage', 'account', 'readonly'])
def test_changed_permissions_do_not_remove(removal_setup, platform, case, change):
    service, writes = removal_setup
    record = prepared(service, case)
    if change == 'stage':
        platform.test_row.update(siteSurveyState='1', flowStatus='9')
    elif change == 'account':
        platform.client._account = 'another-account'
    else:
        platform.client.write_enabled = False
    with pytest.raises(DomainError):
        service.confirm(record['id'], record['fingerprint'])
    assert writes == []


def test_wrong_field_and_readonly_source_cannot_be_removed(removal_setup, platform, case):
    service, writes = removal_setup
    with pytest.raises(DomainError):
        service.prepare(case['id'], 'survey', 'buildingFileInfoList', 'defect')
    enter_stage(platform, case['id'], 'repair')
    with pytest.raises(DomainError, match='其他环节带入'):
        service.prepare(case['id'], 'repair', 'preRepairFileInfo', 'defect')
    assert writes == []


def test_unknown_result_blocks_writes_until_readback(removal_setup, platform, case, monkeypatch, services):
    service, writes = removal_setup
    original_write = platform.client.save_draft_only

    def interrupted(stage, payload):
        original_write(stage, payload)
        raise TimeoutError('响应超时')

    record = prepared(service, case)
    monkeypatch.setattr(platform.client, 'save_draft_only', interrupted)
    assert service.confirm(record['id'], record['fingerprint'])['state'] == 'uncertain'
    with pytest.raises(DomainError, match='待核实'):
        service.require_clear(case['id'], 'survey')
    with pytest.raises(DomainError, match='勿重复发送'):
        service.confirm(record['id'], record['fingerprint'])
    assert platform.preview(case['id'], 'survey')['attachment_removals'][0]['id'] == record['id']
    assert service.reconcile(record['id'])['state'] == 'verified'
    assert not service.pending(case['id'], 'survey')
    assert len(writes) == 1


@pytest.mark.parametrize('outcome', ['rejected', 'retained', 'other_file_lost', 'read_error'])
def test_rejection_or_inconsistent_readback_never_reports_success(removal_setup, platform, case, monkeypatch, outcome):
    service, _ = removal_setup
    record = prepared(service, case)
    normal = platform.client.save_draft_only
    refresh = platform.workflow.refresh_case

    def write(stage, payload):
        if outcome == 'rejected':
            raise PlatformWriteError('官网拒绝删除', rejected=True)
        if outcome == 'retained':
            return {'code': 0}
        result = normal(stage, payload)
        if outcome == 'other_file_lost':
            platform.test_survey['houseFileInfoList'] = []
        if outcome == 'read_error':
            monkeypatch.setattr(platform.workflow, 'refresh_case', lambda *args: (_ for _ in ()).throw(DomainError('网络中断')))
        return result

    monkeypatch.setattr(platform.client, 'save_draft_only', write)
    result = service.confirm(record['id'], record['fingerprint'])
    assert result['state'] == ('failed' if outcome == 'rejected' else 'saved')
    assert result['error']
    if outcome != 'rejected':
        assert service.pending(case['id'], 'survey')
    monkeypatch.setattr(platform.workflow, 'refresh_case', refresh)


def test_repair_withdraw_preserves_inherited_materials(platform, case, monkeypatch):
    platform.client.write_enabled = True
    platform.test_repair['recoveredFileInfo'] = [remote_file('finished', '完工.png')]
    enter_stage(platform, case['id'], 'repair')
    monkeypatch.setattr(platform.client, 'download_attachment', lambda *args: image_bytes())
    writes = []

    def write(stage, payload):
        assert stage == 'repair' and payload['state'] == 0
        writes.append(deepcopy(payload))
        platform.test_repair.update(payload)
        return {'code': 0}

    monkeypatch.setattr(platform.client, 'save_draft_only', write)
    record = platform.removals.prepare(case['id'], 'repair', 'recoveredFileInfo', 'finished')
    assert platform.removals.confirm(record['id'], record['fingerprint'])['state'] == 'verified'
    assert writes[0]['recoveredFileInfo'] == []
    assert writes[0]['preRepairFileInfo'] == [remote_file('defect')]
    assert platform.domain.case(case['id'])['workflow_stage'] == 'repair'


def test_withdraw_excludes_original_and_same_content_new_import(removal_setup, platform, case, photo, services):
    service, _ = removal_setup
    picture = photo('orange', '误传照片.png', stage='survey', role='problem', recognition_state='confirmed')
    plan = platform.save_draft(case['id'], 'survey', draft_data(
        platform, case['id'], 'survey', files={'fileInfoList': [picture['id']]}))
    review = services['reviews'].prepare(case['id'], 'survey', plan['draft']['revision'], save_only=True)
    record = prepared(service, case)
    assert service.confirm(record['id'], record['fingerprint'])['state'] == 'verified'
    assert platform.draft(case['id'], 'survey')['files']['fileInfoList'] == []
    assert services['reviews'].get(review['id'])['state'] == 'stale'
    assert services['domain'].photo(picture['id'])['blob_hash'] == picture['blob_hash']
    # An independent photo ID with identical contents is also excluded.
    other_case = services['domain'].save_case({'project_id': case['project_id'], 'title': '其他案件'})
    with services['db'].tx() as con:
        source = dict(con.execute('SELECT * FROM photos WHERE id=?', (picture['id'],)).fetchone())
        con.execute('UPDATE photos SET case_id=? WHERE id=?', (other_case['id'], picture['id']))
        source['id'] = 'same-content-new-photo'
        con.execute('INSERT INTO photos(' + ','.join(source) + ') VALUES(' + ','.join('?' for _ in source) + ')', tuple(source.values()))
    other = services['domain'].photo(source['id'])
    result = prepare_materials(platform, services['domain'].case(case['id']), [other], actor='photo_rules')
    assert result['state'] == 'excluded'
    # Explicitly choosing the picture again is allowed and clears its content block.
    platform.save_draft(case['id'], 'survey', draft_data(
        platform, case['id'], 'survey', files={'fileInfoList': [other['id']]}))
    assert not services['db'].one('SELECT 1 FROM platform_attachment_blocks WHERE blob_hash=?', (other['blob_hash'],))


def test_remove_local_selection_does_not_delete_photo_or_write_remote(removal_setup, platform, case, photo, services):
    service, writes = removal_setup
    picture = photo('navy', '本地待选.png', stage='survey', role='problem')
    plan = platform.save_draft(case['id'], 'survey', draft_data(
        platform, case['id'], 'survey', files={'fileInfoList': [picture['id']]}))
    with pytest.raises(DomainError):
        service.remove_local(case['id'], 'survey', 'fileInfoList', picture['id'], plan['draft']['revision'] - 1)
    updated = service.remove_local(case['id'], 'survey', 'fileInfoList', picture['id'], plan['draft']['revision'])
    assert not next(f for f in updated['file_fields'] if f['key'] == 'fileInfoList')['selected']
    assert services['domain'].photo(picture['id'])
    assert writes == []


def test_api_requires_explicit_confirmation_and_valid_fingerprint(removal_setup, case, client):
    service, writes = removal_setup
    record = prepared(service, case)
    path = f"/api/platform/attachment-removals/{record['id']}/confirm"
    assert client.post(path, json={'fingerprint': record['fingerprint']}).status_code == 400
    assert client.post(path, json={'acknowledged': True, 'fingerprint': 'changed'}).status_code == 409
    assert writes == []
    response = client.post(path, json={'acknowledged': True, 'fingerprint': record['fingerprint']})
    assert response.status_code == 200 and response.json()['state'] == 'verified'


def test_interrupted_removal_is_not_replayed_on_restart(removal_setup, case):
    service, writes = removal_setup
    record = prepared(service, case)
    service._state(record['id'], 'removing')
    restarted = AttachmentRemovals(service.platform)
    assert restarted.get(record['id'])['state'] == 'uncertain'
    assert writes == []


def test_automatic_cycle_after_withdrawal_does_not_restore_photo(platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    row = ready(services, platform, case, add, auto)
    old_review = row['review_id']
    # The real upload receipt must map back to the retained local original.
    record = platform.removals.prepare(case['id'], 'survey', 'fileInfoList',
                                      platform.test_survey['fileInfoList'][0]['id'])
    assert platform.removals.confirm(record['id'], record['fingerprint'])['state'] == 'verified'
    uploaded_before = list(uploads)
    row = tick(auto, case['id'])
    assert uploads == uploaded_before
    assert platform.test_survey['fileInfoList'] == []
    assert row['state'] == 'needs_fields'
    assert row['review_id'] is None
    assert services['reviews'].get(old_review)['state'] == 'stale'
    assert all(payload['state'] == 0 for _, payload in writes)


def test_readback_accepts_context_ids_returned_only_in_claim(removal_setup, platform, case, monkeypatch):
    service, _ = removal_setup
    record = prepared(service, case)
    write = platform.client.save_draft_only

    def context_in_claim(stage, payload):
        result = write(stage, payload)
        for key in ('communityInfoId', 'engineeringModelingParentId', 'repairUnitId'):
            platform.test_survey.pop(key, None)
        return result

    monkeypatch.setattr(platform.client, 'save_draft_only', context_in_claim)
    assert service.confirm(record['id'], record['fingerprint'])['state'] == 'verified'


def test_removed_original_is_still_accessible_via_audited_receipt(removal_setup, case, client):
    service, _ = removal_setup
    record = prepared(service, case)
    service.confirm(record['id'], record['fingerprint'])
    response = client.get(f"/api/platform/attachment-removals/{record['id']}/original")
    assert response.status_code == 200
    assert response.content == image_bytes('orange')
