import pytest

from conftest import image_bytes
from idi.recognition import extract_checkin_role, extract_fields


CODE = 'ZLBH209901010000000001'


def line(text, y, confidence=.99):
    return {'text': text, 'confidence': confidence, 'box': [[20, y], [260, y], [260, y + 25], [20, y + 25]]}


def evidence(signs=(), action='查勘打卡'):
    labels = ['小区：都会四季', '工程名称：C3-2104', '时间：2026-10-16 09:25:41',
              '施工内容：马桶晃动', '打卡内容：' + action, '案件编号：' + CODE]
    lines = [*signs, *[line(text, 800 + i * 30) for i, text in enumerate(labels)]]
    return '\n'.join(r['text'] for r in lines), lines


@pytest.mark.parametrize('action', ['查勘打卡', '维修打卡'])
@pytest.mark.parametrize('sign,role', [('C3', 'building_checkin'), ('c3栋', 'building_checkin'),
                                     ('2104', 'door_checkin'), ('2104室', 'door_checkin')])
def test_separate_legible_sign_identifies_checkin_location(action, sign, role):
    text, lines = evidence([line(sign, 100)], action)
    fields = extract_fields(text)
    result = extract_checkin_role(fields, lines)
    assert result['role'] == role
    assert result['checkin_role_evidence']['signs'][0]['text'] == sign
    assert fields['role'] == 'other'  # Plain text alone still carries no visual position.


@pytest.mark.parametrize('signs', [[], [line('2104', 830)], [line('2104', 760)],
                                  [line('2104', 100, .89)], [line('2105', 100)],
                                  [line('C3-2104', 100)], [line('C3', 100), line('2104', 200)],
                                  [{'text': '2104', 'confidence': .99}],
                                  [{'text': '2104', 'confidence': .99, 'box': [[0, 0]] * 4}]])
def test_watermark_values_missing_geometry_or_ambiguous_signs_remain_unclassified(signs):
    text, lines = evidence(signs)
    assert extract_checkin_role(extract_fields(text), lines) == {}


def test_inference_requires_checkin_and_watermark_location_and_preserves_explicit_role():
    text, lines = evidence([line('2104', 100)])
    fields = extract_fields(text)
    for changes in ({'photo_group': 'survey_problem'}, {'watermark_area': ''}, {'room': ''},
                    {'role': 'building_checkin'}):
        assert extract_checkin_role({**fields, **changes}, lines) == {}
    assert extract_checkin_role(fields, lines[:2]) == {}


def test_ocr_keeps_geometry(services):
    recognizer = services['recognizer']
    box = [[20., 100.], [260., 100.], [260., 125.], [20., 125.]]
    recognizer._engine = lambda _: ([[box, '2104', .99123]], None)
    text, lines = recognizer.local_ocr('unused.png')
    assert text == '2104' and lines == [{'text': '2104', 'confidence': .9912, 'box': box}]


def test_recognized_signs_populate_correct_stage_fields_with_official_id_priority(services, case, monkeypatch):
    domain, recognizer, platform = services['domain'], services['recognizer'], services['platform']
    domain.save_case({'platform_report_code': CODE, 'building': '1', 'room': '200'}, case['id'])
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    ids = {}
    for sign, role, color, action in [('C3', 'building', 'navy', '查勘打卡'),
                                    ('2104', 'door', 'green', '查勘打卡'),
                                    ('2104', 'work', 'red', '维修打卡')]:
        monkeypatch.setattr(recognizer, 'local_ocr', lambda _, s=sign, a=action: evidence([line(s, 100)], a))
        result = services['importer'].add_file(image_bytes(color), role + '.png')
        photo_id = ids[role] = result['photo_id']
        recognizer.recognize(photo_id)
        photo = domain.photo(photo_id)
        assert photo['case_id'] == case['id'] and photo['recognition_state'] == 'recognized'
        assert photo['recognitions'][0]['suggestions']['fields']['checkin_role_evidence']['signs'][0]['text'] == sign
    plan = platform.preview(case['id'], 'survey')
    fields = {f['key']: f for f in plan['file_fields']}
    assert fields['buildingFileInfoList']['suggested_ids'] == [ids['building']]
    assert fields['houseFileInfoList']['suggested_ids'] == [ids['door']]
    assert plan['draft']['files']['buildingFileInfoList'] == [ids['building']]
    assert plan['draft']['files']['houseFileInfoList'] == [ids['door']]
    assert ids['work'] not in plan['draft']['files']['houseFileInfoList']
    assert not plan['write_enabled']


def test_manual_checkin_location_and_saved_draft_are_not_overwritten(services, case, monkeypatch):
    domain, recognizer, platform = services['domain'], services['recognizer'], services['platform']
    domain.save_case({'platform_report_code': CODE}, case['id'])
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    item = services['importer'].add_file(image_bytes(), 'door.png', case_id=case['id'])
    domain.update_photo(item['photo_id'], {'photo_group': 'survey_checkin', 'role': 'building_checkin', 'confirm': True})
    platform.save_draft(case['id'], 'survey', {'values': {'defectDesc': '保留人工内容'},
                        'files': {'buildingFileInfoList': [], 'houseFileInfoList': []}, 'revision': 0, 'link_revision': 0})
    before = platform.draft(case['id'], 'survey')
    monkeypatch.setattr(recognizer, 'local_ocr', lambda _: evidence([line('2104', 100)]))
    recognizer.recognize(item['photo_id'])
    photo = domain.photo(item['photo_id'])
    assert photo['role'] == 'building_checkin' and photo['recognition_state'] == 'confirmed'
    assert platform.draft(case['id'], 'survey') == before
