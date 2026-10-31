"""Transaction isolation, committed wakeups and bounded background diagnostics."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from idi.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'signals.sqlite3')
    with database.read() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE item(id INTEGER PRIMARY KEY,value TEXT,checked_at INTEGER)')
        con.execute("INSERT INTO item VALUES(1,'old',0)")
    return database


def test_committed_changes_notify_once_after_visibility_and_rollback_is_silent(db):
    observed = []
    db.subscribe({'item': None}, lambda: observed.append(db.one('SELECT value FROM item')['value']))
    with db.connection_scope():
        with db.tx() as con:
            con.execute("UPDATE item SET value='new' WHERE id=1")
            con.execute('UPDATE item SET checked_at=1 WHERE id=1')
            assert observed == []
        assert observed == ['new']
        with pytest.raises(ValueError), db.tx() as con:
            con.execute("UPDATE item SET value='rollback' WHERE id=1")
            raise ValueError('rollback')
    assert observed == ['new']
    assert db.one('SELECT value FROM item')['value'] == 'new'


def test_noop_and_audit_columns_do_not_wake_business_subscriber(db):
    changes = []
    db.subscribe({'item': {'value'}}, lambda: changes.append(True))
    with db.tx() as con:
        con.execute("UPDATE item SET value='unused' WHERE id=2")
        con.execute('UPDATE item SET checked_at=1 WHERE id=1')
    assert changes == []
    with db.tx() as con:
        con.execute("UPDATE item SET VALUE='business' WHERE id=1")
    assert changes == [True]


@pytest.mark.parametrize('method', ['sql', 'autocommit', 'script', 'savepoint'])
def test_other_commit_forms_publish(db, method):
    observed = []
    db.subscribe({'item': None}, lambda: observed.append(True))
    with db.read() as con:
        if method == 'sql':
            con.execute('BEGIN')
            con.execute("UPDATE item SET value='sql'")
            assert not observed
            con.execute('COMMIT')
        elif method == 'autocommit':
            con.execute("UPDATE item SET value='autocommit'")
        elif method == 'savepoint':
            con.execute('SAVEPOINT work')
            con.execute("UPDATE item SET value='savepoint'")
            assert not observed
            con.execute('RELEASE work')
        else:
            con.executescript("BEGIN; UPDATE item SET value='script'; COMMIT;")
    assert observed == [True]


def test_partial_autocommit_batch_failure_still_signals_committed_row(db):
    observed = []
    db.subscribe({'item': None}, lambda: observed.append(True))
    with pytest.raises(sqlite3.IntegrityError), db.read() as con:
        con.executemany('INSERT INTO item VALUES(?,?,?)', [(2, 'saved', 0), (1, 'duplicate', 0)])
    assert observed == [True]
    assert db.one('SELECT value FROM item WHERE id=2')['value'] == 'saved'


def test_sql_rollback_and_subscriber_failure_cannot_change_committed_result(db):
    signal = db.runtime.watch('test', {'item': None})
    original = signal.token()
    with db.read() as con:
        con.execute('BEGIN')
        con.execute("UPDATE item SET value='discard'")
        con.execute('ROLLBACK')
    assert signal.token() == original
    db.subscribe({'item': None}, lambda: (_ for _ in ()).throw(ValueError('subscriber failed')))
    with db.tx() as con:
        con.execute("UPDATE item SET value='committed'")
    assert db.one('SELECT value FROM item')['value'] == 'committed'
    assert signal.token() != original


def test_idle_connection_reuse_never_reuses_an_active_transaction(db):
    with db.connection_scope():
        with db.read() as first:
            pass
        with db.read() as reused:
            assert first is reused
        with db.tx() as writer:
            writer.execute("UPDATE item SET value='uncommitted'")
            with db.read() as reader:
                assert reader is not writer
                assert reader.execute('SELECT value FROM item').fetchone()[0] == 'old'
        assert db.one('SELECT value FROM item')['value'] == 'uncommitted'
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute('SELECT 1')


def test_read_snapshot_is_consistent_and_query_only_resets_before_reuse(db):
    other = Database(db.path)
    with db.connection_scope():
        with db.read_snapshot() as snapshot:
            assert db.one('SELECT value FROM item')['value'] == 'old'
            with other.tx() as writer:
                writer.execute("UPDATE item SET value='external'")
            with db.read_snapshot() as nested:
                assert nested is snapshot
                assert db.one('SELECT value FROM item')['value'] == 'old'
            with pytest.raises(RuntimeError), db.tx():
                pass
            with pytest.raises(sqlite3.OperationalError):
                snapshot.execute("UPDATE item SET value='forbidden'")
        with db.tx() as writer:
            assert writer is snapshot
            writer.execute("UPDATE item SET value='allowed'")
    assert db.one('SELECT value FROM item')['value'] == 'allowed'


def test_exception_scope_closes_connection_and_does_not_retain_snapshot(db):
    with pytest.raises(ValueError), db.connection_scope(), db.read_snapshot() as snapshot:
        raise ValueError('test')
    with pytest.raises(sqlite3.ProgrammingError):
        snapshot.execute('SELECT 1')
    with db.tx() as con:
        con.execute("UPDATE item SET value='still writable'")


def test_connection_scopes_are_thread_local(db):
    barrier = threading.Barrier(2)
    def worker():
        with db.connection_scope(), db.read() as con:
            barrier.wait(timeout=5)
            return con, db.one('SELECT value FROM item')['value']
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(worker) for _ in range(2)]
        first, second = [f.result(timeout=10) for f in futures]
    assert first[0] is not second[0]
    assert first[1] == second[1] == 'old'


def test_shared_signal_wakes_another_database_instance_and_does_not_lose_inflight_event(db):
    other = Database(db.path)
    receiver = db.runtime.watch('cross-process', {'item': None})
    other.runtime.watch('cross-process', {'item': None})
    observed = receiver.token()
    with other.tx() as con:
        con.execute("UPDATE item SET value='other process'")
    assert receiver.token() != observed
    assert receiver.wait(observed, 5, threading.Event())
    stop = threading.Event()
    stop.set()
    assert not receiver.wait(receiver.token(), 5, stop)


def test_metrics_are_bounded_and_slot_releases_after_failure(db, monkeypatch):
    from itertools import count
    clock = count(step=.2)
    monkeypatch.setattr('idi.background.time.thread_time', lambda: next(clock))
    for index in range(120):
        with db.runtime.measure('case.check', str(index)), db.connection_scope():
            db.one('SELECT value FROM item')
    snapshot = db.runtime.snapshot()
    assert len(snapshot['slow_tasks']) == 100
    assert snapshot['tasks']['case.check']['runs'] == 120
    assert snapshot['tasks']['case.check']['connections'] == 120
    db.runtime.flush()
    saved = json.loads((db.runtime.directory / 'background-metrics.json').read_text())
    assert saved['tasks'] == snapshot['tasks']
    with pytest.raises(ValueError), db.runtime.slot('first') as acquired:
        assert acquired
        with db.runtime.slot('second') as deferred:
            assert not deferred
        raise ValueError('release')
    with db.runtime.slot('third') as acquired:
        assert acquired
    assert db.runtime.snapshot()['tasks']['second']['deferred'] == 1
