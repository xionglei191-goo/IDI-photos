import json

import pytest

from idi.domain import DomainError
from tests.test_platform_preparation import draft_data, enter_stage, platform


@pytest.mark.parametrize('node,editable', [
    ('intake', 'intake'), ('insurer_review', None), ('survey', 'survey'),
    ('repair', 'repair'), ('acceptance', 'acceptance'), ('settlement', None), ('closed', None),
])
def test_only_current_business_stage_accepts_draft_changes(platform, client, case, node, editable):
    enter_stage(platform, case['id'], node)
    # Stage permissions also apply when the separate remote-write switch is on.
    platform.client.write_enabled = True
    for stage in ('intake', 'survey', 'repair', 'acceptance'):
        url = f"/api/platform/cases/{case['id']}/stages/{stage}"
        plan = client.get(url).json()
        assert plan['access']['editable'] is (stage == editable)
        response = client.put(url, json=draft_data(platform, case['id'], stage))
        assert response.status_code == (200 if stage == editable else 409)
        if stage != editable:
            assert not plan['actions'] and not plan['prepared_for_review']
            assert all(f['readonly'] for f in plan['file_fields'])
            assert client.post(url + '/package', json={'revision': 0}).status_code == 409
            assert client.post(f"/api/workflow/cases/{case['id']}/review/{stage}", json={'revision': 0}).status_code == 409
    assert all('/get' in path for path, _ in platform.test_requests)


def test_completed_official_records_ignore_old_local_drafts_and_later_file_choices(platform, services, client, case, photo):
    enter_stage(platform, case['id'], 'intake')
    platform.save_draft(case['id'], 'intake', draft_data(platform, case['id'], 'intake',
                        {'acceptanceResult': '1', 'reasonDescription': '旧本地受理草稿', 'modifySurveyTimeMark': '0'}))
    enter_stage(platform, case['id'], 'survey')
    picture = photo('orange', '未提交的本地查勘照片.png', stage='survey', role='problem')
    survey_data = draft_data(platform, case['id'], 'survey', files={'fileInfoList': [picture['id']]})
    survey_data['values']['defectDesc'] = '旧本地查勘草稿'
    platform.save_draft(case['id'], 'survey', survey_data)
    platform.test_claim.update(reasonDescription='官网已保存的受理原因', acceptanceResult='1', modifySurveyTimeMark='0')
    platform.test_survey['defectDesc'] = '官网已提交的查勘结论'
    enter_stage(platform, case['id'], 'repair')
    before = services['db'].all('SELECT * FROM platform_drafts WHERE case_id=? ORDER BY stage', (case['id'],))
    for stage, field, expected in [('intake', 'reasonDescription', '官网已保存的受理原因'), ('survey', 'defectDesc', '官网已提交的查勘结论')]:
        url = f"/api/platform/cases/{case['id']}/stages/{stage}"
        plan = client.get(url).json()
        assert plan['access']['mode'] == 'completed' and plan['view_source'] == 'official'
        assert plan['values'][field] == expected
        assert '旧本地' in plan['draft']['values'][field]  # Retained for provenance, never used as the official view.
        assert all(not f['selected'] and not f['suggested_ids'] for f in plan['file_fields'])
        changed = draft_data(platform, case['id'], stage)
        changed['values'][field] = '试图改写旧环节'
        assert client.put(url, json=changed).status_code == 409
        assert client.post(url + '/preview', json=changed).json()['values'][field] == expected
        with pytest.raises(DomainError, match='仅供查看'):
            platform.build_payload(plan, 'submit', {})
    assert services['db'].all('SELECT * FROM platform_drafts WHERE case_id=? ORDER BY stage', (case['id'],)) == before
    active = client.put(f"/api/platform/cases/{case['id']}/stages/repair", json=draft_data(platform, case['id'], 'repair', {'repairCrewNumber': '2'}))
    assert active.status_code == 200 and active.json()['draft']['values']['repairCrewNumber'] == '2'
    assert client.put(f"/api/platform/cases/{case['id']}/stages/acceptance", json=draft_data(platform, case['id'], 'acceptance')).status_code == 409


def test_old_review_and_payload_cannot_submit_after_advance(platform, services, case, monkeypatch):
    enter_stage(platform, case['id'], 'survey')
    plan = platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey'))
    review = services['reviews'].prepare(case['id'], 'survey', plan['draft']['revision'])
    frozen = json.dumps(review['snapshot'], ensure_ascii=False, sort_keys=True)
    enter_stage(platform, case['id'], 'repair')
    platform.client.write_enabled = True
    monkeypatch.setattr(platform.client, 'upload', lambda *_: pytest.fail('A completed stage must never upload'))
    monkeypatch.setattr(platform.client, 'submit', lambda *_: pytest.fail('A completed stage must never submit'))
    assert not services['reviews'].get(review['id'])['access']['editable']
    with pytest.raises(DomainError, match='仅供查看'):
        services['reviews'].confirm(review['id'], review['fingerprint'])
    with pytest.raises(DomainError, match='仅供查看'):
        platform.build_payload(plan, 'submit', {})
    stored = services['reviews'].get(review['id'])
    assert stored['state'] == 'stale'
    assert json.dumps(stored['snapshot'], ensure_ascii=False, sort_keys=True) == frozen


def test_shared_case_keeps_intake_view_and_repair_action(platform, services, case):
    platform.test_row['flowStatus'] = '9'
    services['workflow'].sync({}, lambda *_: None)
    intake = services['workflow'].board('intake')['items'][0]
    repair = services['workflow'].board('work')['items'][0]
    assert intake['id'] == repair['id'] == case['id']
    assert intake['form_stage'] == 'intake' and intake['action_label'] == '查看'
    assert intake['next_action'] == '查看受理记录'
    assert repair['form_stage'] == 'repair' and repair['action_label'] == '处理'
    assert services['workflow'].detail(case['id'])['stage_access']['survey']['mode'] == 'completed'


def test_official_return_reopens_only_intake(platform, services, case):
    enter_stage(platform, case['id'], 'insurer_review')
    assert not platform.preview(case['id'], 'intake')['access']['editable']
    platform.test_row.update(flowStatus='1', passStatus='2', acceptanceResult='2')
    services['workflow'].refresh_case(case['id'])
    plan = platform.save_draft(case['id'], 'intake', draft_data(platform, case['id'], 'intake',
                             {'acceptanceResult': '1', 'reasonDescription': '根据官网退回意见修改'}))
    assert plan['access']['editable']
    with pytest.raises(DomainError, match='尚未进入'):
        platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey'))
