from copy import deepcopy

import pytest

from idi.domain import DomainError
from idi.platform_prefill import repair_suggestions
from tests.test_platform_preparation import platform, enter_stage, draft_data
from tests.test_automatic_uploads import setup_flow, tick
from tests.test_feishu_agent import bot, capture, task_for


def blank_survey(platform, services, case):
    platform.test_survey.clear()
    platform.test_claim['reasonDescription'] = '施工时保护不到位、操作不当造成'
    enter_stage(platform, case['id'], 'survey')
    services['domain'].save_case({'title': '主卧卫生间瓷砖破裂，卫生间反臭，开空调时房间很臭', 'repair_parts': []}, case['id'])


def confirmed(services, photo, color, group, time, role='problem'):
    p = photo(color, color + '.png')
    return services['domain'].update_photo(p['id'], {'photo_group': group, 'role': role, 'watermark_time': time, 'confirm': True})


def test_case_evidence_is_filled_without_photos_and_ambiguous_fields_are_explained(platform, services, case):
    blank_survey(platform, services, case)
    result = platform.prefill_draft(case['id'], 'survey', set())
    plan = result['plan']
    assert result['changed']
    assert plan['draft']['values'] == {'defectDesc': '施工时保护不到位、操作不当造成', 'defectiveSite': '主卧卫生间瓷砖',
                                      'divisionWorksCode': 'part_engineering_03'}
    assert plan['draft']['files'] == {}
    assert len(plan['field_sources']) == 3
    assert all(s['origin'] == 'automatic' and s['current'] and not s['stale'] for s in plan['field_sources'].values())
    assert '装饰装修工程' in plan['field_sources']['divisionWorksCode']['source']
    assert 'divisionWorksCode' not in plan['field_suggestion_notes']
    assert '多个' not in plan['field_suggestion_notes']['siteSurveyDate']
    revision = plan['draft']['revision']
    again = platform.prefill_draft(case['id'], 'survey', set())
    assert not again['changed'] and again['plan']['draft']['revision'] == revision


def test_single_personnel_prefill_includes_name_phone_and_is_stable(platform, case):
    platform.test_survey.clear()
    platform.test_survey['repairMethod'] = '1'
    platform.test_personnel[:] = platform.test_personnel[:1]
    enter_stage(platform, case['id'], 'survey')
    plan = platform.prefill_draft(case['id'], 'survey', set())['plan']
    assert plan['draft']['values']['repairUserId'] == 'person-1'
    assert plan['business_fields']['repairPersonName'] == '甲'
    assert plan['business_fields']['repairPersonPhone'] == '13800000001'
    assert plan['field_sources']['repairUserId']['origin'] == 'automatic'
    assert not platform.prefill_draft(case['id'], 'survey', set())['changed']
    # A later change to multiple candidates removes the unconfirmed automatic choice.
    platform.test_personnel.append({'userId': 'person-2', 'name': '乙', 'phone': '13800000002'})
    enter_stage(platform, case['id'], 'survey')
    plan = platform.prefill_draft(case['id'], 'survey', set())['plan']
    assert 'repairUserId' not in plan['draft']['values']


@pytest.mark.parametrize('personnel_value', ['person-1', ''])
def test_new_selection_rules_preserve_manual_category_and_personnel(platform, case, personnel_value):
    platform.test_survey.clear()
    platform.test_personnel[:] = platform.test_personnel[:1]
    enter_stage(platform, case['id'], 'survey')
    data = draft_data(platform, case['id'], 'survey', {
        'divisionWorksCode': 'part_engineering_04', 'repairMethod': '1', 'repairUserId': personnel_value})
    platform.save_draft(case['id'], 'survey', data)
    plan = platform.prefill_draft(case['id'], 'survey', set())['plan']
    assert plan['values']['divisionWorksCode'] == 'part_engineering_04'
    assert plan['values']['repairUserId'] == personnel_value
    assert plan['field_sources']['divisionWorksCode']['origin'] == 'manual'
    assert plan['field_sources']['repairUserId']['origin'] == 'manual'


def test_updated_rules_revisit_waiting_cases_and_save_only_drafts(platform, services, case, photo, monkeypatch):
    import idi.automatic_uploads as automatic_module
    import idi.platform as platform_module

    auto, _, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    platform.test_survey.clear()
    platform.test_survey['repairMethod'] = '1'
    platform.test_personnel[:] = platform.test_personnel[:1]
    services['domain'].save_case({'title': '洗手盆水龙头松动，次卧门把手松动'}, case['id'])
    enter_stage(platform, case['id'], 'survey')
    current_suggestions = platform_module.stage_suggestions

    def old_suggestions(*args, **kwargs):
        suggestions, notes = current_suggestions(*args, **kwargs)
        for key in ('divisionWorksCode', 'repairUserId'):
            suggestions.pop(key, None)
        return suggestions, notes

    with monkeypatch.context() as previous:
        previous.setattr(automatic_module, 'PREFILL_RULES_VERSION', 'automatic-prefill-v1')
        previous.setattr(platform_module, 'stage_suggestions', old_suggestions)
        assert tick(auto, case['id'])['state'] == 'needs_fields'
        assert auto.run_once() == 0
    previous_writes = len(writes)
    assert auto.run_once() == 1
    payload = writes[-1][1]
    assert len(writes) == previous_writes + 1
    assert payload['divisionWorksCode'] == 'part_engineering_03'
    assert payload['repairUserId'] == 'person-1'
    assert payload['repairPersonName'] == '甲' and payload['repairPersonPhone'] == '13800000001'
    assert all(p['state'] == 0 for _, p in writes) and not uploads
    assert auto.run_once() == 0


def test_manual_values_explicit_blanks_and_file_selections_are_preserved(platform, services, case, photo):
    blank_survey(platform, services, case)
    p = confirmed(services, photo, 'red', 'survey_checkin', '2026-10-16 09:00', 'building_checkin')
    data = draft_data(platform, case['id'], 'survey', {'defectDesc': '', 'defectiveSite': '人工核实的部位', 'siteSurveyDate': '2026-10-16 09:15'}, {'buildingFileInfoList': [p['id']]})
    platform.save_draft(case['id'], 'survey', data)
    plan = platform.prefill_draft(case['id'], 'survey', {p['id']})['plan']
    assert plan['draft']['values'] == {**data['values'], 'divisionWorksCode': 'part_engineering_03'}
    assert plan['draft']['files'] == data['files']
    assert all(plan['field_sources'][key]['origin'] == 'manual' for key in data['values'])
    assert plan['field_sources']['divisionWorksCode']['origin'] == 'automatic'
    # A removed key also represents an explicit manual clear.
    platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', {}))
    assert platform.prefill_draft(case['id'], 'survey', {p['id']})['plan']['draft']['values'] == {}


def test_new_photo_invalidates_time_then_recomputes_without_reusing_own_official_echo(platform, services, case, photo):
    blank_survey(platform, services, case)
    first = confirmed(services, photo, 'red', 'survey_checkin', '2026-10-16 09:00', 'building_checkin')
    before = platform.prefill_draft(case['id'], 'survey', {first['id']})['plan']
    assert before['values']['siteSurveyDate'] == '2026-10-16 09:00:00'
    platform.test_survey.update(deepcopy(before['business_fields']))
    enter_stage(platform, case['id'], 'survey')
    later = confirmed(services, photo, 'blue', 'survey_checkin', '2026-10-17 09:00', 'door_checkin')
    stale = platform.preview(case['id'], 'survey')
    assert stale['field_sources']['siteSurveyDate']['stale']
    assert any(i['field'] == 'siteSurveyDate' for i in platform.validation_errors(stale, allow_incomplete=True))
    after = platform.prefill_draft(case['id'], 'survey', {first['id'], later['id']})['plan']
    assert 'siteSurveyDate' not in after['draft']['values']
    assert 'siteSurveyDate' not in after['field_sources']
    assert '多个日期' in after['field_suggestion_notes']['siteSurveyDate']
    assert any(i['field'] == 'siteSurveyDate' and i['code'] == 'required' for i in after['issues'])


def test_reclassification_removes_old_automatic_date(platform, services, case, photo):
    blank_survey(platform, services, case)
    p = confirmed(services, photo, 'red', 'survey_checkin', '2026-10-16 09:00', 'building_checkin')
    platform.prefill_draft(case['id'], 'survey', {p['id']})
    services['domain'].update_photo(p['id'], {'photo_group': 'work_checkin', 'role': 'building_checkin', 'confirm': True})
    plan = platform.prefill_draft(case['id'], 'survey', {p['id']})['plan']
    assert 'siteSurveyDate' not in plan['draft']['values']


def test_arrival_during_prefill_does_not_save_outdated_evidence(platform, services, case, photo, monkeypatch):
    blank_survey(platform, services, case)
    original = platform.save_draft
    def raced(*args, **kwargs):
        photo('blue', 'late.png')
        return original(*args, **kwargs)
    monkeypatch.setattr(platform, 'save_draft', raced)
    with pytest.raises(DomainError, match='照片发生变化'):
        platform.prefill_draft(case['id'], 'survey', set())
    assert platform.draft(case['id'], 'survey')['revision'] == 0
    assert not platform.field_sources(case['id'], 'survey')


def test_completed_stage_is_not_automatically_edited(platform, services, case):
    blank_survey(platform, services, case)
    enter_stage(platform, case['id'], 'repair')
    assert not platform.prefill_draft(case['id'], 'survey', set())['changed']
    assert platform.draft(case['id'], 'survey')['revision'] == 0


def repair_photo(pid, group, time, **extra):
    return {'id': pid, 'case_id': 'c', 'media_type': 'image', 'recognition_state': 'recognized', 'photo_group': group,
            'watermark_time': time, 'original_name': pid + '.jpg', **extra}


def repair_evidence(photos, **extra):
    return {'case': {'id': 'c'}, 'link': {}, 'photos': photos, 'selections': {}, 'values': {}, 'dictionaries': {}, **extra}


def test_repair_dates_use_first_checkin_and_last_finished_photo_only():
    evidence = repair_evidence([
        repair_photo('b', 'work_checkin', '2026-10-16 10:51'), repair_photo('d', 'work_checkin', '2026-10-16 10:54'),
        repair_photo('work', 'construction', '2026-10-16 09:20'), repair_photo('survey', 'survey_checkin', '2026-10-15 09:20'),
        repair_photo('f1', 'completion', '2026-10-16 11:29'), repair_photo('f2', 'completion', '2026-10-16 11:30')])
    suggested, notes = repair_suggestions(**evidence)
    assert suggested['approachStartWorkDate']['value'] == '2026-10-16 10:51:00'
    assert suggested['finishWorkDate']['value'] == '2026-10-16 11:30:00'
    assert not notes and set(suggested) == {'approachStartWorkDate', 'finishWorkDate'}


@pytest.mark.parametrize('change', [{'recognition_state': 'conflict'}, {'case_id': 'other'}, {'media_type': 'video'}, {'watermark_time': None}])
def test_repair_time_cannot_be_inferred_from_invalid_photo_or_import_date(change):
    p = repair_photo('b', 'work_checkin', '2026-10-16 10:51', created_at='2026-10-16 10:51')
    p.update(change)
    suggested, notes = repair_suggestions(**repair_evidence([p]))
    assert not suggested and 'approachStartWorkDate' in notes


def test_repair_multiple_visits_require_selection_and_finished_time_cannot_precede_start():
    photos = [repair_photo('b1', 'work_checkin', '2026-10-15 10:00'), repair_photo('b2', 'work_checkin', '2026-10-16 10:00'),
              repair_photo('f', 'completion', '2026-10-16 09:00')]
    suggested, notes = repair_suggestions(**repair_evidence(photos))
    assert 'approachStartWorkDate' not in suggested and '多个日期' in notes['approachStartWorkDate']
    suggested, notes = repair_suggestions(**repair_evidence(photos, selections={'buildingFileInfoList': ['b2']}))
    assert suggested['approachStartWorkDate']['value'] == '2026-10-16 10:00:00'
    assert 'finishWorkDate' not in suggested and '早于' in notes['finishWorkDate']


def test_explicit_shared_survey_checkin_can_establish_work_start():
    p = repair_photo('b', 'survey_checkin', '2026-10-16 10:51', shared_checkin_visit_id='visit1')
    suggested, _ = repair_suggestions(**repair_evidence([p]))
    assert suggested['approachStartWorkDate']['value'] == '2026-10-16 10:51:00'


def test_auto_field_only_save_is_once_and_never_formally_submits(platform, services, case, photo, monkeypatch):
    auto, _, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    platform.test_survey.clear()
    platform.test_claim['reasonDescription'] = '现场已记录的具体缺陷原因'
    row = tick(auto, case['id'])
    assert row['state'] == 'needs_fields', row
    assert not uploads and len(writes) == 1
    assert writes[0][1]['state'] == 0 and writes[0][1]['defectDesc'] == '现场已记录的具体缺陷原因'
    assert row['detail']['filled_fields'] and row['detail']['field_notes']
    assert row['review_id'] is None
    record = services['reviews'].get(row['save_review_id'])
    assert record['save_only'] and record['state'] == 'save_verified'
    assert auto.run_once() == 0
    tick(auto, case['id'])
    assert len(writes) == 1


def test_automatic_prefill_accepts_only_current_permitted_evidence(platform, services, case, photo, monkeypatch):
    auto, _, _, _, _ = setup_flow(services, platform, case, photo, monkeypatch)
    blank_survey(platform, services, case)
    trusted = confirmed(services, photo, 'red', 'survey_checkin', '2026-10-16 09:00', 'building_checkin')
    untrusted = photo('blue', 'untrusted.png', photo_group='survey_checkin', role='door_checkin', watermark_time='2026-10-15 09:00')
    auto.rules.process(trusted['id'], revisit=True)
    auto.rules.process(untrusted['id'], revisit=True)
    assert auto._prefill_photo_ids(services['domain'].case(case['id'])) == {trusted['id']}
    plan = platform.prefill_draft(case['id'], 'survey', auto._prefill_photo_ids(services['domain'].case(case['id'])))['plan']
    assert plan['values']['siteSurveyDate'] == '2026-10-16 09:00:00'
    # Manual file removal also excludes that photo from automatic field evidence.
    platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', plan['draft']['values'], {}))
    assert auto._prefill_photo_ids(services['domain'].case(case['id'])) == set()


def test_existing_repair_photos_fill_and_save_real_work_times(platform, services, case, photo, monkeypatch):
    auto, _, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    enter_stage(platform, case['id'], 'repair')
    confirmed(services, photo, 'red', 'work_checkin', '2026-10-16 10:51', 'building_checkin')
    confirmed(services, photo, 'blue', 'work_checkin', '2026-10-16 10:54', 'door_checkin')
    confirmed(services, photo, 'green', 'completion', '2026-10-16 11:30', 'finished')
    row = tick(auto, case['id'])
    assert len(uploads) == 3 and len(writes) == 1, row
    assert writes[0][0] == 'repair' and writes[0][1]['state'] == 0
    assert writes[0][1]['approachStartWorkDate'] == '2026-10-16 10:51:00'
    assert writes[0][1]['finishWorkDate'] == '2026-10-16 11:30:00'
    assert writes[0][1]['repairCrewNumber'] == 1
    assert {f['field'] for f in row['detail']['filled_fields']} == {'approachStartWorkDate', 'finishWorkDate'}


def test_revoked_group_cannot_supply_automatic_field_evidence(platform, services, case, photo, monkeypatch, bot):
    auto, _, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    mid = capture(bot, '案件编号：ZLBH20260001', images=1, mention=False)
    bot.collect_messages()
    task = task_for(bot, mid)
    pid = task['input']['photo_ids'][0]
    bot.domain.update_photo(pid, {'case_id': case['id'], 'photo_group': 'survey_checkin', 'role': 'building_checkin', 'watermark_time': '2026-10-16 09:00', 'confirm': True})
    auto.rules.process(pid, revisit=True)
    assert pid in auto._prefill_photo_ids(services['domain'].case(case['id']))
    bot.save_binding(bot.test_connector['id'], {'save_drafts': False})
    assert pid not in auto._prefill_photo_ids(services['domain'].case(case['id']))
    assert not uploads and not writes
