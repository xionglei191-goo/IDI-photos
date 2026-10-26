"""SQLite contention must neither deadlock official reads nor park a daily run."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta

import httpx
import pytest

from idi.domain import DomainError
from idi.platform_client import PlatformClient
from idi.platform_sync import PlatformSync
from tests.test_platform_incremental import remote, run


def test_session_validation_does_not_wait_for_request_budget_writer(services, monkeypatch):
    """Real SQLite locks reproduce DB -> session versus request -> DB ordering."""
    db, workflow = services['db'], services['workflow']
    client = PlatformClient(httpx.MockTransport(lambda _: httpx.Response(
        200, json={'code': 0, 'data': [], 'count': 0})))
    client._token, client._account = 'fixture-token', 'reader'
    monkeypatch.setattr(services['platform'], 'client', client)
    session = client.status()
    connect = db.connect

    def short_timeout():
        connection = connect()
        connection.execute('PRAGMA busy_timeout=400')
        return connection

    monkeypatch.setattr(db, 'connect', short_timeout)
    writer_entered, request_entered = threading.Event(), threading.Event()
    errors = []

    def write_case():
        try:
            with db.tx() as con:
                writer_entered.set()
                assert request_entered.wait(3)
                workflow.synchronizer.require_snapshot_source(con, None, {}, session)
                con.execute("INSERT INTO settings VALUES('contention_case_saved','true')")
        except BaseException as exc:
            errors.append(exc)

    def account_request():
        request_entered.set()
        db.set_setting('contention_request_counted', True)

    def read_official():
        try:
            assert writer_entered.wait(3)
            with client.request_budget(1, account_request):
                client.read('intake_list')
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_case), threading.Thread(target=read_official)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4)
        assert not any(thread.is_alive() for thread in threads)
        assert not errors, [repr(error) for error in errors]
        assert db.setting('contention_case_saved') is True
        assert db.setting('contention_request_counted') is True
    finally:
        client.close()


@pytest.mark.parametrize('mode', ['incremental', 'full'])
def test_database_busy_resumes_checkpoint_after_scheduler_restart(remote, services, monkeypatch, mode):
    first, second = remote.add(1), remote.add(2)
    workflow, db, jobs = services['workflow'], services['db'], services['jobs']
    sync = workflow.synchronizer
    original_item = sync._item
    connect = db.connect

    def short_timeout():
        connection = connect()
        connection.execute('PRAGMA busy_timeout=100')
        return connection

    monkeypatch.setattr(db, 'connect', short_timeout)

    def busy_once(item, *args):
        if item['report_receiving_id'] == second:
            blocker = connect()
            try:
                blocker.execute('BEGIN IMMEDIATE')
                return original_item(item, *args)
            finally:
                blocker.rollback()
                blocker.close()
        return original_item(item, *args)

    monkeypatch.setattr(sync, '_item', busy_once)
    result = run(services, mode)
    assert result['state'] == 'partial' and result['retry_at']
    assert result['errors'] == []
    assert result['remote_written'] is False
    run_id = result['run_id']
    assert {r['report_receiving_id']: r['state'] for r in db.all(
        'SELECT * FROM platform_sync_items WHERE run_id=?', (run_id,))} == {first: 'done', second: 'pending'}
    assert db.setting('platform_incremental_success') is None
    retry = datetime.fromisoformat(result['retry_at'])
    assert retry == sync.clock() + timedelta(seconds=30)
    assert sync.tick(retry - timedelta(seconds=1)) is None
    # A fresh scheduler must recover the stored deadline and skip completed items.
    restarted = PlatformSync(workflow)
    restarted.clock = lambda: retry
    workflow.synchronizer = restarted
    before_requests = list(remote.requests)
    job = restarted.tick()
    assert job and job['payload']['run_id'] == run_id
    assert jobs.run_one('network')
    finished = jobs.get(job['id'])
    assert finished['state'] == 'completed' and not finished['result']['errors']
    assert finished['result']['created'] == 2
    assert all(not path.endswith('/getInfo/' + first) for path, _ in remote.requests[len(before_requests):])
    assert db.one('SELECT COUNT(*) AS n FROM cases')['n'] == 2
    assert db.all('SELECT * FROM website_operations') == []
    assert db.one('SELECT retry_at FROM platform_sync_runs WHERE id=?', (run_id,))['retry_at'] is None


def test_database_busy_backoff_is_persistent_and_bounded(remote, services, monkeypatch):
    remote.add(1)
    sync, db = services['workflow'].synchronizer, services['db']
    clock = sync.clock()
    monkeypatch.setattr(sync, 'clock', lambda: clock)

    def busy(*args):
        raise sqlite3.OperationalError('database table is locked')

    monkeypatch.setattr(sync, '_item', busy)
    run_id = None
    for expected_delay in (30, 60, 120, 240, 300, 300):
        result = run(services, **({'run_id': run_id} if run_id else {}))
        run_id = result['run_id']
        assert result['state'] == 'partial'
        retry = datetime.fromisoformat(result['retry_at'])
        assert retry == clock + timedelta(seconds=expected_delay)
        clock = retry
    assert db.one("SELECT state FROM platform_sync_items WHERE run_id=?", (run_id,))['state'] == 'pending'
    sync.configure({'enabled': False})
    assert sync.tick(clock + timedelta(days=1)) is None


@pytest.mark.parametrize('error', [sqlite3.OperationalError('no such table: missing'),
                                  sqlite3.IntegrityError('constraint failed'),
                                  DomainError('案件来源不一致', 409)])
def test_nontransient_case_errors_are_not_automatically_retried(remote, services, monkeypatch, error):
    remote.add(1)
    sync = services['workflow'].synchronizer

    def fail(*args):
        raise error

    monkeypatch.setattr(sync, '_item', fail)
    result = run(services)
    assert result['state'] == 'partial' and result['retry_at'] is None
    assert len(result['errors']) == 1
    assert sync.tick(sync.clock() + timedelta(hours=1)) is None


def test_publish_lock_wait_retries_without_refetching_done_cases(remote, services, monkeypatch):
    remote.add(1)
    sync = services['workflow'].synchronizer
    publish = sync._publish

    def busy(*args):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(sync, '_publish', busy)
    first = run(services, 'full')
    assert first['state'] == 'partial' and first['retry_at']
    requests = list(remote.requests)
    monkeypatch.setattr(sync, '_publish', publish)
    second = run(services, 'full', run_id=first['run_id'])
    assert second['state'] == 'completed'
    assert remote.requests == requests
    saved = services['db'].one('SELECT checkpoint FROM platform_sync_runs WHERE id=?', (first['run_id'],))
    assert json.loads(saved['checkpoint']).get('database_retries', 0) == 0


def test_resuming_middle_failure_keeps_progress_monotonic(remote, services, monkeypatch):
    remote.add(1)
    middle = remote.add(2)
    remote.add(3)
    workflow = services['workflow']
    sync = workflow.synchronizer
    original_item = sync._item

    def failed_middle(item, *args):
        if item['report_receiving_id'] == middle:
            raise DomainError('案件暂待核对', 409)
        return original_item(item, *args)

    monkeypatch.setattr(sync, '_item', failed_middle)
    progress = []
    first = workflow.sync({'mode': 'full'}, lambda done, total: progress.append((done, total)))
    assert first['state'] == 'partial' and progress[-1] == (2, 3)
    monkeypatch.setattr(sync, '_item', original_item)
    resumed = []
    second = workflow.sync({'mode': 'full', 'run_id': first['run_id']},
                           lambda done, total: resumed.append((done, total)))
    assert second['state'] == 'completed'
    assert resumed == [(2, 3), (3, 3)]
