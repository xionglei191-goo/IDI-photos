import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from idi.domain import DomainError
from idi.platform_client import PlatformClient, PlatformReadPaused
from idi.platform_sync import PlatformSync


class Official:
    def __init__(self):
        self.rows, self.intake, self.repair, self.active, self.photos = {}, [], [], set(), {}
        self.requests = []
        self.fail = None
        self.ignore_dates = False
        self.ignore_archived = False
        self.hide = set()

    def add(self, number, report_date='2026-10-16', repair=True, active=True):
        key = str(2110638801184980000 + number)
        row = dict(reportReceivingId=key, reportCode='ZLBH' + key, communityName='增量测试项目',
                   communityDesc=f'1栋{number + 100}号', accidentDesc='墙面维修', reportDate=report_date,
                   reportReceivingState='1', flowStatus='2' if repair else '1', acceptanceResult='1' if repair else '',
                   siteSurveyId='s-' + key if repair else '', siteSurveyState='1' if repair else '',
                   repairAcceptanceId='r-' + key if repair else '', repairAcceptanceFlowStatus='0')
        self.rows[key] = row
        self.photos[key] = []
        self.intake.append(key)
        if repair:
            self.repair.append(key)
            if active:
                self.active.add(key)
        return key

    def __call__(self, request):
        path, body = request.url.path, json.loads(request.content)
        self.requests.append((path, body))
        if self.fail:
            response = self.fail(path, body)
            if response:
                return response
        if path.endswith(('getAttachmentSurveyList', 'getThirdPartyRepairList')):
            keys = self.intake if path.endswith('getAttachmentSurveyList') else self.repair
            keys = [key for key in keys if key not in self.hide]
            if 'reportReceivingId' in body:
                keys = [key for key in keys if key == body['reportReceivingId']]
            if 'endStatus' in body and not self.ignore_archived:
                keys = [key for key in keys if (key in self.active) == (body['endStatus'] == '0')]
            if 'reportDateStart' in body and not self.ignore_dates:
                keys = [key for key in keys if body['reportDateStart'] <= self.rows[key]['reportDate'] <= body['reportDateEnd']]
            total = len(keys)
            keys = keys[(body['page'] - 1) * body['limit']:body['page'] * body['limit']]
            return httpx.Response(200, json={'code': 0, 'data': [self.rows[key] for key in keys], 'count': total})
        key = path.rsplit('/', 1)[1]
        if '/reportReceiving/getInfo/' in path:
            data = self.rows[key]
        elif '/siteSurvey/getInfo/' in path:
            data = {'reportReceivingId': key[2:], 'siteSurveyId': key, 'fileInfoList': []}
        elif '/repairAcceptance/getInfo/' in path:
            data = {'reportReceivingId': key[2:], 'repairAcceptanceId': key, 'preRepairFileInfo': [], 'recoveredFileInfo': self.photos[key[2:]]}
        else:
            pytest.fail(path)
        return httpx.Response(200, json={'code': 0, 'data': data})


@pytest.fixture
def remote(services):
    fake = Official()
    services['platform'].client.close()
    client = PlatformClient(httpx.MockTransport(fake))
    client._token, client._account = 'fixture-token', 'sync-test'
    services['platform'].client = client
    engine = services['workflow'].synchronizer
    engine.clock = lambda: datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)
    return fake


def run(services, mode='incremental', **payload):
    return services['workflow'].sync({'mode': mode, **payload}, lambda *_: None)


def link(services, key):
    return services['db'].one('SELECT * FROM platform_links WHERE report_receiving_id=?', (key,))


def policy(services, **values):
    sync = services['workflow'].synchronizer
    services['db'].set_setting('platform_sync_policy', {**sync.policy(), **values})


def test_recent_intake_and_all_active_repairs_merge_without_losing_history(remote, services):
    historical = remote.add(1, '2026-09-01', repair=False)
    old_active = remote.add(2, '2026-09-01')
    assert run(services, 'full')['state'] == 'completed'
    old_read_at = link(services, old_active)['verified_at']
    services['domain'].save_case({'title': '人工填写的内容', 'responsible': '负责人'}, link(services, historical)['case_id'])
    recent = remote.add(3)
    old_new_repair = remote.add(4, '2026-09-03')
    old_backfill = remote.add(5, '2026-09-03', repair=False)
    remote.requests.clear()
    result = run(services)
    assert result['state'] == 'completed'
    assert result['list_counts'] == {'intake': 1, 'repair': 3}
    assert link(services, recent) and link(services, old_new_repair)
    assert link(services, old_backfill) is None
    assert link(services, old_active)['verified_at'] == old_read_at
    assert services['domain'].case(link(services, historical)['case_id'])['title'] == '人工填写的内容'
    assert services['workflow'].board('intake')['source_total'] == 3
    assert services['workflow'].board('work')['source_total'] == 3
    assert any(body.get('reportDateStart') == '2026-10-16' and body.get('reportDateEnd') == '2026-10-17' for _, body in remote.requests)
    assert any(body.get('endStatus') == '0' and 'reportDateStart' not in body for _, body in remote.requests)
    assert not any(path.endswith('/getInfo/' + old_active) for path, _ in remote.requests)


def test_daily_scheduler_timezone_dedup_offline_pause_and_gap_recovery(remote, services):
    remote.add(1)
    sync, jobs, db = services['workflow'].synchronizer, services['jobs'], services['db']
    assert sync.tick(datetime(2026, 10, 16, 23, 59, tzinfo=timezone.utc)) is None  # 07:59 China
    at_eight = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
    first = sync.tick(at_eight)
    assert first and sync.tick(at_eight) is None
    jobs.run_one('network')
    assert jobs.get(first['id'])['state'] == 'completed'
    assert sync.tick(at_eight) is None
    sync.clock = lambda: datetime(2026, 10, 21, 0, 1, tzinfo=timezone.utc)
    services['platform'].client.logout()
    assert sync.tick() is None
    services['platform'].client._token, services['platform'].client._account = 'fixture-token', 'sync-test'
    second = sync.tick()
    checkpoint = json.loads(db.one('SELECT checkpoint FROM platform_sync_runs WHERE id=?', (second['payload']['run_id'],))['checkpoint'])
    assert checkpoint['date_from'] == '2026-10-17' and checkpoint['date_to'] == '2026-10-21'
    sync.configure({'enabled': False})
    assert sync.status()['schedule']['enabled'] is False


def test_archive_without_echoed_flag_is_confirmed_and_not_inferred_closed(remote, services):
    key = remote.add(1)
    assert run(services, 'full')['state'] == 'completed'
    remote.active.remove(key)
    remote.requests.clear()
    result = run(services)
    assert result['state'] == 'completed'
    assert any(body.get('reportReceivingId') == key for _, body in remote.requests)
    assert services['db'].one('SELECT active FROM platform_repair_activity WHERE report_receiving_id=?', (key,))['active'] == 0
    assert services['domain'].case(link(services, key)['case_id'])['workflow_stage'] == 'repair'
    assert services['workflow'].board('work')['source_total'] == 1


def test_absent_from_both_lists_keeps_case_and_previous_complete_membership(remote, services):
    key = remote.add(1)
    assert run(services, 'full')['state'] == 'completed'
    remote.hide.add(key)
    result = run(services)
    assert result['state'] == 'partial' and result['errors']
    assert services['workflow'].board('work')['source_total'] == 1
    assert services['domain'].case(link(services, key)['case_id'])['workflow_stage'] == 'repair'
    assert services['db'].setting('platform_incremental_success') is None


def test_confirmed_repair_exit_retains_intake_and_local_archive(remote, services):
    key = remote.add(1)
    run(services, 'full')
    remote.repair.remove(key)
    remote.rows[key].update(flowStatus='3')
    assert run(services)['state'] == 'completed'
    assert services['workflow'].board('work')['source_total'] == 0
    assert services['workflow'].board('intake')['source_total'] == 1
    assert services['domain'].case(link(services, key)['case_id'])['workflow_stage'] == 'insurer_review'


def test_attachment_only_changes_are_rotated_with_budget_and_actual_time(remote, services):
    keys = [remote.add(i) for i in range(1, 4)]
    run(services, 'full')
    with services['db'].tx() as con:
        con.execute("UPDATE platform_links SET verified_at='2026-10-10T00:00:00+00:00'")
    remote.photos[keys[0]] = [{'id': 'new-photo', 'fileName': '完工.jpg', 'fileUrl': '/finished.jpg'}]
    policy(services, photo_requests=5)
    remote.requests.clear()
    result = run(services)
    assert result['state'] == 'completed' and result['photos_checked'] == 1 and result['photo_pending'] == 2
    assert json.loads(link(services, keys[0])['snapshot'])['repair']['recoveredFileInfo'][0]['id'] == 'new-photo'
    assert link(services, keys[1])['verified_at'] == '2026-10-10T00:00:00+00:00'
    assert services['workflow'].synchronizer.status()['schedule']['today_photo_requests'] <= 5
    # Manual full reads also refresh same-row attachments, without relying on row changes.
    remote.photos[keys[1]] = [{'id': 'second', 'fileName': '另张.jpg', 'fileUrl': '/second.jpg'}]
    assert run(services, 'full')['state'] == 'completed'
    assert json.loads(link(services, keys[1])['snapshot'])['repair']['recoveredFileInfo']


def test_request_budget_persists_pages_and_details_across_engine_restart(remote, services):
    for i in range(85):
        remote.add(i, repair=False)
    policy(services, batch_requests=2, daily_requests=500, photo_requests=0)
    first = run(services)
    assert first['state'] == 'partial' and first['request_count'] == 2
    assert services['db'].one('SELECT COUNT(*) n FROM platform_sync_rows WHERE run_id=?', (first['run_id'],))['n'] == 80
    old = services['workflow'].synchronizer
    services['workflow'].synchronizer = PlatformSync(services['workflow'])
    services['workflow'].synchronizer.clock = old.clock
    policy(services, batch_requests=100)
    remote.requests.clear()
    second = run(services, run_id=first['run_id'])
    assert second['state'] == 'completed'
    pages = [body['page'] for path, body in remote.requests if path.endswith('getAttachmentSurveyList')]
    assert pages[:2] == [1, 3]  # Verify head, then resume page 3; page 2 is not fetched again.
    assert services['workflow'].board('intake')['source_total'] == 85


def test_changed_page_head_restarts_only_affected_scan(remote, services):
    for i in range(45):
        remote.add(i, repair=False)
    policy(services, batch_requests=1, photo_requests=0)
    first = run(services)
    key = remote.add(99, repair=False)
    remote.intake.remove(key); remote.intake.insert(0, key)
    policy(services, batch_requests=100)
    result = run(services, run_id=first['run_id'])
    assert result['state'] == 'completed' and result['list_counts']['intake'] == 46
    assert services['workflow'].board('intake')['source_total'] == 46


def test_daily_limit_defers_until_next_day_without_advancing_success(remote, services):
    remote.add(1)
    policy(services, daily_requests=1)
    result = run(services)
    assert result['state'] == 'partial'
    assert result['retry_at'].startswith('2026-10-18T00:00:00')
    assert services['db'].setting('platform_incremental_success') is None
    assert len(remote.requests) == 1


def test_429_honors_retry_after_and_stops_remaining_cases(remote, services, monkeypatch):
    remote.add(1); remote.add(2)
    remote.fail = lambda path, body: httpx.Response(429, headers={'Retry-After': '120'}) if '/getInfo/' in path else None
    first = run(services)
    assert first['state'] == 'partial' and first['retry_at'].startswith('2026-10-17T01:02:00')
    assert sum('/getInfo/' in path for path, _ in remote.requests) == 1
    before = len(remote.requests)
    with pytest.raises(PlatformReadPaused):
        services['platform'].client.read('intake_list', {})
    assert len(remote.requests) == before


def test_date_filter_ignored_by_server_stops_without_widening_queries(remote, services):
    remote.add(1, '2026-09-01', repair=False)
    remote.ignore_dates = True
    result = run(services)
    assert result['state'] == 'partial' and '日期范围' in result['error']
    assert len(remote.requests) == 1 and not services['db'].all('SELECT * FROM platform_list_entries')


def test_ignored_archive_filter_without_echoed_flags_is_detected(remote, services):
    remote.add(1)
    remote.ignore_archived = True
    result = run(services)
    assert result['state'] == 'partial' and '筛选结果重叠' in result['error']
    assert not services['db'].all('SELECT * FROM platform_list_entries')


def test_login_expires_stops_and_account_change_cannot_resume(remote, services):
    remote.add(1)
    remote.fail = lambda path, body: httpx.Response(401) if '/getInfo/' in path else None
    result = run(services)
    assert result['state'] == 'partial' and result['retry_at']
    assert not services['platform'].client.status()['connected']
    remote.fail = None
    services['platform'].client._token, services['platform'].client._account = 'new-token', 'different-account'
    count = len(remote.requests)
    with pytest.raises(DomainError, match='账号'):
        run(services, run_id=result['run_id'])
    assert len(remote.requests) == count


def test_manual_full_queued_behind_daily_is_not_downgraded(remote, services, client):
    remote.add(1)
    sync = services['workflow'].synchronizer
    automatic = sync.enqueue('incremental')
    manual = client.post('/api/workflow/sync').json()
    assert manual['id'] != automatic['id'] and manual['payload']['mode'] == 'full'
    assert client.post('/api/workflow/sync').json()['id'] == manual['id']
    assert client.put('/api/workflow/sync/policy', json={'enabled': False}).status_code == 200


def test_older_background_snapshot_cannot_overwrite_newer_single_case_read(remote, services):
    key = remote.add(1)
    run(services, 'full')
    stored = link(services, key)
    snapshot = json.loads(stored['snapshot'])
    snapshot['fetched_at'] = '2000-01-01T00:00:00+00:00'
    snapshot['row']['repairAcceptanceFlowStatus'] = '9'
    result = services['workflow'].apply_snapshot(snapshot)
    assert result['stale_ignored']
    assert services['domain'].case(stored['case_id'])['workflow_stage'] == 'repair'


def test_single_case_refresh_publishes_new_repair_membership(remote, services):
    key = remote.add(1, repair=False)
    run(services, 'full')
    case_id = link(services, key)['case_id']
    remote.rows[key].update(flowStatus='2', acceptanceResult='1')
    remote.repair.append(key)
    services['workflow'].refresh_case(case_id)
    assert services['workflow'].board('work')['source_total'] == 1


def test_three_consecutive_detail_errors_stop_the_batch(remote, services, monkeypatch):
    for i in range(10):
        remote.add(i)
    attempts = []
    def fail(row, scope):
        attempts.append(row['reportReceivingId'])
        raise DomainError('资料状态尚未核对', 409)
    monkeypatch.setattr(services['platform'].client, 'snapshot_from_row', fail)
    result = run(services)
    assert result['state'] == 'partial' and len(attempts) == 3
    assert '连续三个' in result['error']


def test_one_client_spaces_reads_and_writes_without_automatic_replay(monkeypatch):
    clock, sent = [100.0], []
    def transport(request):
        sent.append(clock[0])
        return httpx.Response(200, json={'code': 0, 'data': []})
    client = PlatformClient(httpx.MockTransport(transport), write_enabled=True)
    client._token = 'fixture'
    client.request_interval = 1.0
    monkeypatch.setattr('idi.platform_client.time.monotonic', lambda: clock[0])
    def wait(seconds):
        clock[0] += seconds
        return False
    monkeypatch.setattr(client._closing, 'wait', wait)
    client.read('intake_list')
    client.save_draft_only('survey', {'state': 0})
    client.read('repair_list')
    assert sent == [100.0, 101.0, 102.0]
    client.close()


@pytest.mark.parametrize('start,end', [('2026-02-30', '2026-03-01'), ('2026-10-18', '2026-10-17'), ('2026-10-16', None)])
def test_invalid_date_range_never_requests_official(remote, services, start, end):
    with pytest.raises(DomainError):
        services['platform'].client.search(report_start=start, report_end=end)
    assert not remote.requests
