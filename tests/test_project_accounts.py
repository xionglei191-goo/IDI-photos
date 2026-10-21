import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import httpx
import pytest

from idi.domain import DomainError
from idi.identity import acting_as
from idi.platform_client import PlatformClient, PlatformWriteError
from tests.test_project_access import make_person


def configured(services, project_id, name, requests, *, fail=False):
    accounts = services['project_accounts']
    accounts.write_enabled = True

    def transport(request):
        requests.append({'account': name, 'authorization': request.headers.get('authorization'),
                         'path': request.url.path, 'payload': json.loads(request.content)})
        if fail:
            raise httpx.ReadTimeout('test response unknown')
        return httpx.Response(200, json={'code': 0, 'data': {}})

    accounts.client_factory = lambda: PlatformClient(httpx.MockTransport(transport), write_enabled=True)
    accounts.configure(project_id, {'username': name, 'password': 'website-test-password', 'auto_connect': False})
    account = accounts.account(project_id)
    account.client._token = 'token-' + name
    account.client._account = name
    return account


def test_company_is_permanently_read_only_and_unbound_write_cannot_fallback(services, project, case):
    accounts = services['project_accounts']
    calls = []
    configured(services, project['id'], 'project-a', calls)
    company = accounts.company.client
    company.write_enabled = True
    company._token, company._account = 'company-token', 'company'
    with pytest.raises(DomainError, match='公司账号仅用于'):
        company.submit('survey', {'state': 1})
    with pytest.raises(DomainError, match='缺少项目'):
        accounts.router.save_draft_only('survey', {'state': 0})
    assert not calls


def test_two_projects_retain_independent_tokens_and_audit_actor_and_uploader(services, project, case):
    calls = []
    configured(services, project['id'], 'project-a', calls)
    second = services['domain'].save_project({'name': '第二项目'})
    other_case = services['domain'].save_case({'project_id': second['id'], 'title': '第二案件'})
    configured(services, second['id'], 'project-b', calls)
    accounts = services['project_accounts']
    manager = make_person(services, project['id'], 'manager', 'manager')
    uploader = make_person(services, project['id'], 'member', 'uploader')
    from tests.conftest import image_bytes
    with acting_as(services['access'].principal(uploader['id'])):
        photo_id = services['importer'].add_file(image_bytes(), '现场.png', case_id=case['id'])['photo_id']
    record = {'snapshot': json.dumps({'write_binding': accounts.binding(project['id']),
        'file_fields': [{'selected': [{'id': photo_id}]}]})}
    with acting_as(services['access'].principal(manager['id'])):
        with accounts.bind(case['id'], 'automatic_save', record):
            accounts.router.save_draft_only('survey', {'state': 0, 'reportReceivingId': 'case-a'})
    # A separate project operation never receives project A's cookie/token.
    with ThreadPoolExecutor(max_workers=2) as pool:
        def save(case_id):
            with accounts.bind(case_id, 'automatic_save'):
                return accounts.router.save_draft_only('survey', {'state': 0})
        list(pool.map(save, [case['id'], other_case['id']]))
    assert {r['authorization'] for r in calls} == {'Bearer token-project-a', 'Bearer token-project-b'}
    assert all(r['authorization'] == 'Bearer token-' + r['account'] for r in calls)
    row = services['db'].one('SELECT * FROM website_operations WHERE actor_id=?', (manager['id'],))
    assert row['website_account'] == 'project-a' and row['actor_name'] == manager['display_name']
    assert json.loads(row['photo_ids']) == [photo_id]
    assert json.loads(row['source_users']) == [{'id': uploader['id'], 'display_name': uploader['display_name']}]
    assert row['state'] == 'written'
    assert 'token-' not in json.dumps(services['db'].all('SELECT * FROM website_operations'))


def test_company_credentials_cannot_be_assigned_to_project_or_vice_versa(services, project):
    accounts = services['project_accounts']
    accounts.company.configure({'username': 'company', 'password': 'company-test-password', 'auto_connect': False})
    with pytest.raises(DomainError, match='不能使用公司'):
        accounts.configure(project['id'], {'username': 'COMPANY', 'password': 'project-test-password', 'auto_connect': False})
    calls = []
    configured(services, project['id'], 'project-a', calls)
    with pytest.raises(DomainError, match='不能与已配置'):
        accounts.company.configure({'username': 'project-a', 'password': 'company-test-password', 'auto_connect': False})
    assert not calls


def test_unconfigured_project_offline_and_changed_binding_never_fallback(services, project, case):
    calls = []
    accounts = services['project_accounts']
    with accounts.bind(case['id'], 'automatic_save'):
        with pytest.raises(DomainError, match='尚未配置'):
            accounts.router.save_draft_only('survey', {'state': 0})
    # The unconfigured client was created by bind, replace it with a mocked one.
    accounts.accounts.pop(project['id']).client.close()
    account = configured(services, project['id'], 'project-a', calls)
    old = deepcopy(accounts.binding(project['id']))
    accounts.configure(project['id'], {'username': 'project-a', 'password': 'changed-test-password', 'auto_connect': False})
    with pytest.raises(DomainError, match='已变更'):
        with accounts.bind(case['id'], 'automatic_save', {'snapshot': json.dumps({'write_binding': old})}):
            pytest.fail('Old write must not run')
    with accounts.bind(case['id'], 'automatic_save'):
        with pytest.raises(DomainError, match='连接与配置'):
            accounts.router.save_draft_only('survey', {'state': 0})
    assert not calls


def test_unknown_project_write_is_recorded_and_does_not_touch_company_or_other_project(services, project, case):
    calls = []
    configured(services, project['id'], 'project-a', calls, fail=True)
    accounts = services['project_accounts']
    with pytest.raises(PlatformWriteError):
        with accounts.bind(case['id'], 'automatic_save'):
            accounts.router.save_draft_only('survey', {'state': 0})
    row = services['db'].one('SELECT * FROM website_operations')
    assert row['state'] == 'uncertain'
    assert len(calls) == 1 and calls[0]['account'] == 'project-a'


def test_member_cannot_invoke_writer_even_with_valid_project_client(services, project, case):
    calls = []
    configured(services, project['id'], 'project-a', calls)
    member = make_person(services, project['id'])
    with acting_as(services['access'].principal(member['id'])):
        with pytest.raises(DomainError, match='管理权限'):
            with services['project_accounts'].bind(case['id'], 'automatic_save'):
                pytest.fail('Member cannot request business writes')
    assert not calls


def test_project_credentials_are_separate_encrypted_files_and_survive_reload(services, project):
    calls = []
    account = configured(services, project['id'], 'project-a', calls)
    assert account.store.path.name == 'platform-project-' + project['id'] + '.bin'
    raw = account.store.path.read_bytes()
    assert b'website-test-password' not in raw and b'project-a' not in raw
    from idi.platform_account import PlatformAccount
    restored = PlatformAccount(services['platform'], services['settings'].data_dir, client=account.client, key=project['id'])
    assert restored.status()['username'] == 'project-a'
    assert restored.state_key != services['platform_account'].state_key


def test_project_reconnections_bound_simultaneous_browser_logins(services, monkeypatch):
    registry, accounts = services['project_accounts'], []
    condition, release = threading.Condition(), threading.Event()
    active = maximum = entered = 0
    def login(username, password):
        nonlocal active, maximum, entered
        with condition:
            active += 1
            entered += 1
            maximum = max(maximum, active)
            condition.notify_all()
        assert release.wait(5)
        with condition:
            active -= 1
    for index in range(3):
        project = services['domain'].save_project({'name': f'重连项目 {index}'})
        registry.configure(project['id'], {'username': f'project-{index}',
            'password': 'isolated-site-password', 'auto_connect': False})
        account = registry.account(project['id'])
        monkeypatch.setattr(account.client, 'login', login)
        accounts.append(account)
    try:
        for account in accounts:
            account.request_connect()
        with condition:
            assert condition.wait_for(lambda: entered >= 2, timeout=4)
            assert active == 2
    finally:
        release.set()
        for account in accounts:
            account._attempt_thread.join(timeout=5)
    assert entered == 3 and maximum == 2
