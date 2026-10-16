from copy import deepcopy
import json
from pathlib import Path

import pytest

from idi.platform_prefill import survey_suggestions


def photo(pid, group, stamp, **extra):
    return {'id': pid, 'case_id': 'case1', 'media_type': 'image', 'photo_group': group,
            'recognition_state': 'recognized', 'watermark_time': stamp, 'original_name': pid + '.jpg',
            'process': '马桶晃动', 'part': '', **extra}


@pytest.fixture
def evidence():
    dictionaries = json.loads((Path(__file__).parents[1] / 'idi/platform_dictionary.json').read_text(encoding='utf-8'))['items']
    return {'case': {'id': 'case1', 'title': '主卧马桶地面连接处开胶 马桶晃动', 'repair_parts': []},
            'link': {'verified_at': '2026-10-16', 'snapshot': {
                'claim': {'reasonDescription': '施工时保护不到位、操作不当造成', 'issueDescription': '卫生间-防水渗漏问题'},
                'survey': {}, 'personnel': [{'userId': 'person1', 'name': '甲', 'phone': '123'}]}},
            'photos': [photo('building', 'survey_checkin', '2026-10-16 09:25'),
                       photo('door', 'survey_checkin', '2026-10-16 09:29'),
                       photo('problem', 'survey_problem', '2026-10-16 09:30'),
                       photo('work', 'construction', '2026-10-16 09:31'),
                       photo('finished', 'completion', '2026-10-16 09:47')],
            'selections': {'buildingFileInfoList': ['building'], 'houseFileInfoList': ['door'], 'fileInfoList': ['problem']},
            'values': {}, 'dictionaries': dictionaries}


def test_realistic_case_suggestions_have_sources_and_do_not_mutate_inputs(evidence):
    before = deepcopy(evidence)
    suggested, notes = survey_suggestions(**evidence)
    assert {k: v['value'] for k, v in suggested.items()} == {
        'siteSurveyDate': '2026-10-16 09:25', 'defectDesc': '施工时保护不到位、操作不当造成',
        'defectiveSite': '主卧马桶地面连接处', 'divisionWorksCode': 'part_engineering_04',
        'defectTypeCode': 'defect_type_other', 'repairMethod': '1', 'planRepairDate': '2026-10-16'}
    assert suggested['siteSurveyDate']['photo_ids'] == ['building']
    assert suggested['defectDesc']['needs_review'] and '受理' in suggested['defectDesc']['source']
    assert all(v['source'] for v in suggested.values())
    assert not notes and evidence == before
    # A sole personnel option and a low cost threshold do not establish actual staff or cost.
    assert not {'repairUserId', 'estimateRestorationCostCode'} & suggested.keys()


def test_multiple_visits_require_explicit_selection(evidence):
    evidence['photos'].append(photo('previous', 'survey_checkin', '2026-10-15 08:00'))
    selected, _ = survey_suggestions(**evidence)
    assert selected['siteSurveyDate']['value'] == '2026-10-16 09:25'
    evidence['selections'] = {}
    mixed, notes = survey_suggestions(**evidence)
    assert 'siteSurveyDate' not in mixed and 'repairMethod' not in mixed
    assert '多个日期' in notes['siteSurveyDate']


@pytest.mark.parametrize('change', [{'recognition_state': 'conflict'}, {'recognition_state': 'failed'},
                                  {'case_id': 'another'}, {'media_type': 'video'}, {'media_type': 'document'}])
def test_uncertain_foreign_and_non_image_files_are_not_time_evidence(evidence, change):
    evidence['photos'] = [photo('building', 'survey_checkin', '2026-10-16 09:25', **change)]
    suggested, _ = survey_suggestions(**evidence)
    assert 'siteSurveyDate' not in suggested and 'repairMethod' not in suggested


@pytest.mark.parametrize('stamp', [None, '', '2026-10-16', '2026-99-99 09:25', '2026-10-16T09:25+08:00'])
def test_only_a_valid_watermark_time_can_supply_survey_time(evidence, stamp):
    evidence['photos'] = [photo('building', 'survey_checkin', stamp, exif_time='2026-10-16 09:25',
                                created_at='2026-10-16 09:25', original_name='2026-10-16_09-25.jpg')]
    evidence['link']['snapshot']['claim'].update(modifiedSurveyTime='2026-10-16 09:25', createTime='2026-10-16 09:25')
    suggested, _ = survey_suggestions(**evidence)
    assert 'siteSurveyDate' not in suggested


def test_defect_symptoms_are_not_a_cause_and_unverified_link_is_not_reused(evidence):
    evidence['link']['snapshot']['claim'].pop('reasonDescription')
    suggested, notes = survey_suggestions(**evidence)
    assert 'defectDesc' not in suggested and '现场检查' in notes['defectDesc']
    evidence['link']['snapshot']['claim']['reasonDescription'] = '另一条原因说明'
    evidence['link']['verified_at'] = None
    assert 'defectDesc' not in survey_suggestions(**evidence)[0]


@pytest.mark.parametrize('work_time,finished_time', [('2026-10-17 09:31', '2026-10-17 09:47'),
                                                  ('2026-10-16 08:00', '2026-10-16 09:47'),
                                                  ('2026-10-16 09:31', '2026-10-17 09:47'),
                                                  (None, '2026-10-16 09:47'),
                                                  ('2026-10-16 09:31', None)])
def test_repair_method_requires_a_consistent_same_day_work_timeline(evidence, work_time, finished_time):
    evidence['photos'][-2]['watermark_time'] = work_time
    evidence['photos'][-1]['watermark_time'] = finished_time
    suggested, _ = survey_suggestions(**evidence)
    assert 'repairMethod' not in suggested and 'planRepairDate' not in suggested


def test_completion_alone_does_not_establish_repair_method_or_acceptance(evidence):
    evidence['photos'] = [p for p in evidence['photos'] if p['photo_group'] != 'construction']
    suggested, _ = survey_suggestions(**evidence)
    assert 'repairMethod' not in suggested and 'acceptanceDate' not in suggested


def test_manual_date_and_method_control_dependent_date_suggestion(evidence):
    evidence['values'] = {'siteSurveyDate': '2026-10-15 09:25'}
    assert 'repairMethod' not in survey_suggestions(**evidence)[0]
    evidence['values'] = {'repairMethod': '2'}
    suggested, _ = survey_suggestions(**evidence)
    assert 'planRepairDate' not in suggested
    assert evidence['values'] == {'repairMethod': '2'}


def test_ambiguous_categories_are_not_guessed(evidence):
    evidence['case']['title'] = '卫生间地砖开裂，马桶晃动'
    suggested, notes = survey_suggestions(**evidence)
    assert 'divisionWorksCode' not in suggested and 'defectTypeCode' not in suggested
    assert '装饰装修工程' in notes['divisionWorksCode'] and '建筑给水排水工程' in notes['divisionWorksCode']
    assert '裂' in notes['defectTypeCode'] and '其他' in notes['defectTypeCode']


def test_options_use_current_dictionary_codes_and_never_guess_unknown_type(evidence):
    evidence['dictionaries']['part_engineering'] = [{'code': 'new-plumbing-id', 'name': '建筑给水排水工程'}]
    assert survey_suggestions(**evidence)[0]['divisionWorksCode']['value'] == 'new-plumbing-id'
    evidence['dictionaries']['part_engineering'] = []
    assert 'divisionWorksCode' not in survey_suggestions(**evidence)[0]
    evidence['case']['title'] = '需要现场检查'
    for p in evidence['photos']:
        p['process'] = ''
    suggested, _ = survey_suggestions(**evidence)
    assert 'defectTypeCode' not in suggested and 'defectiveSite' not in suggested


def test_explicit_photo_part_and_saved_survey_fields_have_priority(evidence):
    evidence['photos'][2]['part'] = '主卧卫生间马桶底座'
    evidence['link']['snapshot']['survey'] = {'siteSurveyDate': '2026-10-16 09:28:00',
        'defectDesc': '查勘已记录的具体原因', 'divisionWorksCode': 'part_engineering_03',
        'defectTypeCode': 'defect_type_03', 'repairUserId': 'unavailable-person'}
    suggested, _ = survey_suggestions(**evidence)
    assert suggested['defectiveSite']['value'] == '主卧卫生间马桶底座'
    assert suggested['siteSurveyDate']['value'] == '2026-10-16 09:28:00'
    assert suggested['defectDesc']['value'] == '查勘已记录的具体原因'
    assert suggested['divisionWorksCode']['value'] == 'part_engineering_03'
    assert suggested['defectTypeCode']['value'] == 'defect_type_03'
    assert not suggested['defectDesc']['needs_review'] and 'repairUserId' not in suggested


def test_plan_exposes_suggestions_without_writing_draft_or_bypassing_stage_lock(services, case, photo):
    domain, platform, db = services['domain'], services['platform'], services['db']
    domain.save_case({'title': '主卧马桶地面连接处开胶 马桶晃动'}, case['id'])
    p = photo()
    domain.update_photo(p['id'], {'photo_group': 'survey_checkin', 'role': 'building_checkin',
                                'watermark_time': '2026-10-16 09:25', 'confirm': True})
    with db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    before = db.all('SELECT * FROM platform_drafts')
    plan = platform.preview(case['id'], 'survey')
    assert plan['field_suggestions']['siteSurveyDate']['value'] == '2026-10-16 09:25'
    assert plan['draft']['values'] == {} and db.all('SELECT * FROM platform_drafts') == before
    assert not plan['write_enabled']
    with db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair' WHERE id=?", (case['id'],))
    locked = platform.preview(case['id'], 'survey')
    assert not locked['access']['editable'] and locked['field_suggestions'] == {}
    assert platform.preview(case['id'], 'repair')['field_suggestions'] == {}
