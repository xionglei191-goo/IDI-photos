from copy import deepcopy

import pytest

from idi.automatic_uploads import AutomaticUploads
from idi.domain import DomainError
from idi.platform_client import PlatformWriteError
from tests.test_platform_preparation import platform, enter_stage, draft_data, remote_file
from tests.test_feishu_agent import bot, capture, task_for


def setup_flow(services, platform, case, photo, monkeypatch, *, quiet_seconds=0):
    # Existing workflow tests isolate the downstream save/confirmation behavior.
    # The dedicated quiet-window tests exercise the production 300-second wait.
    monkeypatch.setattr('idi.automatic_uploads.PROPOSAL_QUIET_SECONDS', quiet_seconds)
    platform.test_survey.update(buildingFileInfoList=[], houseFileInfoList=[], fileInfoList=[], defectDesc='')
    enter_stage(platform, case['id'], 'survey')
    platform.client.write_enabled = True
    auto = services['automatic_uploads']
    auto.configure({'enabled': True})
    auto.enroll(case['id'])
    uploads, writes = [], []

    def add(color, stage, role):
        p = photo(color, color + '.png', stage=stage, role=role)
        return services['domain'].update_photo(p['id'], {'confirm': True})

    def upload(path, name, mime):
        uploads.append(name)
        return remote_file('auto-' + str(len(uploads)), name)

    def write(stage, payload):
        writes.append((stage, deepcopy(payload)))
        target = platform.test_survey if stage == 'survey' else platform.test_repair
        target.update(deepcopy(payload))
        if stage == 'survey':
            platform.test_row['siteSurveyState'] = str(payload['state'])
        else:
            platform.test_row.update(repairAcceptanceState=str(payload['state']), repairAcceptanceFlowStatus=str(payload['state']))
        return {'code': 0}

    monkeypatch.setattr(platform.client, 'upload', upload)
    monkeypatch.setattr(platform.client, 'save_draft_only', write)
    monkeypatch.setattr(platform.client, 'submit', write)
    return auto, add, uploads, writes, write


def tick(auto, case_id):
    with auto.db.tx() as con:
        con.execute('UPDATE automatic_upload_cases SET retry_at=1 WHERE case_id=?', (case_id,))
    auto.run_once()
    return auto.get(case_id)


def ready(services, platform, case, add, auto):
    for color, role in [('red', 'building_checkin'), ('blue', 'door_checkin'), ('green', 'problem')]:
        add(color, 'survey', role)
    platform.test_survey['defectDesc'] = '现场核实的原因'
    enter_stage(platform, case['id'], 'survey')
    return tick(auto, case['id'])


def test_incremental_save_then_two_human_submissions_with_existing_next_stage_photos(platform, services, case, photo, monkeypatch, client):
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    add('red', 'survey', 'building_checkin')
    row = tick(auto, case['id'])
    assert row['state'] == 'needs_fields', row
    assert len(uploads) == 1 and writes[-1][1]['state'] == 0
    assert 'defectDesc' not in writes[-1][1]
    save = services['reviews'].get(row['save_review_id'])
    assert save['save_only'] and save['state'] == 'save_verified'
    with pytest.raises(DomainError, match='仅用于'):
        services['reviews'].confirm(save['id'], save['fingerprint'])
    add('blue', 'survey', 'door_checkin')
    tick(auto, case['id'])
    add('green', 'survey', 'problem')
    row = tick(auto, case['id'])
    assert len(uploads) == 3 and all(payload['state'] == 0 for _, payload in writes)
    assert row['state'] == 'awaiting_confirmation'
    incomplete = services['reviews'].get(row['review_id'])
    assert incomplete['snapshot']['review_only']
    assert incomplete['snapshot']['pending_fields'] == [{'field': 'defectDesc', 'label': '缺陷原因分析', 'message': '缺少缺陷原因分析'}]
    for action in ('save', 'submit'):
        with pytest.raises(DomainError, match='待确认字段'):
            services['reviews'].confirm(incomplete['id'], incomplete['fingerprint'], action=action)
    assert all(payload['state'] == 0 for _, payload in writes)
    plan = platform.draft(case['id'], 'survey')
    platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', {**plan['values'], 'defectDesc': '现场确认的缺陷原因'}))
    # Photos for the next phase are already in the archive before submitting survey.
    add('yellow', 'work', 'building_checkin')
    add('orange', 'work', 'door_checkin')
    add('purple', 'acceptance', 'finished')
    platform.test_repair.update(approachStartWorkDate='2026-10-17 09:00:00', finishWorkDate='2026-10-17 10:00:00', repairCrewNumber=1)
    row = tick(auto, case['id'])
    assert row['state'] == 'awaiting_confirmation', row
    assert len(uploads) == 3
    review = services['reviews'].get(row['review_id'])
    assert not review['snapshot'].get('review_only')
    assert services['reviews'].get(incomplete['id'])['state'] == 'stale'
    assert client.post(f"/api/workflow/reviews/{review['id']}/confirm", json={'fingerprint': review['fingerprint']}).status_code == 400
    result = client.post(f"/api/workflow/reviews/{review['id']}/confirm", json={'acknowledged': True, 'fingerprint': review['fingerprint']})
    assert result.status_code == 200 and result.json()['state'] == 'verified', result.text
    assert services['domain'].case(case['id'])['workflow_stage'] == 'repair'
    tick(auto, case['id'])
    row = tick(auto, case['id'])
    assert row['state'] == 'awaiting_confirmation', row
    assert len(uploads) == 6 and writes[-1][0] == 'repair' and writes[-1][1]['state'] == 0
    assert 'acceptanceCheckFileInfo' not in writes[-1][1]
    repair = services['reviews'].get(row['review_id'])
    assert services['reviews'].confirm(repair['id'], repair['fingerprint'])['state'] == 'verified'
    assert services['domain'].case(case['id'])['workflow_stage'] == 'acceptance'
    assert tick(auto, case['id'])['state'] == 'waiting_stage'
    assert len(uploads) == 6 and [p['state'] for _, p in writes].count(1) == 2


def test_incomplete_review_requires_all_files_and_rejects_invalid_business_values(platform, services, case, photo, monkeypatch):
    auto, add, _, _, _ = setup_flow(services, platform, case, photo, monkeypatch)
    add('red', 'survey', 'building_checkin')
    tick(auto, case['id'])
    with pytest.raises(DomainError, match='资料待补或待核对'):
        services['reviews'].snapshot(case['id'], 'survey', review_only=True)
    add('blue', 'survey', 'door_checkin')
    add('green', 'survey', 'problem')
    data = draft_data(platform, case['id'], 'survey', {'siteSurveyDate': 'not-an-actual-date'})
    platform.save_draft(case['id'], 'survey', data)
    with pytest.raises(DomainError, match='资料待补或待核对'):
        services['reviews'].snapshot(case['id'], 'survey', review_only=True)


def test_new_photo_invalidates_proposal_before_rules_process_it(platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    row = ready(services, platform, case, add, auto)
    assert row['state'] == 'awaiting_confirmation', row
    review = services['reviews'].get(row['review_id'])
    add('yellow', 'survey', 'problem')
    with pytest.raises(DomainError, match='新资料'):
        services['reviews'].confirm(review['id'], review['fingerprint'])
    assert all(p['state'] == 0 for _, p in writes)
    row = tick(auto, case['id'])
    assert row['review_id'] != review['id'] and len(uploads) == 4
    assert services['reviews'].get(review['id'])['state'] == 'stale'


def test_unknown_business_response_is_reconciled_after_restart_without_resend(platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, write = setup_flow(services, platform, case, photo, monkeypatch)
    add('red', 'survey', 'building_checkin')
    def unknown(stage, payload):
        write(stage, payload)
        raise PlatformWriteError('保存响应丢失')
    monkeypatch.setattr(platform.client, 'save_draft_only', unknown)
    row = tick(auto, case['id'])
    assert row['state'] == 'readback_pending' and len(writes) == 1, row
    resumed = AutomaticUploads(services['workflow'], services['reviews'], services['photo_workflow'])
    row = tick(resumed, case['id'])
    assert row['state'] == 'needs_fields' and len(writes) == 1 and len(uploads) == 1, row


def test_partial_upload_retries_same_batch_and_preserves_receipts(platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    add('red', 'survey', 'building_checkin')
    add('blue', 'survey', 'door_checkin')
    original = platform.client.upload
    failed = False
    def flaky(*args):
        nonlocal failed
        if len(uploads) == 1 and not failed:
            failed = True
            raise DomainError('网络中断', 502)
        return original(*args)
    monkeypatch.setattr(platform.client, 'upload', flaky)
    row = tick(auto, case['id'])
    batch_id = row['save_review_id']
    assert row['state'] == 'retry_wait' and len(uploads) == 1 and not writes
    row = tick(auto, case['id'])
    assert row['save_review_id'] == batch_id and len(uploads) == 2 and len(writes) == 1, row


def test_offline_recovers_and_pause_prevents_writes(platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    add('red', 'survey', 'building_checkin')
    platform.client._token = ''
    assert tick(auto, case['id'])['state'] == 'awaiting_login'
    assert not uploads
    platform.client._token = 'test-token'
    assert auto.run_once() == 1
    assert len(writes) == 1
    auto.enroll(case['id'], False)
    add('blue', 'survey', 'door_checkin')
    assert auto.run_once() == 0 and len(writes) == 1


def test_enabled_queue_does_not_scan_old_cases_and_new_receipts_enroll(platform, services, case, photo, monkeypatch):
    picture = photo('red', stage='survey', role='building_checkin')
    with services['db'].tx() as con:
        con.execute("UPDATE photos SET created_at='2020-01-01' WHERE id=?", (picture['id'],))
    auto = services['automatic_uploads']
    auto.configure({'enabled': True})
    assert auto.run_once() == 0 and auto.get(case['id']) is None
    photo('blue', stage='survey', role='door_checkin')
    auto.run_once()
    assert auto.get(case['id']) is not None


def test_unconfirmed_and_excluded_photos_are_not_uploaded(platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    photo('blue', stage='survey', role='door_checkin')
    add('red', 'survey', 'building_checkin')
    services['photo_workflow'].run_once()
    platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', files={}))
    row = tick(auto, case['id'])
    assert not uploads and not writes and row['state'] == 'needs_fields', row


def test_auto_policy_requires_boolean_and_defaults_off(client):
    assert client.get('/api/workflow/uploads').json()['policy']['enabled'] is False
    assert client.put('/api/workflow/uploads/policy', json={'enabled': 'yes'}).status_code == 400


def test_photo_arriving_during_final_refresh_stops_formal_submission(platform, services, case, photo, monkeypatch):
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    row = ready(services, platform, case, add, auto)
    review = services['reviews'].get(row['review_id'])
    original = services['workflow'].refresh_case
    def refresh(case_id):
        result = original(case_id)
        add('orange', 'survey', 'problem')
        return result
    monkeypatch.setattr(services['workflow'], 'refresh_case', refresh)
    with pytest.raises(DomainError, match='新资料'):
        services['reviews'].confirm(review['id'], review['fingerprint'])
    assert all(payload['state'] == 0 for _, payload in writes)


def test_unchanged_case_does_not_poll_official_or_recreate_proposal(platform, services, case, photo, monkeypatch):
    auto, add, _, _, _ = setup_flow(services, platform, case, photo, monkeypatch)
    row = ready(services, platform, case, add, auto)
    original = row['review_id']
    monkeypatch.setattr(services['workflow'], 'refresh_case', lambda *args: pytest.fail('No new evidence'))
    assert auto.run_once() == 0
    assert auto.get(case['id'])['review_id'] == original


def test_revoked_group_permission_stops_auto_save(platform, services, case, photo, monkeypatch, bot):
    auto, _, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    mid = capture(bot, '案件编号：ZLBH20260001', images=1, mention=False)
    bot.collect_messages()
    task = task_for(bot, mid)
    bot.domain.update_photo(task['input']['photo_ids'][0], {'case_id': case['id'], 'photo_group': 'survey_problem', 'confirm': True})
    bot.save_binding(bot.test_connector['id'], {'save_drafts': False})
    tick(auto, case['id'])
    assert not uploads and not writes
    assert auto.get(case['id'])['state'] in ('needs_check', 'needs_fields')


def test_restore_pauses_auto_queue_and_preserves_receipts(platform, services, case, photo, monkeypatch, tmp_path):
    from idi.cli import restore
    from idi.db import Database
    auto, add, _, _, _ = setup_flow(services, platform, case, photo, monkeypatch)
    add('red', 'survey', 'building_checkin')
    row = tick(auto, case['id'])
    backup = services['reporter'].backup({}, lambda *args: None)
    target = tmp_path / 'restored-auto'
    restore(services['settings'].data_dir / 'backups' / backup['filename'], target)
    restored = Database(target / 'archive.sqlite3')
    assert not restored.setting('automatic_upload_policy')['enabled']
    assert restored.one('SELECT save_review_id FROM automatic_upload_cases')['save_review_id'] == row['save_review_id']
    assert restored.one('SELECT state FROM platform_review_batches')['state'] == 'save_verified'


def test_photo_arriving_while_proposal_is_created_is_not_frozen_as_reviewed(platform, services, case, photo, monkeypatch):
    auto, add, _, _, _ = setup_flow(services, platform, case, photo, monkeypatch)
    original = services['reviews'].prepare
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get('save_only'):
            add('orange', 'survey', 'problem')
        return result
    monkeypatch.setattr(services['reviews'], 'prepare', prepare)
    row = ready(services, platform, case, add, auto)
    assert row['state'] == 'queued' and not row['review_id'], row
    monkeypatch.setattr(services['reviews'], 'prepare', original)
    assert tick(auto, case['id'])['state'] == 'awaiting_confirmation'
