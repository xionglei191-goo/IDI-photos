import pytest

from idi.checkin_copies import migrate_legacy_copies, valid_work_copy, verified_copy_receipts
from idi.db import now
from tests.conftest import image_bytes
from tests.test_manual_file_selections import repair_flow, choose
from tests.test_platform_preparation import platform, enter_stage
from tests.test_automatic_uploads import tick
from tests.test_visit_records import entry


def reuse(services, case, original):
    return services['domain'].reuse_checkin(original['id'], {
        'case_id': case['id'], 'revision': original['revision'], 'repair_date': '2026-10-16'})


def test_reuse_is_idempotent_with_separate_metadata_and_unchanged_original(client, services, case, photo):
    original = photo(stage='survey', role='building_checkin', watermark_time='2026-10-16 10:08')
    db, domain = services['db'], services['domain']
    before = db.one('SELECT * FROM photos WHERE id=?', (original['id'],))
    path = f"/api/photos/{original['id']}/reuse-checkin"
    body = {'case_id': case['id'], 'revision': original['revision'], 'repair_date': '2026-10-16'}
    first = client.post(path, json=body)
    assert first.status_code == 200, first.text
    copy = first.json()['photo']
    assert first.json()['created'] and copy['id'] != original['id']
    assert valid_work_copy(copy) and copy['reuse_source_exists']
    assert (copy['stage'], copy['photo_group'], copy['role']) == ('work', 'work_checkin', 'building_checkin')
    assert client.get(copy['original_url']).content == client.get(original['original_url']).content
    second = client.post(path, json=body).json()
    assert not second['created'] and second['photo']['id'] == copy['id']
    domain.update_photo(copy['id'], {'notes': '维修副本单独备注', 'part': '维修位置'})
    assert db.one('SELECT * FROM photos WHERE id=?', (original['id'],)) == before
    domain.update_photo(original['id'], {'notes': '查勘备注', 'watermark_time': '2026-10-17 11:00'})
    current = domain.photo(copy['id'])
    assert current['notes'] == '维修副本单独备注' and current['watermark_time'] == '2026-10-16 10:08'
    assert current['reuse_metadata']['source']['revision'] == original['revision']
    assert domain.photos(case_id=case['id'])['total'] == 2
    imported = services['importer'].add_file(image_bytes(), '再次导入.png', case_id=case['id'])
    assert imported['photo_id'] == original['id']


@pytest.mark.parametrize('which', ['original', 'copy'])
def test_deleting_either_record_keeps_the_other_and_does_not_restore_virtual_reuse(
        client, services, case, photo, which):
    original = photo(stage='survey', role='building_checkin', watermark_time='2026-10-16 10:08')
    visit = services['domain'].save_visit(case['id'], entry(shared_checkin_ids=[original['id']]))
    copy = services['domain'].photo(visit['copied_checkin_ids'][0])
    deleted, kept = (original, copy) if which == 'original' else (copy, original)
    assert client.delete(f"/api/photos/{deleted['id']}?revision={deleted['revision']}").status_code == 200
    current = services['domain'].photo(kept['id'])
    assert current['stage'] == kept['stage'] and current['revision'] == kept['revision']
    assert client.get(kept['original_url']).content == image_bytes()
    assert services['domain'].photos(case_id=case['id'], photo_group='work_checkin')['total'] == (which == 'original')
    assert not services['db'].all('SELECT * FROM visit_shared_checkins')
    if which == 'original':
        assert valid_work_copy(current) and not current['reuse_source_exists']
    assert services['db'].all('PRAGMA foreign_key_check') == []


def test_detaching_work_copy_clears_only_its_entry_relationship(services, case, photo):
    original = photo(stage='survey', role='building_checkin', watermark_time='2026-10-16 10:08')
    domain = services['domain']
    visit = domain.save_visit(case['id'], entry(shared_checkin_ids=[original['id']]))
    domain.update_photo(visit['copied_checkin_ids'][0], {'visit_id': None})
    current = domain.visits(case['id'])[0]
    assert current['counts']['building_checkin'] == 0 and not current['shared_checkin_ids']
    assert domain.photo(original['id'])['revision'] == original['revision']
    assert domain.photos(case_id=case['id'], photo_group='work_checkin')['total'] == 1


def test_moving_original_to_another_case_keeps_copy_visible_in_visit(services, case, photo):
    original = photo(stage='survey', role='building_checkin', watermark_time='2026-10-16 10:08')
    domain = services['domain']
    visit = domain.save_visit(case['id'], entry(shared_checkin_ids=[original['id']]))
    other = domain.save_case({'project_id': case['project_id'], 'title': '另案'})
    domain.update_photo(original['id'], {'case_id': other['id']})
    current = domain.visits(case['id'])[0]
    assert current['photo_ids'] == visit['copied_checkin_ids'] and not current['shared_checkin_ids']
    assert current['counts']['building_checkin'] == 1
    domain.save_visit(case['id'], {**current, 'same_day_repair': True}, visit['id'])


@pytest.mark.parametrize('invalid', ['stale', 'case', 'date', 'role', 'conflict'])
def test_invalid_copy_request_is_atomic(client, services, case, photo, invalid):
    original = photo(stage='survey', role='building_checkin', watermark_time='2026-10-16 10:08')
    body = {'case_id': case['id'], 'revision': original['revision'], 'repair_date': '2026-10-16'}
    if invalid == 'stale':
        body['revision'] += 1
    elif invalid == 'case':
        body['case_id'] = 'another-case'
    elif invalid == 'date':
        body['repair_date'] = '2026-10-17'
    else:
        with services['db'].tx() as con:
            if invalid == 'role':
                con.execute("UPDATE photos SET role='problem' WHERE id=?", (original['id'],))
            else:
                con.execute("UPDATE photos SET recognition_state='conflict' WHERE id=?", (original['id'],))
    result = client.post(f"/api/photos/{original['id']}/reuse-checkin", json=body)
    assert result.status_code in (400, 409)
    assert services['domain'].photos(case_id=case['id'])['total'] == 1


def test_source_ocr_retained_without_purpose_conflict_but_opposite_role_still_blocks(
        platform, services, case, photo, monkeypatch):
    enter_stage(platform, case['id'], 'repair')
    original = photo(stage='survey', role='building_checkin')
    raw = '案件编号：ZLBH20260001\n时间：2026-10-16 10:08:00\n打卡内容：查勘打卡'
    monkeypatch.setattr(services['recognizer'], 'local_ocr', lambda path: (raw, []))
    services['recognizer'].recognize(original['id'])
    original = services['domain'].update_photo(original['id'], {
        'photo_group': 'survey_checkin', 'role': 'building_checkin', 'confirm': True})
    copy = reuse(services, case, original)['photo']
    assert copy['recognitions'][0]['raw_text'] == raw
    assert services['photo_workflow'].process(copy['id'])['state'] == 'local_draft'
    services['recognizer'].recognize(copy['id'])
    current = services['domain'].photo(copy['id'])
    assert current['photo_group'] == 'work_checkin' and current['recognition_state'] != 'conflict'
    fields = {'stage': 'survey', 'photo_group': 'survey_checkin', 'role': 'door_checkin'}
    assert services['photo_workflow'].purpose_conflicts(current, fields, set(current['confirmed_fields'])) == {'role'}


def test_original_changes_do_not_invalidate_selected_work_copy(platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    original = add('red', 'survey', 'building_checkin')
    plan = choose(platform, case, {'buildingFileInfoList': [original['id']]})
    copy_id = plan['draft']['files']['buildingFileInfoList'][0]
    services['domain'].update_photo(original['id'], {'part': '查勘后修改'})
    state = tick(auto, case['id'])
    assert state['state'] == 'needs_fields' and len(uploads) == len(writes) == 1
    assert copy_id in services['reviews'].get(state['save_review_id'])['result']['uploaded']


def test_old_repair_receipt_prevents_reupload_after_copy_and_is_field_scoped(
        platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    original = add('red', 'survey', 'building_checkin')
    # Reproduce a pre-migration, explicitly selected original already saved to
    # the repair field using the normal upload and verified readback.
    plan = choose(platform, case, {'buildingFileInfoList': [original['id']]}, actor='legacy_test')
    reviews = services['reviews']
    record = reviews.prepare(case['id'], 'repair', plan['draft']['revision'], save_only=True)
    saved = reviews.save_for_agent(record['id'], record['fingerprint'])
    assert saved['state'] == 'save_verified' and len(uploads) == len(writes) == 1
    copy = services['domain'].reuse_checkin(original['id'], {
        'case_id': case['id'], 'revision': original['revision']})['photo']
    assert verified_copy_receipts(platform, case['id'], 'repair')[copy['id']]['id'] == 'auto-1'
    assert not verified_copy_receipts(platform, case['id'], 'survey')
    tick(auto, case['id'])
    assert len(uploads) == len(writes) == 1
    assert services['photo_workflow'].process(copy['id'])['state'] == 'already_saved'
    # Explicitly picking it again may save a new draft, but cannot upload the
    # image again or duplicate the existing official attachment.
    plan = choose(platform, case, {'buildingFileInfoList': [copy['id']]})
    assert reviews._verified_uploads(plan)[copy['id']]['id'] == 'auto-1'
    record = reviews.prepare(case['id'], 'repair', plan['draft']['revision'], save_only=True)
    saved = reviews.save_for_agent(record['id'], record['fingerprint'])
    assert saved['state'] == 'save_verified' and len(uploads) == 1
    assert len(platform.test_repair['buildingFileInfoList']) == 1
    link = platform.link(case['id'])
    link['snapshot']['repair']['buildingFileInfoList'] = []
    link['snapshot']['repair']['houseFileInfoList'] = [saved['result']['uploaded'][copy['id']]]
    assert not verified_copy_receipts(platform, case['id'], 'repair', link)
    link = platform.link(case['id'])
    link['report_receiving_id'] = 'wrong-target'
    assert not verified_copy_receipts(platform, case['id'], 'repair', link)


def test_migration_converts_legacy_shared_and_manual_records_once(platform, services, case, photo):
    enter_stage(platform, case['id'], 'repair')
    originals = [
        photo('red', stage='survey', role='building_checkin', watermark_time='2026-10-16 10:08'),
        photo('blue', stage='survey', role='door_checkin', watermark_time='2026-10-16 10:09'),
    ]
    before = {p['id']: services['db'].one('SELECT * FROM photos WHERE id=?', (p['id'],)) for p in originals}
    plan = choose(platform, case, {'houseFileInfoList': [originals[1]['id']]}, actor='legacy_test')
    with services['db'].tx() as con:
        con.execute("INSERT INTO visits(id,case_id,stage,visit_date,created_at,same_day_repair) VALUES('legacy',?,'work','2026-10-16',?,1)", (case['id'], now()))
        con.execute("INSERT INTO visit_shared_checkins VALUES(?,'legacy',?)", (originals[0]['id'], now()))
        con.execute("INSERT INTO platform_manual_file_selections VALUES(?,'repair','houseFileInfoList',?,?,?,?,?)",
                    (case['id'], originals[1]['id'], originals[1]['revision'], originals[1]['blob_hash'],
                     plan['link']['report_receiving_id'], now()))
        migrate_legacy_copies(con)
        migrate_legacy_copies(con)
    copies = services['domain'].photos(case_id=case['id'], photo_group='work_checkin')['items']
    assert len(copies) == 2 and all(valid_work_copy(p) for p in copies)
    for original in originals:
        assert services['db'].one('SELECT * FROM photos WHERE id=?', (original['id'],)) == before[original['id']]
    assert originals[1]['id'] not in platform.draft(case['id'], 'repair')['files']['houseFileInfoList']
    assert services['db'].all('PRAGMA foreign_key_check') == []
