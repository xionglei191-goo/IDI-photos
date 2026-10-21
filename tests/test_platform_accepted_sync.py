"""Automatic sync stops fetching details after official acceptance is complete."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from idi.db import dump
from idi.platform_photos import online_photo_counts
from tests.test_platform_incremental import link, policy, remote, run


def old_details(services, key):
    stored = link(services, key)
    snapshot = json.loads(stored['snapshot'])
    snapshot['fetched_at'] = '2026-10-10T00:00:00+00:00'
    with services['db'].tx() as con:
        con.execute('UPDATE platform_links SET verified_at=?,snapshot=? WHERE case_id=?',
                    (snapshot['fetched_at'], dump(snapshot), stored['case_id']))
    return link(services, key)


def details_requested(remote):
    return [path for path, _ in remote.requests if '/getInfo/' in path]


@pytest.mark.parametrize('flow,stage', [('2', 'settlement'), ('9', 'closed')])
def test_new_accepted_case_is_archived_from_list_without_reading_details(remote, services, flow, stage):
    key = remote.add(1)
    remote.rows[key]['repairAcceptanceFlowStatus'] = flow
    policy(services, batch_requests=3)
    result = run(services)
    assert result['state'] == 'completed' and result['request_count'] == 3
    assert result['photos_checked'] == result['photo_pending'] == 0
    assert not details_requested(remote)
    stored = services['platform'].link(link(services, key)['case_id'])
    assert services['domain'].case(stored['case_id'])['workflow_stage'] == stage
    assert stored['verified_at'] is None and stored['snapshot']['list_verified_at']
    assert not any(stored['snapshot'].get(part) for part in ('claim', 'survey', 'repair'))
    counts = online_photo_counts(stored)
    assert counts['read_at'] is None and counts['groups']['completion']['state'] == 'unread'
    assert services['workflow'].board('work')['source_total'] == 1
    plan = services['platform'].preview(stored['case_id'], 'acceptance')
    assert not any(item['field'] == 'binding' for item in plan['issues'])
    assert any('详情和附件尚未读取' in item['message'] for item in plan['issues'])


@pytest.mark.parametrize('flow,stage', [('2', 'settlement'), ('9', 'closed')])
def test_acceptance_transition_preserves_cached_photos_and_actual_read_time(remote, services, flow, stage):
    key = remote.add(1)
    remote.rows[key]['repairAcceptanceFlowStatus'] = '1'
    remote.photos[key] = [{'id': 'kept', 'fileName': '已核验.jpg', 'fileUrl': '/kept.jpg'}]
    assert run(services, 'full')['state'] == 'completed'
    before = old_details(services, key)
    saved = json.loads(before['snapshot'])
    services['domain'].save_case({'title': '保留本地填写'}, before['case_id'])
    remote.rows[key]['repairAcceptanceFlowStatus'] = flow
    remote.photos[key] = [{'id': 'unread', 'fileName': '未读取.jpg', 'fileUrl': '/unread.jpg'}]
    remote.requests.clear()
    result = run(services)
    assert result['state'] == 'completed' and result['photos_checked'] == result['photo_pending'] == 0
    assert not details_requested(remote)
    after = link(services, key)
    snapshot = json.loads(after['snapshot'])
    assert after['verified_at'] == before['verified_at']
    assert snapshot['fetched_at'] == saved['fetched_at'] and snapshot['list_verified_at']
    assert {k: snapshot[k] for k in ('claim', 'survey', 'repair', 'personnel')} == {
        k: saved[k] for k in ('claim', 'survey', 'repair', 'personnel')}
    case = services['domain'].case(before['case_id'])
    assert case['workflow_stage'] == stage and case['title'] == '保留本地填写'
    assert case['workflow_verified_at'] == snapshot['list_verified_at']
    counts = online_photo_counts(services['platform'].link(before['case_id']))
    assert counts['read_at'] == before['verified_at'] and counts['groups']['completion']['count'] == 1
    event = services['db'].one('SELECT * FROM case_events WHERE case_id=? ORDER BY rowid DESC LIMIT 1', (before['case_id'],))
    assert event['source'] == 'platform_list'
    remote.requests.clear()
    assert run(services)['state'] == 'completed'
    assert not details_requested(remote)


def test_waiting_for_acceptance_still_gets_periodic_photo_checks(remote, services):
    key = remote.add(1)
    remote.rows[key]['repairAcceptanceFlowStatus'] = '1'
    run(services, 'full')
    old_details(services, key)
    remote.photos[key] = [{'id': 'new', 'fileName': '待验收.jpg', 'fileUrl': '/new.jpg'}]
    remote.requests.clear()
    result = run(services)
    assert result['state'] == 'completed' and result['photos_checked'] == 1
    assert details_requested(remote)
    snapshot = json.loads(link(services, key)['snapshot'])
    assert snapshot['repair']['recoveredFileInfo'][0]['id'] == 'new'
    assert services['domain'].case(link(services, key)['case_id'])['workflow_stage'] == 'acceptance'


@pytest.mark.parametrize('missing_flow', [None, '', 'unknown'])
def test_incomplete_list_does_not_reopen_accepted_case(remote, services, missing_flow):
    key = remote.add(1)
    remote.rows[key]['repairAcceptanceFlowStatus'] = '2'
    run(services, 'full')
    before = old_details(services, key)
    if missing_flow is None:
        remote.rows[key].pop('repairAcceptanceFlowStatus')
    else:
        remote.rows[key]['repairAcceptanceFlowStatus'] = missing_flow
    remote.rows[key]['updateTime'] = '2026-10-17 09:00:00'
    remote.requests.clear()
    assert run(services)['state'] == 'completed'
    assert not details_requested(remote)
    assert services['domain'].case(before['case_id'])['workflow_stage'] == 'settlement'
    assert link(services, key)['verified_at'] == before['verified_at']
    assert services['workflow'].synchronizer.photo_backlog() == 0


def test_accepted_case_disappearing_from_active_list_only_confirms_status(remote, services):
    key = remote.add(1)
    remote.rows[key]['repairAcceptanceFlowStatus'] = '2'
    run(services, 'full')
    before = old_details(services, key)
    remote.active.remove(key)
    remote.rows[key]['repairAcceptanceFlowStatus'] = '9'
    remote.requests.clear()
    assert run(services)['state'] == 'completed'
    assert any(body.get('reportReceivingId') == key for _, body in remote.requests)
    assert not details_requested(remote)
    assert link(services, key)['verified_at'] == before['verified_at']
    assert services['domain'].case(before['case_id'])['workflow_stage'] == 'closed'
    assert services['db'].one('SELECT active FROM platform_repair_activity WHERE report_receiving_id=?', (key,))['active'] == 0


def test_photo_rotation_detects_newly_completed_acceptance_before_fetching_details(remote, services):
    key = remote.add(1, '2026-09-01')
    remote.rows[key]['repairAcceptanceFlowStatus'] = '1'
    run(services, 'full')
    before = old_details(services, key)
    def finish_acceptance(path, body):
        if path.endswith('getThirdPartyRepairList') and body.get('reportReceivingId') == key:
            remote.rows[key]['repairAcceptanceFlowStatus'] = '2'
    remote.fail = finish_acceptance
    remote.requests.clear()
    result = run(services)
    assert result['state'] == 'completed' and result['photos_checked'] == result['photo_pending'] == 0
    assert any(body.get('reportReceivingId') == key for _, body in remote.requests)
    assert not details_requested(remote)
    assert link(services, key)['verified_at'] == before['verified_at']
    assert services['domain'].case(before['case_id'])['workflow_stage'] == 'settlement'


@pytest.mark.parametrize('manual', ['full', 'single'])
def test_explicit_manual_refresh_can_read_accepted_case_details(remote, services, manual):
    key = remote.add(1)
    remote.rows[key]['repairAcceptanceFlowStatus'] = '2'
    run(services)
    before = link(services, key)
    assert before['verified_at'] is None
    remote.photos[key] = [{'id': 'manual', 'fileName': '手动读取.jpg', 'fileUrl': '/manual.jpg'}]
    remote.requests.clear()
    if manual == 'full':
        assert run(services, 'full')['state'] == 'completed'
    else:
        services['workflow'].refresh_case(before['case_id'])
    assert details_requested(remote)
    after = link(services, key)
    assert after['verified_at'] and json.loads(after['snapshot'])['repair']['recoveredFileInfo'][0]['id'] == 'manual'


def test_explicit_return_to_waiting_acceptance_resumes_detail_reads(remote, services):
    key = remote.add(1)
    remote.rows[key]['repairAcceptanceFlowStatus'] = '2'
    run(services)
    remote.rows[key]['repairAcceptanceFlowStatus'] = '1'
    remote.requests.clear()
    assert run(services)['state'] == 'completed'
    assert details_requested(remote)
    assert services['domain'].case(link(services, key)['case_id'])['workflow_stage'] == 'acceptance'


def test_older_full_snapshot_cannot_undo_newer_list_acceptance(remote, services):
    key = remote.add(1)
    run(services, 'full')
    before = link(services, key)
    old = json.loads(before['snapshot'])
    current = deepcopy(old)
    current['row']['repairAcceptanceFlowStatus'] = '2'
    current['fetched_at'] = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    services['workflow'].apply_snapshot(current, details_read=False)
    result = services['workflow'].apply_snapshot(old)
    assert result['stale_ignored']
    after = link(services, key)
    assert after['verified_at'] == before['verified_at']
    assert services['domain'].case(after['case_id'])['workflow_stage'] == 'settlement'
