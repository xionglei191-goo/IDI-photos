import pytest

from conftest import image_bytes
from idi.recognition import extract_fields


CODE = 'ZLBH209901010000000001'


def watermark(action='维修', code=CODE, area='C3-2104', community='都会四季'):
    return f'小\n区：{community}\n工程名称：{area}\n时间：2026-10-1609:25:41\n施工内容：卫生间地砖开裂，马桶晃动\n打卡内容：{action}\n案件编号：{code}'


@pytest.fixture
def official_case(services):
    domain = services['domain']
    project = domain.save_project({'name': '金茂万科都会四季C地块'})
    return domain.save_case({'project_id': project['id'], 'title': '卫生间地砖开裂，马桶晃动',
                             'building': 'C3', 'room': '2104', 'platform_report_code': CODE})


@pytest.mark.parametrize('action,group', [('查勘打卡', 'survey_checkin'), ('查勘', 'survey_problem'),
                                       ('维修打卡', 'work_checkin'), ('维修', 'construction'), ('验收', 'completion')])
def test_new_watermark_stage_is_independent_of_defect_description(services, official_case, action, group):
    fields = extract_fields(watermark(action))
    assert fields['watermark_project'] == '都会四季'
    assert fields['watermark_area'] == 'C3-2104'
    assert fields['building'] == 'C3' and fields['room'] == '2104'
    assert fields['watermark_time'] == '2026-10-16 09:25'
    assert fields['process'] == '卫生间地砖开裂，马桶晃动'
    assert fields['checkin_content'] == action and fields['photo_group'] == group
    assert fields['platform_report_code'] == CODE and 'short_code' not in fields
    candidates, assigned = services['recognizer'].match(watermark(action), fields)
    assert assigned == official_case['id'] and not candidates[0]['needs_review']
    if group.endswith('checkin'):
        assert fields['role'] == 'other'  # A written room is not a visible door check-in.


@pytest.mark.parametrize('stamp,expected', [('2026-10-1609:25:41', '2026-10-16 09:25'),
                                        ('2026.9.22 16:50', '2026-09-22 16:50'),
                                        ('2026年9月2日 9:05', '2026-09-02 09:05')])
def test_timestamp_without_address_never_becomes_a_room(stamp, expected):
    fields = extract_fields('时间：' + stamp)
    assert fields['watermark_time'] == expected
    assert not {'building', 'room', 'unit'} & fields.keys()


@pytest.mark.parametrize('label', ['时间', '时#间', '时7间', '时\n间', '时#\n间', '间'])
def test_ocr_label_noise_does_not_pollute_area(label):
    fields = extract_fields(watermark().replace('时间', label))
    assert fields['watermark_area'] == 'C3-2104'
    assert fields['building'] == 'C3' and fields['room'] == '2104'


def test_missing_community_character_and_unknown_stage_preserve_uncertainty():
    fields = extract_fields(watermark('待处理').replace('小\n区', '区'))
    assert fields['watermark_project'] == '都会四季'
    assert 'photo_group' not in fields and 'stage' not in fields


@pytest.mark.parametrize('code', ['ZLBH209901010000000002', 'ZLBH202610132OO5010577'])
def test_unknown_or_misread_explicit_id_never_falls_back_to_same_address(services, official_case, code, monkeypatch):
    recognizer = services['recognizer']
    monkeypatch.setattr(recognizer, 'local_ocr', lambda _: (watermark(code=code), []))
    item = services['importer'].add_file(image_bytes(), 'unknown.png', project_id=official_case['project_id'])
    assert not recognizer.recognize(item['photo_id'])['auto_assigned']
    photo = services['domain'].photo(item['photo_id'])
    assert photo['case_id'] is None and photo['recognition_state'] == 'conflict'


def test_multiple_official_identifiers_require_review(services, official_case):
    text = watermark() + '\n案件编号：ZLBH209901010000000002'
    assert services['recognizer'].match(text, extract_fields(text))[1] is None
    # An external model's guessed field cannot override two visible official IDs.
    assert services['recognizer'].match(text, {**extract_fields(text), 'platform_report_code': CODE})[1] is None


def test_official_id_overrides_project_hints(services, official_case, project):
    recognizer = services['recognizer']
    assert recognizer.match(watermark(), extract_fields(watermark()), project['id'])[1] == official_case['id']
    text = watermark(community='另一花园')
    candidates, assigned = recognizer.match(text, extract_fields(text))
    assert assigned == official_case['id'] and not candidates[0]['needs_review']
    assert any('水印项目' in reason for reason in candidates[0]['reasons'])


@pytest.mark.parametrize('suffix', ['\n编号：WRONG-C3-2104-01',
                                  '\n编号：WRONG-C3-2104-01\n编号：WRONG-C3-2104-02',
                                  '\n报案号：INS000001\n报案号：INS000002'])
def test_unique_official_id_overrides_other_identifier_differences(services, official_case, suffix):
    text = watermark() + suffix
    candidates, assigned = services['recognizer'].match(text, extract_fields(text))
    assert assigned == official_case['id'] and not candidates[0]['needs_review']
    assert any('已按案件编号归案' in reason for reason in candidates[0]['reasons'])


def test_official_identity_wins_over_similar_address_without_rewriting_case(services, official_case, monkeypatch):
    domain, recognizer = services['domain'], services['recognizer']
    # The platform sometimes has placeholder addresses. Keep both sources visible.
    domain.save_case({'building': '1', 'room': '200'}, official_case['id'])
    domain.save_case({'project_id': official_case['project_id'], 'title': official_case['title'],
                     'building': 'C3', 'room': '2104', 'platform_report_code': 'ZLBH209901010000000002'})
    before = services['db'].all('SELECT * FROM cases ORDER BY id')
    monkeypatch.setattr(recognizer, 'local_ocr', lambda _: (watermark('验收'), []))
    item = services['importer'].add_file(image_bytes(), '2025-01-01.png')
    result = recognizer.recognize(item['photo_id'])
    photo = domain.photo(item['photo_id'])
    assert result['auto_assigned'] and photo['case_id'] == official_case['id']
    assert photo['recognition_state'] == 'recognized' and photo['photo_group'] == 'completion'
    assert photo['watermark_time'] == '2026-10-16 09:25'
    assert not result['candidates'][0]['needs_review']
    assert any('水印：2104；台账：200' in r for r in result['candidates'][0]['reasons'])
    assert services['db'].all('SELECT * FROM cases ORDER BY id') == before
    item = next(item for item in services['workflow'].board(source='local')['items'] if item['id'] == official_case['id'])
    assert item['photo_counts']['completion'] == 1 and item['exception_count'] == 0


def test_unassigned_photo_moves_from_import_project_to_official_case(services, official_case, project, monkeypatch):
    recognizer, domain = services['recognizer'], services['domain']
    monkeypatch.setattr(recognizer, 'local_ocr', lambda _: (watermark(community=project['name']), []))
    item = services['importer'].add_file(image_bytes(), 'wrong-project.png', project_id=project['id'])
    assert recognizer.recognize(item['photo_id'])['auto_assigned']
    photo = domain.photo(item['photo_id'])
    assert photo['case_id'] == official_case['id'] and photo['project_id'] == official_case['project_id']
    assert photo['recognition_state'] == 'recognized'


def test_existing_assignment_and_manual_confirmation_survive(services, official_case, case, monkeypatch):
    recognizer, domain = services['recognizer'], services['domain']
    monkeypatch.setattr(recognizer, 'local_ocr', lambda _: (watermark(), []))
    item = services['importer'].add_file(image_bytes(), 'existing.png', case_id=case['id'])
    recognizer.recognize(item['photo_id'])
    photo = domain.photo(item['photo_id'])
    assert photo['case_id'] == case['id'] and photo['recognition_state'] == 'conflict'
    domain.update_photo(photo['id'], {'photo_group': 'survey_problem', 'process': '人工核对内容', 'confirm': True})
    recognizer.recognize(photo['id'])
    photo = domain.photo(photo['id'])
    assert photo['case_id'] == case['id'] and photo['recognition_state'] == 'confirmed'
    assert photo['photo_group'] == 'survey_problem' and photo['process'] == '人工核对内容'


def test_conflicting_source_context_cannot_override_photo_id(services, official_case, monkeypatch):
    recognizer = services['recognizer']
    monkeypatch.setattr(recognizer, 'local_ocr', lambda _: (watermark(), []))
    item = services['importer'].add_file(image_bytes(), 'context.png', metadata={'context_text': '案件编号：ZLBH209901010000000002'})
    assert not recognizer.recognize(item['photo_id'])['auto_assigned']
    assert services['domain'].photo(item['photo_id'])['recognition_state'] == 'conflict'
