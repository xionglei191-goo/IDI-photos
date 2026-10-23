"""Company ledger reads can follow a verified readback by the bound project."""
import json

import httpx
import pytest

from idi.db import dump
from idi.platform_client import PlatformClient
from tests.conftest import image_bytes
from tests.test_platform_incremental import Official, link, policy, run


@pytest.fixture
def account_remote(services):
    fake = Official()
    accounts = services['project_accounts']
    accounts.company.configure({'username': 'company-reader', 'password': 'test-password', 'auto_connect': False})
    accounts.company.client.close()
    company = PlatformClient(httpx.MockTransport(fake))
    company._permanently_read_only = True
    company._token, company._account = 'company-token', 'company-reader'
    accounts.company._client = company
    accounts.client_factory = lambda: PlatformClient(httpx.MockTransport(fake))
    project = services['domain'].save_project({'name': '增量测试项目', 'aliases': ['测试项目别名']})
    accounts.configure(project['id'], {'username': 'project-writer', 'password': 'test-password', 'auto_connect': False})
    writer = accounts.account(project['id']).client
    writer._token, writer._account = 'project-token', 'project-writer'
    from datetime import datetime, timezone
    services['workflow'].synchronizer.clock = lambda: datetime(2026, 10, 17, 1, tzinfo=timezone.utc)
    key = fake.add(1)
    assert run(services, 'full')['state'] == 'completed'
    case_id = link(services, key)['case_id']
    with accounts.bind(case_id):
        services['workflow'].refresh_case(case_id)
    assert json.loads(link(services, key)['snapshot'])['account'] == 'project-writer'
    fake.requests.clear()
    yield fake, key, project, case_id
    accounts.router.close()


@pytest.mark.parametrize('mode,changed', [('full', False), ('incremental', True), ('incremental', False)])
def test_company_sync_after_project_readback_preserves_local_work(account_remote, services, mode, changed):
    fake, key, project, case_id = account_remote
    services['domain'].save_case({'title': '保留人工内容', 'responsible': '现场负责人'}, case_id)
    photo_id = services['importer'].add_file(image_bytes(), '现场原图.png', case_id=case_id)['photo_id']
    services['platform'].save_draft(case_id, 'repair', {
        'values': {'repairCrewNumber': 2}, 'files': {}, 'revision': 0,
        'link_revision': link(services, key)['revision']})
    db = services['db']
    retained = {table: db.all('SELECT * FROM ' + table) for table in
                ('photos', 'blobs', 'platform_drafts', 'platform_draft_field_sources')}
    if changed:
        fake.rows[key]['accidentDesc'] = '官网更新的内容'
    result = run(services, mode)
    assert result['state'] == 'completed' and not result['errors'] and result['remote_written'] is False
    assert services['domain'].case(case_id)['title'] == '保留人工内容'
    assert services['domain'].case(case_id)['responsible'] == '现场负责人'
    assert services['domain'].photo(photo_id)['case_id'] == case_id
    assert retained == {table: db.all('SELECT * FROM ' + table) for table in retained}
    # A later project preflight/readback and another company scan remain valid.
    with services['project_accounts'].bind(case_id):
        services['workflow'].refresh_case(case_id)
    assert run(services, 'full')['state'] == 'completed'
    assert db.one('SELECT COUNT(*) AS n FROM cases')['n'] == 1
    assert db.all('SELECT * FROM website_operations') == []


def test_photo_rotation_accepts_project_readback_without_changing_list(account_remote, services):
    fake, key, project, case_id = account_remote
    with services['db'].tx() as con:
        con.execute("UPDATE platform_links SET verified_at='2026-10-10T00:00:00+00:00'")
    fake.photos[key] = [{'id': 'new-photo', 'fileName': '完工.jpg', 'fileUrl': '/finished.jpg'}]
    policy(services, photo_requests=5)
    result = run(services)
    assert result['state'] == 'completed' and result['photos_checked'] == 1
    snapshot = json.loads(link(services, key)['snapshot'])
    assert snapshot['account'] == 'company-reader'
    assert snapshot['repair']['recoveredFileInfo'][0]['id'] == 'new-photo'


@pytest.mark.parametrize('mismatch', ['unknown_account', 'other_project_account', 'report_code',
                                    'project_name', 'claim_code', 'company_changed', 'raw_client'])
def test_cross_account_mismatch_keeps_existing_link_and_list(account_remote, services, mismatch):
    fake, key, project, case_id = account_remote
    accounts, db = services['project_accounts'], services['db']
    if mismatch in ('unknown_account', 'other_project_account'):
        source = 'untrusted-writer'
        if mismatch == 'other_project_account':
            other = services['domain'].save_project({'name': '另一个项目'})
            accounts.configure(other['id'], {'username': source, 'password': 'test-password', 'auto_connect': False})
        stored = json.loads(link(services, key)['snapshot'])
        stored['account'] = source
        with db.tx() as con:
            con.execute('UPDATE platform_links SET snapshot=? WHERE case_id=?', (dump(stored), case_id))
    elif mismatch == 'report_code':
        fake.rows[key]['reportCode'] = 'DIFFERENT-CODE'
    elif mismatch == 'project_name':
        fake.rows[key]['communityName'] = '另一个项目'
    elif mismatch == 'claim_code':
        fake.fail = lambda path, body: httpx.Response(200, json={'code': 0, 'data':
            {**fake.rows[key], 'reportCode': 'OTHER-CLAIM'}}) if '/reportReceiving/getInfo/' in path else None
    elif mismatch == 'company_changed':
        accounts.company.client._account = 'unrecognized-company'
    elif mismatch == 'raw_client':
        services['platform'].client = accounts.company.client
    before = link(services, key)
    result = run(services, 'full')
    assert result['state'] == 'partial' and result['errors']
    assert link(services, key) == before
    assert db.one('SELECT COUNT(*) AS n FROM cases')['n'] == 1
    assert services['workflow'].board('work')['source_total'] == 1


@pytest.mark.parametrize('change', ['binding', 'case_project', 'snapshot_account'])
def test_source_is_rechecked_after_network_read(account_remote, services, change):
    fake, key, project, case_id = account_remote
    accounts, db = services['project_accounts'], services['db']
    before = link(services, key)
    invoked = []
    def change_during_read(path, body):
        if '/reportReceiving/getInfo/' not in path or invoked:
            return None
        invoked.append(True)
        if change == 'binding':
            accounts.configure(project['id'], {'username': 'replacement-writer', 'password': 'test-password', 'auto_connect': False})
        elif change == 'case_project':
            other = services['domain'].save_project({'name': '新归属项目'})
            services['domain'].save_case({'project_id': other['id']}, case_id)
        else:
            stored = json.loads(before['snapshot'])
            stored['account'] = 'unexpected-writer'
            with db.tx() as con:
                con.execute('UPDATE platform_links SET snapshot=? WHERE case_id=?', (dump(stored), case_id))
        return None
    fake.fail = change_during_read
    result = run(services, 'full')
    assert invoked and result['state'] == 'partial' and result['errors']
    assert link(services, key)['verified_at'] == before['verified_at']
    assert link(services, key)['revision'] == before['revision']


def test_full_sync_can_explicitly_resume_a_corrected_failed_batch(account_remote, services, client):
    fake, key, project, case_id = account_remote
    before = json.loads(link(services, key)['snapshot'])
    with services['db'].tx() as con:
        con.execute('UPDATE platform_links SET snapshot=? WHERE case_id=?',
                    (dump({**before, 'account': 'unrecognized-writer'}), case_id))
    failed = run(services, 'full')
    assert failed['state'] == 'partial' and failed['retry_at'] is None
    # Restoring the verified project readback resolves the provenance error.
    with services['db'].tx() as con:
        con.execute('UPDATE platform_links SET snapshot=? WHERE case_id=?', (dump(before), case_id))
    response = client.post('/api/workflow/sync', json={'resume_id': failed['run_id']})
    assert response.status_code == 200, response.text
    job = response.json()
    assert job['payload']['run_id'] == failed['run_id']
    services['jobs'].run_one('network')
    assert services['jobs'].get(job['id'])['result']['state'] == 'completed'
    assert services['workflow'].synchronizer.status()['latest']['state'] == 'completed'
    assert client.post('/api/workflow/sync', json={'resume_id': []}).status_code == 400
    assert client.post('/api/workflow/sync', json={'resume_id': 'unknown'}).status_code == 409
