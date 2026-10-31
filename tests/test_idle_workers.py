"""Idle workers retain durable claims, bounded recovery and committed wakeups."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from idi.background import BackgroundRuntime
from idi.connectors.feishu import FeishuCapture
from idi.connectors.feishu_worker import run_capture_queue
from idi.db import Database, dump, now
from tests.test_feishu_capture import connector, event, FakeClient
from tests.test_feishu_agent import bot, capture, task_for
from tests.test_resilient_workflow import configured, quality


@pytest.mark.parametrize('name,lane', [('jobs', 'local'), ('jobs', 'network'),
    ('case_agent', None), ('workspace_chat', None), ('photo_ai', None)])
def test_empty_queues_never_take_writer_lock(services, monkeypatch, name, lane):
    worker = services[name]
    monkeypatch.setattr(services['db'], 'tx', lambda: pytest.fail('empty queue took writer lock'))
    if name == 'photo_ai':
        monkeypatch.setattr(worker.runtime, 'status', lambda: pytest.fail('empty queue read model configuration'))
    assert not (worker.run_one(lane) if name == 'jobs' else worker.run_one())


def test_job_claim_rechecks_under_writer_lock_with_concurrent_consumers(services):
    jobs, calls = services['jobs'], []
    jobs.register('idle-test', lambda payload, progress: calls.append(payload['key']))
    task = jobs.enqueue('idle-test', {'key': 'once'})
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: jobs.run_one(), range(6)))
    assert sum(results) == 1 and calls == ['once']
    assert jobs.get(task['id'])['state'] == 'completed'
    assert services['db'].runtime.snapshot()['tasks']['jobs.run']['claims'] == 1


def test_job_lanes_keep_network_work_separate(services):
    jobs, calls = services['jobs'], []
    for kind in ('recognize', 'platform_sync', 'export'):
        jobs.register(kind, lambda payload, progress: calls.append(payload['name']))
    jobs.enqueue('platform_sync', {'name': 'sync'})
    jobs.enqueue('export', {'name': 'official', 'check_official_media': True})
    jobs.enqueue('recognize', {'name': 'local'})
    assert jobs.run_one('local') and calls == ['local']
    assert not jobs.run_one('local')
    assert jobs.run_one('network') and jobs.run_one('network')
    assert sorted(calls) == ['local', 'official', 'sync']


def test_enqueue_between_empty_check_and_wait_is_not_lost(services, monkeypatch):
    jobs, original, calls = services['jobs'], services['jobs'].run_one, []
    jobs.register('idle-test', lambda payload, progress: (calls.append('handled'), jobs._stop.set()) and {})
    def race(lane):
        result = original(lane)
        if not calls and not result:
            calls.append('enqueued')
            jobs.enqueue('idle-test', {})
        return result
    monkeypatch.setattr(jobs, 'run_one', race)
    jobs._loop('local')
    assert calls == ['enqueued', 'handled']
    assert services['db'].runtime.snapshot()['tasks']['jobs.local']['wakeups'] == 1


def test_photo_ai_enqueue_only_wakes_after_outer_transaction_commits(services, photo):
    worker, db, pic = services['photo_ai'], services['db'], photo()
    observed = worker._signal.token()
    with pytest.raises(ValueError), db.tx() as con:
        worker.enqueue('quality', pic, 'rolled-back', connection=con)
        assert worker._signal.token() == observed
        raise ValueError('rollback')
    assert worker._signal.token() == observed
    with db.tx() as con:
        worker.enqueue('quality', pic, 'committed', connection=con)
        assert worker._signal.token() == observed
    assert worker._signal.token() != observed


def test_photo_ai_retry_deadline_and_config_wakeup(services, photo):
    configured(services)
    worker, pic = services['photo_ai'], photo()
    quality(services, pic)
    current = time.time()
    worker.runtime.clock = lambda: current
    with worker.db.tx() as con:
        con.execute('UPDATE photo_ai_tasks SET retry_at=?', (current + 3,))
    assert worker.next_delay() == 3
    observed = worker._signal.token()
    worker.db.set_setting('recognition', {'vision_enabled': False})
    assert worker._signal.token() != observed


def test_photo_ai_queued_work_wakes_at_provider_recovery_deadline(services, photo):
    from idi.model_runtime import config_key
    runtime = configured(services)
    worker, pic = services['photo_ai'], photo()
    quality(services, pic)
    current = time.time()
    runtime.clock = lambda: current
    with worker.db.tx() as con:
        con.execute("INSERT INTO model_health(config_key,state,retry_at) VALUES(?,'paused',?)",
                    (config_key(runtime.config()), current + 3))
    assert not worker.run_one()
    assert worker.next_delay() == 3


def test_connector_supervision_uses_cached_configuration_and_refreshes(services, connector, monkeypatch):
    service = services['connectors']
    service._collectors[connector['id']] = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(service, 'get', lambda *a: pytest.fail('background loaded UI counts'))
    service.supervise_once()
    connections = service.db.connection_count()
    for _ in range(8):
        service.supervise_once()
    assert service.db.connection_count() == connections
    with service.db.tx() as con:
        con.execute('UPDATE connectors SET enabled=0 WHERE id=?', (connector['id'],))
    service.supervise_once()
    assert not service._configs[0]['enabled']
    service._config_at -= 16
    service.supervise_once()
    assert service.db.connection_count() > connections


def test_heartbeat_is_throttled_and_does_not_wake_configuration(services, connector, monkeypatch):
    worker = FeishuCapture(services['connectors'], connector['id'], FakeClient())
    clock, calls = [100.0], []
    monkeypatch.setattr('idi.connectors.feishu.time', SimpleNamespace(monotonic=lambda: clock[0]))
    original = worker.state
    monkeypatch.setattr(worker, 'state', lambda **v: (calls.append(v), original(**v))[1])
    observed = worker.service._config_signal.token()
    for _ in range(25):
        worker.heartbeat()
    assert len(calls) == 1
    clock[0] += 15
    worker.heartbeat()
    assert len(calls) == 2 and worker.service._config_signal.token() == observed


def test_capture_empty_queue_no_writer_and_retry_deadline(services, connector, monkeypatch):
    client = FakeClient()
    worker = FeishuCapture(services['connectors'], connector['id'], client)
    with monkeypatch.context() as patch:
        patch.setattr(worker.db, 'tx', lambda: pytest.fail('empty capture took writer lock'))
        assert not worker.process_one()
    client.fail = {'img_test'}
    worker.receive(event())
    assert worker.process_one()
    with worker.db.tx() as con:
        con.execute('UPDATE feishu_inbox SET retry_at=?', (time.time() + 3,))
    assert 2 < worker.next_delay() <= 3
    client.fail.clear()
    worker.service.retry_feishu(connector['id'])
    assert worker.process_one()
    assert worker.db.one('SELECT state FROM feishu_inbox')['state'] == 'completed'


def test_capture_cycle_uses_deadlines_instead_of_per_second_queries(services, connector, monkeypatch):
    worker = FeishuCapture(services['connectors'], connector['id'], FakeClient())
    clock, waits, history, polls = [100.0], [], [], []
    stop = threading.Event()
    fake = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    monkeypatch.setattr('idi.connectors.feishu_worker.time', fake)
    monkeypatch.setattr('idi.connectors.feishu.time', fake)
    monkeypatch.setattr(worker, 'catch_up', lambda: history.append(clock[0]))
    monkeypatch.setattr(worker, 'process_one', lambda: polls.append(clock[0]) and False)
    monkeypatch.setattr(worker, 'next_delay', lambda: 15)
    def wait(name, signal, observed, delay, guard):
        waits.append(delay)
        clock[0] += delay
        if len(waits) == 4:
            stop.set()
    monkeypatch.setattr(worker.db.runtime, 'wait', wait)
    run_capture_queue(worker, lambda: True, stop)
    assert waits == [15, 15, 15, 15]
    assert polls == [100, 115, 130, 145] and history == [100, 130]


def test_capture_commit_visible_to_other_database_and_heartbeat_is_silent(services, connector):
    service = services['connectors']
    other = Database(service.db.path)
    signal = other.runtime.watch('feishu-capture', {})
    observed = signal.token()
    worker = FeishuCapture(service, connector['id'], FakeClient())
    worker.heartbeat()
    assert signal.token() == observed
    worker.receive(event())
    assert signal.token() != observed
    assert other.one('SELECT state FROM feishu_inbox')['state'] == 'pending'


def test_archive_revision_wakes_and_periodic_file_verification_remains(services, project, monkeypatch):
    worker, calls = services['archive_tree'], []
    def sync(force=False):
        calls.append(force)
        with worker.db.tx() as con:
            con.execute('UPDATE archive_tree_state SET synced_revision=revision')
        return {'error_count': 0}
    monkeypatch.setattr(worker, 'sync', sync)
    monkeypatch.setattr(worker, 'status', lambda: pytest.fail('idle poll loaded full UI status'))
    assert worker.run_once() == 15 and calls == [True]
    assert worker.run_once() == 15 and calls == [True]
    observed = worker._signal.token()
    with worker.db.tx() as con:
        con.execute("UPDATE projects SET name='changed' WHERE id=?", (project['id'],))
    assert worker._signal.token() != observed
    worker.run_once()
    assert calls == [True, False]
    worker._last_scan -= 61
    worker.run_once()
    assert calls == [True, False, False]


def test_archive_busy_slot_and_error_keep_bounded_retry(services, monkeypatch):
    worker = services['archive_tree']
    with worker.db.runtime.slot('occupied'):
        assert worker.run_once() == 1 and worker._first
    monkeypatch.setattr(worker, 'sync', lambda **kw: {'error_count': 1})
    assert worker.run_once() == 15
    monkeypatch.setattr(worker, 'background_sync', lambda **kw: pytest.fail('retried before error deadline'))
    assert worker.run_once() == 15


def test_photo_rules_wake_from_trigger_source_tables(services, photo):
    signal = services['photo_workflow']._signals[False]
    observed = signal.token()
    pic = photo()
    assert signal.token() != observed
    assert services['db'].one('SELECT dirty FROM photo_pipeline WHERE photo_id=?', (pic['id'],))['dirty'] == 1
    observed = signal.token()
    with services['db'].tx() as con:
        con.execute("UPDATE photos SET stage='survey' WHERE id=?", (pic['id'],))
    assert signal.token() != observed


def test_repeated_awaiting_login_result_does_not_wake_itself(bot, case):
    mid = capture(bot, '查询进展')
    bot.schedule_once()
    task = task_for(bot, mid)
    # PhotoWorkflow is attached separately from the website Workflow service.
    from idi.photo_workflow import PhotoWorkflow
    worker = PhotoWorkflow(bot.db, bot.domain, bot.storage, bot.recognizer, bot.platform, bot)
    row = {'id': 'test-intent', 'task_id': task['id'], 'case_id': case['id'], 'stage': 'survey'}
    with bot.db.tx() as con:
        con.execute('INSERT INTO rule_draft_intents(id,intent_key,task_id,case_id,stage,photo_ids,signature,case_signature,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (row['id'], 'test-key', task['id'], case['id'], 'survey', '[]', '', '', now(), now()))
    worker.save_result(row, 'awaiting_login', {'reason': 'waiting'})
    observed = worker._signals[True].token()
    before = bot.db.one('SELECT updated_at FROM rule_draft_intents')
    worker.save_result(row, 'awaiting_login', {'reason': 'waiting'})
    assert worker._signals[True].token() == observed
    assert bot.db.one('SELECT updated_at FROM rule_draft_intents') == before


def test_process_metrics_preserve_main_and_mark_old_children_stale(services):
    runtime = services['db'].runtime
    runtime.count('main-work', 'runs')
    runtime.flush()
    original = (runtime.directory / 'background-metrics.json').read_bytes()
    child = BackgroundRuntime(services['db'])
    child.role = 'feishu-test'
    child.count('capture.process', 'runs')
    child.flush()
    assert (runtime.directory / 'background-metrics.json').read_bytes() == original
    diagnostics = runtime.diagnostics()
    assert len(diagnostics['processes']) == 2
    assert diagnostics['processes'][1]['status'] == 'fresh'
    target = runtime.directory / 'background-metrics-feishu-test.json'
    value = json.loads(target.read_text())
    value['observed_at'] -= 91
    target.write_text(json.dumps(value))
    assert runtime.diagnostics()['processes'][1]['status'] == 'stale'
    target.write_text('{broken')
    assert len(runtime.diagnostics()['processes']) == 1


def test_wait_metrics_and_stopping_do_not_delay_shutdown(services):
    runtime = services['db'].runtime
    signal = runtime.watch('test-wait', {})
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    assert not runtime.wait('test-wait', signal, signal.token(), 15, stop)
    assert time.monotonic() - started < 1
    assert runtime.snapshot()['tasks']['test-wait']['stops'] == 1
