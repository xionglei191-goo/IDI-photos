import pytest

from tests.test_platform_preparation import platform, enter_stage


def checkin(services, case, photo, monkeypatch, *, survey=True):
    services['domain'].save_case({'platform_report_code': 'ZLBH20260001'}, case['id'])
    picture = photo(stage='work', role='other')
    raw = '案件编号：ZLBH20260001\n时间：2026-10-16 10:51:27\n打卡内容：维修打卡'
    monkeypatch.setattr(services['recognizer'], 'local_ocr', lambda path: (raw, []))
    services['recognizer'].recognize(picture['id'])
    return services['domain'].update_photo(picture['id'], {
        'photo_group': 'survey_checkin' if survey else 'work_checkin',
        'role': 'building_checkin', 'watermark_time': '2026-10-16 10:51:27', 'confirm': True})


def share(services, case, picture):
    return services['domain'].save_visit(case['id'], {
        'stage': 'work', 'visit_date': '2026-10-16', 'visit_key': '同次查勘维修',
        'started_at': '2026-10-16 10:51:27', 'same_day_repair': True,
        'shared_checkin_ids': [picture['id']], 'photo_ids': []})


def test_explicit_same_entry_unblocks_original_work_watermark_for_both_stages(platform, services, case, photo, monkeypatch):
    platform.test_survey.clear()
    enter_stage(platform, case['id'], 'survey')
    picture = checkin(services, case, photo, monkeypatch)
    rules = services['photo_workflow']
    assert rules.process(picture['id'])['state'] == 'needs_check'
    visit = share(services, case, picture)
    current = services['domain'].photo(picture['id'])
    assert current['blob_hash'] == picture['blob_hash']
    assert current['revision'] == picture['revision'] + 1
    assert services['db'].one('SELECT dirty FROM photo_pipeline WHERE photo_id=?', (picture['id'],))['dirty'] == 1
    assert rules.process(picture['id'])['state'] == 'local_draft'
    plan = platform.prefill_draft(case['id'], 'survey', {picture['id']})['plan']
    assert plan['values']['siteSurveyDate'] == '2026-10-16 10:51:00'
    assert plan['draft']['files']['buildingFileInfoList'] == [picture['id']]
    assert plan['field_sources']['siteSurveyDate']['photo_ids'] == [picture['id']]
    enter_stage(platform, case['id'], 'repair')
    assert rules.process(picture['id'])['state'] == 'local_draft'
    assert platform.draft(case['id'], 'repair')['files']['buildingFileInfoList'] == [picture['id']]
    assert services['domain'].photo(picture['id'])['shared_checkin_visit_id'] == visit['id']
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 1
    assert not services['db'].one('SELECT * FROM platform_review_batches')


def test_removing_reuse_invalidates_queued_evidence_and_noop_visit_edit_does_not(platform, services, case, photo, monkeypatch):
    platform.test_survey.clear()
    enter_stage(platform, case['id'], 'survey')
    picture = checkin(services, case, photo, monkeypatch)
    visit = share(services, case, picture)
    rules = services['photo_workflow']
    assert rules.process(picture['id'])['state'] == 'local_draft'
    platform.prefill_draft(case['id'], 'survey', {picture['id']})
    before = services['domain'].photo(picture['id'])
    visit = services['domain'].save_visit(case['id'], {**visit, 'same_day_repair': True, 'notes': '补充备注'}, visit['id'])
    assert services['domain'].photo(picture['id'])['revision'] == before['revision']
    assert services['db'].one('SELECT dirty FROM photo_pipeline WHERE photo_id=?', (picture['id'],))['dirty'] == 0
    services['domain'].save_visit(case['id'], {**visit, 'same_day_repair': False, 'shared_checkin_ids': []}, visit['id'])
    assert services['domain'].photo(picture['id'])['revision'] == before['revision'] + 1
    assert services['db'].one('SELECT dirty FROM photo_pipeline WHERE photo_id=?', (picture['id'],))['dirty'] == 1
    assert platform.preview(case['id'], 'survey')['field_sources']['siteSurveyDate']['stale']
    assert rules.process(picture['id'])['state'] == 'needs_check'


def test_reviewed_role_can_refine_generic_ocr_work_checkin_without_reuse(platform, services, case, photo, monkeypatch):
    enter_stage(platform, case['id'], 'repair')
    picture = checkin(services, case, photo, monkeypatch, survey=False)
    assert services['photo_workflow'].process(picture['id'])['state'] == 'local_draft'
    assert platform.draft(case['id'], 'repair')['files']['buildingFileInfoList'] == [picture['id']]


@pytest.mark.parametrize('change', ['opposite_role', 'not_checkin', 'unconfirmed', 'wrong_date', 'wrong_case', 'not_same_entry'])
def test_shared_visit_does_not_hide_real_purpose_or_scope_conflict(platform, services, case, photo, monkeypatch, change):
    enter_stage(platform, case['id'], 'survey')
    picture = checkin(services, case, photo, monkeypatch)
    visit = share(services, case, picture)
    current = services['domain'].photo(picture['id'])
    fields = {'stage': 'work', 'photo_group': 'work_checkin', 'role': 'other'}
    if change == 'opposite_role':
        fields['role'] = 'door_checkin'
    elif change == 'not_checkin':
        fields.update(photo_group='construction', role='construction')
    elif change == 'unconfirmed':
        current['recognition_state'] = 'recognized'
    elif change == 'wrong_date':
        current['watermark_time'] = '2026-10-17 10:51'
    elif change == 'wrong_case':
        current['case_id'] = 'another-case'
    else:
        with services['db'].tx() as con:
            con.execute('UPDATE visits SET same_day_repair=0 WHERE id=?', (visit['id'],))
    assert services['photo_workflow'].purpose_conflicts(current, fields, set(current['confirmed_fields']))


def test_reclassifying_shared_photo_removes_reuse_and_marks_pipeline_dirty(platform, services, case, photo, monkeypatch):
    enter_stage(platform, case['id'], 'survey')
    picture = checkin(services, case, photo, monkeypatch)
    share(services, case, picture)
    services['photo_workflow'].process(picture['id'])
    current = services['domain'].update_photo(picture['id'], {'photo_group': 'work_checkin', 'role': 'building_checkin'})
    assert not current['shared_checkin_visit_id']
    assert services['db'].one('SELECT dirty FROM photo_pipeline WHERE photo_id=?', (picture['id'],))['dirty'] == 1
    assert services['db'].all('PRAGMA foreign_key_check') == []
