from pathlib import Path

import pytest

from idi.domain import DomainError
from tests.test_automatic_uploads import setup_flow, tick
from tests.test_platform_preparation import platform, enter_stage, draft_data, remote_file
from tests.test_feishu_agent import bot, capture, task_for


def repair_flow(services, platform, case, photo, monkeypatch):
    flow = setup_flow(services, platform, case, photo, monkeypatch)
    platform.test_repair.update(
        approachStartWorkDate='2026-10-16 10:15:00',
        finishWorkDate='2026-10-16 10:34:00', repairCrewNumber=1,
        recoveredFileInfo=[remote_file(f'finished-{i}') for i in range(5)])
    enter_stage(platform, case['id'], 'repair')
    return flow


def choose(platform, case, files, *, actor='local'):
    return platform.save_draft(case['id'], 'repair',
                               draft_data(platform, case['id'], 'repair', files=files), actor=actor)


def test_manual_survey_checkins_upload_as_work_copies_without_changing_originals(
        platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    building = add('red', 'survey', 'building_checkin')
    door = add('blue', 'survey', 'door_checkin')
    # A stage mismatch alone must never automatically choose historical photos.
    tick(auto, case['id'])
    assert not uploads and not writes
    choose(platform, case, {'buildingFileInfoList': [building['id']], 'houseFileInfoList': [door['id']]})
    row = tick(auto, case['id'])
    assert row['state'] == 'awaiting_confirmation', row
    assert len(uploads) == 2 and len(writes) == 1
    stage, payload = writes[0]
    assert stage == 'repair' and payload['state'] == 0
    assert len(payload['buildingFileInfoList']) == len(payload['houseFileInfoList']) == 1
    assert {f['id'] for f in payload['recoveredFileInfo']} == {f'finished-{i}' for i in range(5)}
    assert [f['id'] for f in payload['preRepairFileInfo']] == ['defect']
    assert services['reviews'].get(row['save_review_id'])['state'] == 'save_verified'
    for original in (building, door):
        current = services['domain'].photo(original['id'])
        assert (current['stage'], current['role'], current['revision']) == (
            original['stage'], original['role'], original['revision'])
    assert not platform.manual_file_selections(case['id'], 'repair')
    assert not any(platform.draft(case['id'], 'repair')['files'].values())
    tick(auto, case['id'])
    assert len(uploads) == 2 and len(writes) == 1


def test_automatic_choice_does_not_grant_a_cross_stage_override(
        platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    picture = add('red', 'survey', 'building_checkin')
    choose(platform, case, {'buildingFileInfoList': [picture['id']]}, actor='rule')
    row = tick(auto, case['id'])
    assert row['state'] == 'needs_check' and '重新选择' in row['detail']['reason']
    assert not uploads and not writes


def test_automatic_field_refresh_preserves_the_existing_manual_choice(
        platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    picture = add('red', 'survey', 'building_checkin')
    files = {'buildingFileInfoList': [picture['id']]}
    plan = choose(platform, case, files)
    choose(platform, case, plan['draft']['files'], actor='automatic_prefill')
    row = tick(auto, case['id'])
    assert row['state'] == 'needs_fields' and len(uploads) == 1 and len(writes) == 1


def test_changed_photo_requires_a_new_manual_choice(
        platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    picture = add('red', 'survey', 'problem')
    files = {'buildingFileInfoList': [picture['id']]}
    choose(platform, case, files)
    services['domain'].update_photo(picture['id'], {'part': '已核对后的部位'})
    row = tick(auto, case['id'])
    assert row['state'] == 'needs_check' and not uploads and not writes
    choose(platform, case, files)
    row = tick(auto, case['id'])
    assert row['state'] == 'needs_fields' and len(uploads) == 1 and len(writes) == 1


@pytest.mark.parametrize('change', ['field', 'target'])
def test_manual_choice_does_not_cover_another_field_or_official_case(
        platform, services, case, photo, monkeypatch, change):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    picture = add('red', 'survey', 'problem')
    files = {'buildingFileInfoList': [picture['id']]}
    choose(platform, case, files)
    services['photo_workflow'].process(picture['id'], revisit=True)
    if change == 'field':
        choose(platform, case, {'houseFileInfoList': [picture['id']]}, actor='automatic_prefill')
    else:
        with services['db'].tx() as con:
            con.execute("UPDATE platform_links SET report_receiving_id='different-target' WHERE case_id=?", (case['id'],))
    with pytest.raises(DomainError, match='重新选择'):
        auto._permitted(case['id'], {picture['id']})
    assert not uploads and not writes


def test_removing_choice_clears_manual_permission_and_does_not_readd_photo(
        platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    picture = add('red', 'survey', 'building_checkin')
    plan = choose(platform, case, {'buildingFileInfoList': [picture['id']]})
    platform.removals.remove_local(case['id'], 'repair', 'buildingFileInfoList', plan['draft']['files']['buildingFileInfoList'][0],
                                  plan['draft']['revision'])
    assert not platform.manual_file_selections(case['id'], 'repair')
    tick(auto, case['id'])
    assert not uploads and not writes


@pytest.mark.parametrize('invalid', ['unverified', 'dirty', 'conflict', 'original'])
def test_manual_choice_keeps_identity_conflict_and_original_checks(
        platform, services, case, photo, monkeypatch, invalid):
    auto, add, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    picture = add('red', 'survey', 'building_checkin')
    plan = choose(platform, case, {'buildingFileInfoList': [picture['id']]})
    picture = services['domain'].photo(plan['draft']['files']['buildingFileInfoList'][0])
    services['photo_workflow'].process(picture['id'], revisit=True)
    with services['db'].tx() as con:
        if invalid == 'unverified':
            con.execute("UPDATE photo_pipeline SET identity='unverified' WHERE photo_id=?", (picture['id'],))
        elif invalid == 'dirty':
            con.execute('UPDATE photo_pipeline SET dirty=1 WHERE photo_id=?', (picture['id'],))
        elif invalid == 'conflict':
            con.execute("UPDATE photo_pipeline SET state='needs_check' WHERE photo_id=?", (picture['id'],))
    if invalid == 'original':
        monkeypatch.setattr(platform.storage, 'verify', lambda p: False)
    with pytest.raises(DomainError, match='未核对|原件'):
        auto._permitted(case['id'], {picture['id']})
    assert not uploads and not writes


def test_manual_choice_keeps_original_group_saving_permission(
        platform, services, case, photo, monkeypatch, bot):
    auto, _, uploads, writes, _ = repair_flow(services, platform, case, photo, monkeypatch)
    mid = capture(bot, '案件编号：ZLBH20260001', images=1, mention=False)
    bot.collect_messages()
    task = task_for(bot, mid)
    picture = bot.domain.update_photo(task['input']['photo_ids'][0], {
        'case_id': case['id'], 'photo_group': 'survey_checkin', 'role': 'building_checkin', 'confirm': True})
    plan = plan = choose(platform, case, {'buildingFileInfoList': [picture['id']]})
    picture = services['domain'].photo(plan['draft']['files']['buildingFileInfoList'][0])
    picture = services['domain'].photo(plan['draft']['files']['buildingFileInfoList'][0])
    services['photo_workflow'].process(picture['id'], revisit=True)
    pipeline = services['db'].one('SELECT * FROM photo_pipeline WHERE photo_id=?', (picture['id'],))
    assert pipeline['state'] == 'local_draft' and pipeline['identity'] != 'unverified'
    bot.save_binding(bot.test_connector['id'], {'save_drafts': False})
    # Updating the binding cancels its outstanding task; that task's old
    # permission must not survive merely because a person selected the photo.
    with pytest.raises(DomainError, match='群照片缺少有效的原收图任务授权'):
        auto._permitted(case['id'], {picture['id']})
    assert not uploads and not writes


@pytest.mark.parametrize('change', ['none', 'photo', 'automatic_draft'])
def test_migration_only_recovers_unchanged_current_manual_choices(
        platform, services, case, photo, monkeypatch, change):
    _, add, _, _, _ = repair_flow(services, platform, case, photo, monkeypatch)
    picture = add('red', 'survey', 'building_checkin')
    files = {'buildingFileInfoList': [picture['id']]}
    plan = choose(platform, case, files)
    files = plan['draft']['files']
    picture = services['domain'].photo(files['buildingFileInfoList'][0])
    if change == 'photo':
        # Deterministic timestamp comparison for a photo changed after its save.
        with services['db'].tx() as con:
            con.execute("UPDATE photos SET revision=revision+1,updated_at='2099-01-01T00:00:00+00:00' WHERE id=?", (picture['id'],))
    if change == 'automatic_draft':
        choose(platform, case, files, actor='automatic_prefill')
    migration = Path(__file__).parents[1] / 'idi/migrations/029_manual_file_selections.sql'
    with services['db'].read() as con:
        con.execute('DROP TRIGGER clear_removed_manual_file_selections')
        con.execute('DROP TABLE platform_manual_file_selections')
        con.executescript(migration.read_text(encoding='utf-8'))
    expected = {('buildingFileInfoList', picture['id'])} if change == 'none' else set()
    assert platform.manual_file_selections(case['id'], 'repair') == expected
