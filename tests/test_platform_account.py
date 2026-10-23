import json
import os
import subprocess
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from idi.domain import DomainError
from idi.platform_account import PlatformAccount, PlatformAccountStore
from idi.platform_client import PlatformClient, PlatformLoginError, PlatformWriteError


USER = 'isolated-system-user'
PASSWORD = 'isolated-password-never-log'


@pytest.fixture
def account(services, monkeypatch):
    # Deterministic test-only cipher for portable lifecycle tests. DPAPI is tested separately.
    monkeypatch.setattr('idi.platform_account.protect', lambda data, decrypt=False: bytes(b ^ 0xA5 for b in data))
    value = services['platform_account']
    yield value
    value.stop()


def configured(value, enabled=True):
    value.store.save(dict(username=USER, password=PASSWORD, auto_connect=enabled, revision='isolated-revision'))
    restored = PlatformAccount(value.platform, value.store.path.parent.parent)
    value.platform.account = restored
    return restored


def fake_login(value, monkeypatch, failure=None):
    calls = []
    def login(username, password):
        calls.append((username, password))
        if failure:
            raise failure
        value.client._token = 'isolated-test-token'
        value.client._account = username
        value.client._connected_at = '2026-10-17T12:00:00+00:00'
        return value.client.status()
    monkeypatch.setattr(value.client, 'login', login)
    return calls


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_actual_dpapi_round_trip_excludes_plaintext(tmp_path):
    store = PlatformAccountStore(tmp_path)
    data = dict(username=USER, password=PASSWORD, auto_connect=True, revision='one')
    store.save(data)
    assert USER.encode() not in store.path.read_bytes()
    assert PASSWORD.encode() not in store.path.read_bytes()
    assert store.load() == data
    assert not list(store.path.parent.glob('*.tmp'))
    store.delete()
    assert store.load() == {}


def test_save_is_write_only_and_credential_not_in_backup(account, client, services):
    result = client.put('/api/platform/account', json={'username': USER, 'password': PASSWORD, 'auto_connect': False})
    assert result.status_code == 200
    status = result.json()
    assert status['configured'] and status['password_stored'] and status['state'] == 'disabled'
    assert 'password' not in status and PASSWORD not in result.text
    assert PASSWORD.encode() not in account.store.path.read_bytes()
    assert PASSWORD not in json.dumps(services['db'].all('SELECT * FROM settings'))
    exported = services['reporter'].backup({}, lambda *_: None)
    path = services['settings'].data_dir / 'backups' / exported['filename']
    with zipfile.ZipFile(path) as archive:
        assert not any('secret' in name for name in archive.namelist())
        assert all(PASSWORD.encode() not in archive.read(name) for name in archive.namelist())
    assert client.get('/api/platform/account').json()['username'] == USER


def test_restart_restores_account_and_expired_session_reconnects(account, monkeypatch):
    restored = configured(account)
    calls = fake_login(restored, monkeypatch)
    restored.tick()
    assert calls == [(USER, PASSWORD)] and restored.status()['state'] == 'connected'
    restored.tick()
    assert len(calls) == 1
    restored.client.logout()  # Same invalidation used by the existing HTTP 401 / business code 2 paths.
    restored.tick()
    assert len(calls) == 2 and restored.status()['session']['connected']
    restored.client.logout()
    restarted = PlatformAccount(restored.platform, restored.store.path.parent.parent)
    restarted.tick()
    assert len(calls) == 3 and restarted.status()['state'] == 'connected'


def test_transient_failures_back_off_across_restart_and_then_recover(account, monkeypatch):
    restored = configured(account)
    current = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    restored.clock = lambda: current
    calls = fake_login(restored, monkeypatch, DomainError('upstream ' + PASSWORD, 502))
    restored.tick()
    status = restored.status()
    assert status['state'] == 'retry_wait' and PASSWORD not in json.dumps(status)
    assert datetime.fromisoformat(status['next_retry_at']) == current + timedelta(seconds=60)
    restarted = PlatformAccount(restored.platform, restored.store.path.parent.parent)
    restarted.clock = lambda: current
    restarted.tick()
    assert len(calls) == 1
    current += timedelta(seconds=60)
    restarted.tick()
    assert len(calls) == 2
    assert datetime.fromisoformat(restarted.status()['next_retry_at']) == current + timedelta(seconds=120)
    good_calls = fake_login(restarted, monkeypatch)
    current += timedelta(seconds=120)
    restarted.tick()
    assert len(good_calls) == 1 and restarted.status()['state'] == 'connected'
    assert not restarted.status()['error'] and not restarted.status()['next_retry_at']


@pytest.mark.parametrize('status', [400, 401, 403])
def test_bad_credentials_stop_automatic_retries_even_after_restart(account, monkeypatch, status):
    restored = configured(account)
    calls = fake_login(restored, monkeypatch, DomainError(PASSWORD, status))
    restored.tick()
    assert restored.status()['state'] == 'needs_attention'
    assert restored.status()['next_retry_at'] is None
    assert PASSWORD not in json.dumps(restored.status())
    restarted = PlatformAccount(restored.platform, restored.store.path.parent.parent)
    for _ in range(3):
        restarted.tick()
    assert len(calls) == 1
    good_calls = fake_login(restarted, monkeypatch)
    restarted.request_connect()
    restarted._attempt_thread.join(timeout=2)
    assert len(good_calls) == 1 and restarted.status()['state'] == 'connected'


def test_explicit_disconnect_survives_restart_and_forget_removes_file(account, monkeypatch):
    restored = configured(account)
    calls = fake_login(restored, monkeypatch)
    restored.tick()
    restored.disconnect()
    restarted = PlatformAccount(restored.platform, restored.store.path.parent.parent)
    restarted.tick()
    assert len(calls) == 1 and restarted.status()['state'] == 'disabled'
    assert not restarted.status()['session']['connected']
    restarted.forget()
    assert not restarted.store.path.exists() and restarted.status()['state'] == 'unconfigured'


def test_blank_password_keeps_secret_but_account_change_requires_new_password(account):
    account.configure({'username': USER, 'password': PASSWORD, 'auto_connect': False})
    account.configure({'username': USER, 'password': '', 'auto_connect': False})
    assert account.store.load()['password'] == PASSWORD
    with pytest.raises(DomainError, match='更换系统账号'):
        account.configure({'username': 'other-user', 'password': '', 'auto_connect': False})
    assert account.store.load()['username'] == USER


@pytest.mark.parametrize('data', [
    {'username': '', 'password': PASSWORD, 'auto_connect': True},
    {'username': USER, 'password': None, 'auto_connect': True},
    {'username': USER, 'password': PASSWORD, 'auto_connect': 'true'},
    {'username': USER, 'password': PASSWORD, 'auto_connect': False, 'token': 'untrusted'},
])
def test_invalid_configuration_never_saved(account, data):
    with pytest.raises(DomainError):
        account.configure(data)
    assert not account.store.path.exists()


def test_encryption_failure_preserves_existing_account_and_session(account, monkeypatch):
    account.configure({'username': USER, 'password': PASSWORD, 'auto_connect': False})
    account.client._token = 'test-token'
    account.client._account = USER
    before = account.store.path.read_bytes()
    def fail(*args, **kwargs):
        raise DomainError('test encryption failure')
    monkeypatch.setattr('idi.platform_account.protect', fail)
    with pytest.raises(DomainError):
        account.configure({'username': 'replacement', 'password': 'replacement-password', 'auto_connect': True})
    assert account.store.path.read_bytes() == before
    assert account.status()['username'] == USER and account.status()['session']['connected']


def test_unreadable_credentials_are_visible_and_never_login(account, monkeypatch):
    account.store.path.parent.mkdir(parents=True, exist_ok=True)
    account.store.path.write_bytes(b'corrupt')
    restored = PlatformAccount(account.platform, account.store.path.parent.parent)
    calls = fake_login(restored, monkeypatch)
    restored.tick()
    assert not calls and restored.status()['state'] == 'needs_attention'
    assert '无法解密' in restored.status()['error']


def test_login_single_flight_status_responsive_and_edits_rejected(account, monkeypatch):
    restored = configured(account)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def login(username, password):
        calls.append(username); entered.set(); assert release.wait(3)
        restored.client._token, restored.client._account = 'test-token', username
    monkeypatch.setattr(restored.client, 'login', login)
    try:
        restored.request_connect()
        assert entered.wait(1)
        assert restored.status()['state'] == 'connecting'
        restored.tick(); restored.request_connect()
        with pytest.raises(DomainError) as caught:
            restored.disconnect()
        assert caught.value.status == 409
        assert calls == [USER]
    finally:
        release.set(); restored._attempt_thread.join(timeout=2)


@pytest.mark.parametrize('username,password', [('temporary-user', 'temporary-password'), (USER, 'new-temporary-password')])
def test_temporary_login_does_not_get_replaced_but_saved_auto_connect_survives_restart(account, monkeypatch, username, password):
    restored = configured(account)
    saved = restored.store.path.read_bytes()
    calls = fake_login(restored, monkeypatch)
    restored.temporary_login(username, password)
    assert restored.status()['auto_connect'] and restored.status()['automatic_paused']
    assert restored.status()['state'] == 'temporary'
    restored.client.logout(); restored.tick()
    assert calls == [(username, password)]
    restored.stop()
    fresh_client = PlatformClient(httpx.MockTransport(lambda _: pytest.fail('no HTTP request expected')))
    restarted = PlatformAccount(restored.platform, restored.store.path.parent.parent, client=fresh_client)
    try:
        restarted_calls = fake_login(restarted, monkeypatch)
        restarted.tick()
        assert restarted_calls == [(USER, PASSWORD)]
        assert restarted.status()['state'] == 'connected' and restarted.status()['auto_connect']
        assert not restarted.status()['automatic_paused']
        assert restarted.store.path.read_bytes() == saved
    finally:
        restarted.stop(); fresh_client.close()


def test_manual_login_with_saved_credentials_keeps_automatic_reconnect(account, monkeypatch):
    restored = configured(account)
    saved = restored.store.path.read_bytes()
    calls = fake_login(restored, monkeypatch)
    restored.temporary_login(' ' + USER + ' ', PASSWORD)
    assert restored.status()['auto_connect'] and not restored.status()['automatic_paused']
    restored.client.logout(); restored.tick()
    assert calls == [(' ' + USER + ' ', PASSWORD), (USER, PASSWORD)]
    assert restored.store.path.read_bytes() == saved


def test_failed_temporary_login_preserves_saved_block_and_automatic_preference(account, monkeypatch):
    restored = configured(account)
    calls = fake_login(restored, monkeypatch, DomainError('rejected', 401))
    restored.tick()
    saved = restored.store.path.read_bytes()
    state = dict(restored._state)
    with pytest.raises(DomainError):
        restored.temporary_login('temporary-user', 'temporary-password')
    restored.tick()
    assert len(calls) == 2
    assert restored._state == state and restored.status()['state'] == 'needs_attention'
    assert restored.status()['auto_connect'] and restored.store.path.read_bytes() == saved


def test_explicit_saved_account_reconnect_ends_temporary_override(account, monkeypatch):
    restored = configured(account)
    calls = fake_login(restored, monkeypatch)
    restored.temporary_login('temporary-user', 'temporary-password')
    restored.request_connect()
    restored._attempt_thread.join(timeout=2)
    assert calls == [('temporary-user', 'temporary-password'), (USER, PASSWORD)]
    assert restored.status()['state'] == 'connected' and not restored.status()['automatic_paused']


@pytest.mark.parametrize('status', [503, 504])
def test_browser_or_verification_issue_requires_manual_retry(account, monkeypatch, status):
    restored = configured(account)
    calls = fake_login(restored, monkeypatch, PlatformLoginError('setup or verification issue', status, retryable=False))
    restored.tick(); restored.tick()
    assert len(calls) == 1 and restored.status()['state'] == 'needs_attention'
    assert restored.status()['next_retry_at'] is None


def test_background_worker_attempts_saved_account_without_browser_request(account, monkeypatch):
    restored = configured(account)
    connected = threading.Event()
    calls = fake_login(restored, monkeypatch)
    original = restored.client.login
    def login(username, password):
        result = original(username, password)
        connected.set()
        return result
    monkeypatch.setattr(restored.client, 'login', login)
    try:
        restored.start()
        assert connected.wait(2)
    finally:
        restored.stop()
    assert calls == [(USER, PASSWORD)] and restored.status()['state'] == 'connected'


def test_client_status_is_responsive_during_login_and_close_discards_late_token(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def run(args, **kwargs):
        entered.set(); assert release.wait(3)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({'token': 'late-token'}), stderr='')
    monkeypatch.setattr('idi.platform_client.subprocess.run', run)
    monkeypatch.setattr('idi.platform_client.shutil.which', lambda _: 'node')
    client = PlatformClient(httpx.MockTransport(lambda _: pytest.fail('no HTTP request expected')))
    errors = []
    def login():
        try:
            client.login(USER, PASSWORD)
        except DomainError as error:
            errors.append(error)
    thread = threading.Thread(target=login)
    try:
        thread.start(); assert entered.wait(1)
        assert not client.status()['connected']
        client.close()
    finally:
        release.set(); thread.join(timeout=2)
    assert not thread.is_alive() and errors and errors[0].status == 409
    assert not client.status()['connected']


def test_unauthorized_write_is_not_replayed_by_reconnection(account, monkeypatch):
    calls = []
    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={'code': 2})
    account.platform.client.close()
    account.platform.client = PlatformClient(httpx.MockTransport(transport), write_enabled=True)
    account.platform.project_accounts = None  # lifecycle of an isolated project writer
    restored = configured(account)
    logins = fake_login(restored, monkeypatch)
    restored.tick()
    with pytest.raises(PlatformWriteError) as caught:
        restored.client.save_draft_only('survey', {'state': 0})
    assert caught.value.rejected and caught.value.status == 401
    restored.tick()
    assert len(logins) == 2 and len(calls) == 1


def test_account_mutations_keep_existing_origin_boundary(account, client):
    data = {'username': USER, 'password': PASSWORD, 'auto_connect': False}
    response = client.put('/api/platform/account', json=data, headers={'Origin': 'https://untrusted.example'})
    assert response.status_code == 403 and not account.store.path.exists()
