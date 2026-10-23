from copy import deepcopy

import pytest

from tests.test_platform_preparation import (
    REPAIR_ID, draft_data, enter_stage, platform, remote_file,
)


@pytest.mark.parametrize('method', ['1', '2'])
def test_new_repair_save_then_submit_reuses_files_and_created_record(
        platform, services, case, photo, monkeypatch, client, method):
    platform.test_row.update(repairAcceptanceId=None, repairAcceptanceState=None, repairMethod=method)
    platform.test_survey['repairMethod'] = method
    platform.test_repair.clear()
    enter_stage(platform, case['id'], 'repair')
    # Explicitly selected survey check-ins represent user-confirmed same-day reuse.
    # Their original archive roles must remain intact after saving and submitting.
    pictures = [photo(color, name, stage=stage, role=role) for color, name, stage, role in [
        ('red', '楼栋.png', 'survey', 'building_checkin'),
        ('blue', '入户.png', 'survey', 'door_checkin'),
        ('green', '完工1.png', 'acceptance', 'finished'),
        ('yellow', '完工2.png', 'acceptance', 'finished'),
    ]]
    first = platform.draft(case['id'], 'repair')
    assert first['values']['repairCrewNumber'] == '1'
    assert not first['files']['buildingFileInfoList'] and not first['files']['houseFileInfoList']
    values = {**first['values'], 'approachStartWorkDate': '2026-10-16T09:29', 'finishWorkDate': '2026-10-16T09:47'}
    files = {'buildingFileInfoList': [pictures[0]['id']], 'houseFileInfoList': [pictures[1]['id']],
             'recoveredFileInfo': [p['id'] for p in pictures[2:]]}
    plan = platform.save_draft(case['id'], 'repair', draft_data(platform, case['id'], 'repair', values, files))
    assert plan['error_count'] == 0
    assert plan['business_fields']['timeLimit'] == 1
    assert 'repairAcceptanceId' not in plan['business_fields']
    batch = services['reviews'].prepare(case['id'], 'repair', plan['draft']['revision'])
    uploaded, writes = [], []

    def upload(path, name, mime):
        uploaded.append(name)
        return remote_file('new-repair-file-' + str(len(uploaded)), name)

    def write(stage, payload):
        assert stage == 'repair'
        writes.append(deepcopy(payload))
        platform.test_repair.update(deepcopy(payload), repairAcceptanceId=REPAIR_ID)
        platform.test_row.update(repairAcceptanceId=REPAIR_ID,
                                 repairAcceptanceState=str(payload['state']),
                                 repairAcceptanceFlowStatus=str(payload['state']))
        return {'code': 0, 'data': {'repairAcceptanceId': REPAIR_ID}}

    monkeypatch.setattr(platform.client, 'upload', upload)
    monkeypatch.setattr(platform.client, 'submit', write)
    platform.client.write_enabled = True
    response = client.post(f"/api/workflow/reviews/{batch['id']}/confirm", json={
        'acknowledged': True, 'fingerprint': batch['fingerprint'], 'action': 'save'})
    assert response.status_code == 200
    result = response.json()
    assert result['state'] == 'save_verified' and result['result']['mismatches'] == []
    assert not result['result']['business_submitted'] and writes[0]['state'] == 0
    assert len(uploaded) == 4 and len(writes) == 1
    assert [f['id'] for f in writes[0]['preRepairFileInfo']] == ['defect']
    assert 'acceptanceDate' not in writes[0] and 'acceptanceCheckFileInfo' not in writes[0]
    assert ('supplementaryMaterialsFileInfo' in writes[0]) == (method == '2')
    assert services['domain'].case(case['id'])['workflow_stage'] == 'repair'
    services['default_packages'].run_once()
    assert services['default_packages'].get(case['id'])['state'] == 'waiting'
    saved = platform.draft(case['id'], 'repair')
    assert saved['values'] == values and all(not ids for ids in saved['files'].values())
    next_batch = services['reviews'].prepare(case['id'], 'repair', saved['revision'])
    submitted = services['reviews'].confirm(next_batch['id'], next_batch['fingerprint'], action='submit')
    assert submitted['state'] == 'verified' and not submitted['result']['mismatches']
    assert writes[1]['state'] == 1 and writes[1]['repairAcceptanceId'] == REPAIR_ID
    assert len(uploaded) == 4 and len(writes) == 2
    assert services['domain'].case(case['id'])['workflow_stage'] == 'acceptance'
    services['default_packages'].run_once()
    while services['jobs'].run_one():
        pass
    services['default_packages'].run_once()
    assert services['default_packages'].get(case['id'])['state'] == 'ready'
    for picture in pictures:
        current = services['domain'].photo(picture['id'])
        assert (current['stage'], current['role'], current['blob_hash']) == (picture['stage'], picture['role'], picture['blob_hash'])


def test_repair_saved_empty_defect_list_does_not_restore_survey_attachments(platform, case):
    platform.test_repair['preRepairFileInfo'] = []
    enter_stage(platform, case['id'], 'repair')
    plan = platform.preview(case['id'], 'repair')
    assert next(f for f in plan['file_fields'] if f['key'] == 'preRepairFileInfo')['existing'] == []


def test_repair_crew_default_never_overwrites_official_or_saved_values(platform, case):
    platform.test_repair['repairCrewNumber'] = 3
    enter_stage(platform, case['id'], 'repair')
    assert platform.draft(case['id'], 'repair')['values']['repairCrewNumber'] == '3'
    for count in ('2', ''):
        platform.save_draft(case['id'], 'repair', draft_data(platform, case['id'], 'repair', values={'repairCrewNumber': count}))
        assert platform.draft(case['id'], 'repair')['values']['repairCrewNumber'] == count
