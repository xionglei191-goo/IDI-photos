import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from idi.db import dump, now, uid
from idi.domain import DomainError
from idi.model_runtime import ModelRuntime, ModelUnavailable
from idi.photo_ai import PhotoAI
from tests.conftest import image_bytes
from tests.test_feishu_agent import bot, capture, task_for
from tests.test_platform_preparation import platform, enter_stage, remote_file


def configured(services):
    services['recognizer'].save_config({'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'test-vision'})
    return services['model_runtime']


def envelope(value=None, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(value if value is not None else {'findings': []})}}]}


def mock_model(monkeypatch, handle):
    original = httpx.Client
    calls = []
    def invoke(request):
        calls.append(request)
        return handle(request)
    monkeypatch.setattr('idi.model_runtime.httpx.Client', lambda **kw: original(transport=httpx.MockTransport(invoke), **kw))
    return calls


def request(runtime, *, timeout=.1):
    return runtime.request(runtime.config(), 'test-only', {'model': 'test-vision', 'messages': []}, timeout, capability='photo_quality')


def quality(services, photo):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (photo['case_id'],))
    services['followup'].save_config({'ai_review': True})
    services['followup'].review()
    return services['db'].one('SELECT * FROM photo_ai_tasks WHERE photo_id=? ORDER BY created_at DESC LIMIT 1', (photo['id'],))


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_configuration_errors_pause_shared_provider_without_retry(services, monkeypatch, status):
    runtime = configured(services)
    calls = mock_model(monkeypatch, lambda req: httpx.Response(status, json={'error': 'not exposed'}))
    for _ in range(3):
        with pytest.raises(ModelUnavailable):
            request(runtime)
    assert len(calls) == 1
    assert runtime.status()['state'] == 'configuration_error'
    assert 'not exposed' not in runtime.status()['error']
    runtime.allow_retry()
    assert runtime.status()['state'] == 'unverified'


def test_transient_breaker_retry_after_readonly_probe_and_restart(services, monkeypatch):
    runtime = configured(services)
    clock = [1000.0]
    runtime.clock = lambda: clock[0]
    response = [503, 503, 503, 503, 200]
    calls = mock_model(monkeypatch, lambda req: httpx.Response(response.pop(0), json=envelope()))
    for _ in range(3):
        with pytest.raises(ModelUnavailable):
            request(runtime)
    assert runtime.status()['state'] == 'paused' and len(calls) == 3
    restarted = ModelRuntime(services['db'])
    restarted.clock = runtime.clock
    with pytest.raises(ModelUnavailable):
        request(restarted)
    clock[0] += 61
    assert restarted.status()['state'] == 'recovering'
    with pytest.raises(ModelUnavailable, match='只读'):
        request(restarted)
    with restarted.background(), pytest.raises(ModelUnavailable):
        request(restarted)
    assert services['db'].one('SELECT cooldown FROM model_health')['cooldown'] == 120
    clock[0] += 121
    with restarted.background():
        request(restarted)
    assert restarted.status()['state'] == 'healthy' and len(calls) == 5


def test_429_respects_long_retry_after_and_no_per_photo_probes(services, photo, monkeypatch):
    runtime = configured(services)
    clock = [2000.0]
    runtime.clock = lambda: clock[0]
    calls = mock_model(monkeypatch, lambda req: httpx.Response(429, headers={'Retry-After': '600'}, json={}))
    with pytest.raises(ModelUnavailable):
        request(runtime)
    with pytest.raises(ModelUnavailable, match='仍要求等待'):
        runtime.allow_retry()
    picture = photo()
    quality(services, picture)
    for _ in range(25):
        assert not services['photo_ai'].run_one()
    task = services['db'].one('SELECT * FROM photo_ai_tasks')
    assert task['attempts'] == 0 and task['deferred'] == 1 and len(calls) == 1
    clock[0] += 599
    assert not services['photo_ai'].run_one()
    assert runtime.status()['state'] == 'paused'


@pytest.mark.parametrize('kind', ['json', 'length', 'empty', 'tool_shape'])
def test_http_200_is_not_valid_analysis_and_is_not_mechanically_replayed(services, photo, monkeypatch, kind):
    runtime = configured(services)
    picture = photo()
    task = quality(services, picture)
    def handle(req):
        if kind == 'json':
            return httpx.Response(200, text='not-json')
        value = envelope(finish='length' if kind == 'length' else 'stop')
        if kind == 'empty':
            value['choices'][0]['message']['content'] = ''
        elif kind == 'tool_shape':
            value['choices'][0]['message'] = {'tool_calls': 'broken'}
        return httpx.Response(200, json=value)
    calls = mock_model(monkeypatch, handle)
    assert services['photo_ai'].run_one()
    assert not services['photo_ai'].run_one()
    current = services['db'].one('SELECT * FROM photo_ai_tasks WHERE id=?', (task['id'],))
    assert current['state'] == 'failed' and current['attempts'] == 1
    assert runtime.status()['last_transport_ok'] and not runtime.status()['last_response_ok']
    assert len(calls) == 1
    local = services['db'].one('SELECT * FROM photo_rule_reviews WHERE photo_id=?', (picture['id'],))
    assert not local['error']


def test_model_concurrency_and_late_success_cannot_clear_new_configuration_failure(services, monkeypatch):
    runtime = configured(services)
    entered, release = threading.Event(), threading.Event()
    def handle(req):
        if req.headers['Authorization'] == 'Bearer slow':
            entered.set()
            assert release.wait(5)
            return httpx.Response(200, json=envelope())
        return httpx.Response(401, json={})
    mock_model(monkeypatch, handle)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(runtime.request, runtime.config(), 'slow', {}, .1)
        assert entered.wait(5)
        with pytest.raises(ModelUnavailable):
            request(runtime)
        release.set()
        slow.result()
    assert runtime.status()['state'] == 'configuration_error'
    assert runtime.status()['active'] == 0


def test_background_has_one_slot_foreground_keeps_second_slot(services, monkeypatch):
    runtime = configured(services)
    entered, release = threading.Event(), threading.Event()
    def handle(req):
        if req.headers['Authorization'] == 'Bearer background':
            entered.set()
            assert release.wait(5)
        return httpx.Response(200, json=envelope())
    calls = mock_model(monkeypatch, handle)
    def background():
        with runtime.background():
            return runtime.request(runtime.config(), 'background', {}, .1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(background)
        assert entered.wait(5)
        try:
            with runtime.background(), pytest.raises(ModelUnavailable):
                request(runtime)
            request(runtime)
            assert runtime.status()['background_active'] == 1
        finally:
            release.set()
        running.result()
    assert len(calls) == 2 and runtime.status()['active'] == 0


def test_local_checks_finish_before_any_ai_request_and_success_projects_later(services, photo, monkeypatch):
    runtime = configured(services)
    calls = mock_model(monkeypatch, lambda req: httpx.Response(200, json=envelope()))
    picture = photo()
    task = quality(services, picture)
    assert task['state'] == 'queued' and not calls
    assert services['db'].one('SELECT state FROM photo_rule_reviews')['state'] == 'needs_review'
    assert services['photo_ai'].run_one()
    result = services['followup'].review()
    assert result['ai_states'] == {'completed': 1}
    assert runtime.status()['capabilities']['photo_quality']['state'] == 'response_valid'
    assert services['db'].one('SELECT model FROM photo_quality_reviews')['model'] == 'test-vision'


def test_retry_limit_survives_restart_and_repeated_manual_retry_is_idempotent(services, photo, monkeypatch):
    runtime = configured(services)
    clock = [3000.0]
    runtime.clock = lambda: clock[0]
    calls = mock_model(monkeypatch, lambda req: httpx.Response(503, json={}))
    task = quality(services, photo())
    worker = services['photo_ai']
    for _ in range(3):
        assert worker.run_one()
        clock[0] += 31
        worker = PhotoAI(services['db'], services['domain'], services['storage'], services['recognizer'], services['followup'])
    row = services['db'].one('SELECT * FROM photo_ai_tasks WHERE id=?', (task['id'],))
    assert row['attempts'] == 3 and row['state'] == 'failed'
    clock[0] += 300
    assert not worker.run_one() and len(calls) == 3
    first = worker.retry(task['id'])
    second = worker.retry(task['id'])
    assert first['id'] == second['id'] and first['attempts'] == 0
    assert services['db'].one('SELECT COUNT(*) n FROM photo_ai_tasks')['n'] == 2


@pytest.mark.parametrize('change', ['photo', 'case', 'config', 'disable'])
def test_late_ai_result_is_discarded_after_context_changes(services, photo, monkeypatch, change):
    configured(services)
    picture = photo()
    task = quality(services, picture)
    def handle(req):
        if change == 'photo':
            services['domain'].update_photo(picture['id'], {'photo_group': 'completion', 'confirm': True})
        elif change == 'case':
            services['domain'].save_case({'title': '人工改正了维修事项'}, picture['case_id'])
        elif change == 'config':
            services['recognizer'].save_config({'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'changed'})
        else:
            services['recognizer'].save_config({'vision_enabled': False, 'base_url': 'https://model.test/v1', 'model': 'test-vision'})
        return httpx.Response(200, json=envelope())
    mock_model(monkeypatch, handle)
    services['photo_ai'].run_one()
    row = services['db'].one('SELECT * FROM photo_ai_tasks WHERE id=?', (task['id'],))
    assert row['state'] in ('stale','cancelled','failed') and row['result_json'] == '{}'
    assert not services['db'].one('SELECT id FROM rule_draft_intents')
    assert not services['db'].one('SELECT id FROM feishu_agent_outbox')


def test_one_hundred_pending_analyses_drain_without_historical_scan_or_duplicate_tasks(services, photo, monkeypatch):
    configured(services)
    pictures = [photo(color=(n, n, 10)) for n in range(100)]
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey'")
    services['followup'].save_config({'ai_review': True})
    services['followup'].review()
    calls = mock_model(monkeypatch, lambda req: httpx.Response(200, json=envelope()))
    worker = services['photo_ai']
    for _ in range(40):
        assert worker.run_one()
    worker = PhotoAI(services['db'], services['domain'], services['storage'], services['recognizer'], services['followup'])
    services['followup'].review()
    assert services['db'].one('SELECT COUNT(*) n FROM photo_ai_tasks')['n'] == 100
    for _ in range(60):
        assert worker.run_one()
    assert not worker.run_one() and len(calls) == 100
    assert services['db'].one("SELECT COUNT(*) n FROM photo_ai_tasks WHERE state='completed' AND attempts=1")['n'] == 100
    assert len(pictures) == services['db'].one('SELECT COUNT(*) n FROM photos')['n']


def test_rules_without_ai_preserve_manual_fields_and_excluded_materials(services, case, photo):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    picture = photo(stage='survey', role='problem')
    picture = services['domain'].update_photo(picture['id'], {'confirm': True})
    rules, platform = services['photo_workflow'], services['platform']
    result = rules.process(picture['id'])
    assert result['state'] == 'local_draft'
    draft = platform.draft(case['id'], 'survey')
    assert draft['files']['fileInfoList'] == [picture['id']]
    platform.save_draft(case['id'], 'survey', {'revision': draft['revision'], 'link_revision': 0,
        'values': {**draft['values'], 'defectDesc': '人工填写的原因'}, 'files': {}})
    assert rules.process(picture['id'])['state'] == 'excluded'
    current = platform.draft(case['id'], 'survey')
    assert current['values']['defectDesc'] == '人工填写的原因' and not current['files']
    assert not services['db'].one('SELECT id FROM rule_draft_intents')
    assert not services['db'].one('SELECT * FROM model_health')


def test_new_case_targets_only_unknown_exact_identifier_and_no_ocr_repeat(services, project, monkeypatch):
    result = services['importer'].add_file(image_bytes(), 'unknown.png')
    photo_id = result['photo_id']
    text = '案件编号：ZLBH20269999\n施工内容：查勘问题\n施工区域：9栋999房'
    calls = []
    monkeypatch.setattr(services['recognizer'], 'local_ocr', lambda path: (calls.append(path) or text, []))
    services['recognizer'].recognize(photo_id)
    rules = services['photo_workflow']
    assert rules.process(photo_id)['state'] == 'needs_check'
    assert not services['domain'].photo(photo_id)['case_id']
    case = services['domain'].save_case({'project_id': project['id'], 'platform_report_code': 'ZLBH20269999', 'building': '1', 'room': '201', 'title': '墙面维修'})
    assert services['db'].one('SELECT dirty FROM photo_pipeline WHERE photo_id=?', (photo_id,))['dirty'] == 1
    rules.run_once()
    assert services['domain'].photo(photo_id)['case_id'] == case['id']
    assert services['domain'].case(case['id'])['room'] == '201'
    assert len(calls) == 1
    assert services['db'].one('SELECT identity FROM photo_pipeline WHERE photo_id=?', (photo_id,))['identity'] == 'official_identifier'


@pytest.mark.parametrize('role', ['other', 'unknown'])
def test_checkin_role_and_address_candidates_never_get_automatic_reporting_eligibility(services, case, photo, role):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    picture = photo(stage='survey', role=role)
    # Existing case_id from a candidate matcher is not a strong identity.
    rules = services['photo_workflow']
    assert rules.process(picture['id'])['identity'] == 'unverified'
    picture = services['domain'].update_photo(picture['id'], {'photo_group': 'survey_checkin', 'confirm': True})
    assert rules.process(picture['id'])['state'] == 'needs_check'
    assert not services['db'].one('SELECT * FROM platform_drafts')


def test_missing_original_blocks_rule_preparation_and_photo_ai(services, photo, monkeypatch):
    configured(services)
    picture = photo(stage='survey', role='problem')
    picture = services['domain'].update_photo(picture['id'], {'confirm': True})
    quality(services, picture)
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (picture['blob_hash'],))
    services['storage'].resolve(blob['stored_path']).unlink()
    assert services['photo_workflow'].process(picture['id'])['state'] == 'storage_error'
    calls = mock_model(monkeypatch, lambda req: pytest.fail('Cannot call model for a missing original'))
    services['photo_ai'].run_one()
    assert not calls
    assert services['db'].one('SELECT state FROM photo_ai_tasks')['state'] == 'stale'


def test_authorized_group_rule_save_works_with_ai_off_and_never_submits(bot, platform, services, case, monkeypatch):
    enter_stage(platform, case['id'], 'survey')
    bot.save_binding(bot.test_connector['id'], {'ai_enabled': False})
    mid = capture(bot, '案件编号：ZLBH20260001', images=1, mention=False)
    bot.collect_messages()
    task = task_for(bot, mid)
    photo_id = task['input']['photo_ids'][0]
    monkeypatch.setattr(services['recognizer'], 'local_ocr', lambda path: ('案件编号：ZLBH20260001\n施工内容：查勘问题', []))
    services['recognizer'].recognize(photo_id)
    writes, uploads = [], []
    platform.client.write_enabled = True
    monkeypatch.setattr(bot, 'request_model', lambda *args: pytest.fail('AI is disabled'))
    monkeypatch.setattr(platform.client, 'upload', lambda *args: uploads.append(1) or remote_file('rule-file', 'photo.png'))
    def save(stage, payload):
        assert stage == 'survey' and payload['state'] == 0
        writes.append(payload)
        platform.test_survey.update(payload)
        return {'code': 0}
    monkeypatch.setattr(platform.client, 'save_draft_only', save)
    monkeypatch.setattr(platform.client, 'submit', lambda *args: pytest.fail('No business submission allowed'))
    assert bot.run_one()
    task = task_for(bot, mid)
    assert len(writes) == len(uploads) == 1, task['drafts']
    assert task['drafts'][0]['result']['state'] == 'save_verified'
    assert task['result']['rule_processed'] == 1 and task['state'] == 'completed'
    assert not services['photo_workflow'].save_one(task['id'])
    assert not services['db'].one('SELECT id FROM photo_ai_tasks')
    assert len(writes) == 1


def test_group_rule_intent_revalidates_binding_after_login(bot, platform, services, case, monkeypatch):
    enter_stage(platform, case['id'], 'survey')
    mid = capture(bot, '案件编号：ZLBH20260001', images=1)
    bot.collect_messages()
    task = task_for(bot, mid)
    picture = bot.domain.update_photo(task['input']['photo_ids'][0], {'case_id': case['id'], 'photo_group': 'survey_problem', 'confirm': True})
    rules = services['photo_workflow']
    rules.process_task(task)
    platform.client._token = ''
    assert not rules.save_one(task['id'])
    bot.save_binding(bot.test_connector['id'], {'enabled': False})
    platform.client._token = 'test-token'
    monkeypatch.setattr(platform.client, 'upload', lambda *args: pytest.fail('Binding revoked'))
    assert rules.save_one(task['id'])
    assert services['db'].one('SELECT state FROM rule_draft_intents')['state'] == 'stale'
    assert bot.domain.photo(picture['id'])['case_id'] == case['id']


def test_known_outage_brief_skips_model_and_remains_once(bot, services, project, monkeypatch):
    start = datetime.now(timezone.utc).astimezone(bot.CHINA) if hasattr(bot, 'CHINA') else datetime.now(timezone.utc)
    from idi.feishu_agent import CHINA, stamp
    start = start.astimezone(CHINA).replace(hour=0, minute=0, second=0, microsecond=0)
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_agent_bindings SET activated_at=?', (stamp(start),))
        con.execute('INSERT INTO followup_contacts VALUES(?,?,?,?,?,?,?)', (project['id'], bot.test_connector['id'], 'ou_owner', '负责人', 0, stamp(start), stamp(start)))
    # Disable only the model. Statistics and the original daily schedule survive.
    config = services['db'].setting('recognition')
    services['db'].set_setting('recognition', {**config, 'vision_enabled': False})
    assert bot.binding_status(bot.test_connector)['next_brief_at']
    monkeypatch.setattr(bot, 'request_model', lambda *args: pytest.fail('Known disabled model must be skipped'))
    assert bot.schedule_briefs(start.replace(hour=18, minute=30)) == 1
    bot.run_one()
    task = bot.get(bot.db.one("SELECT id FROM feishu_agent_tasks WHERE kind='brief'")['id'])
    assert task['delivery'] and 'AI' in task['delivery']['text']
    assert task['usage']['requests'] == 0
    services['db'].set_setting('recognition', config)
    assert bot.schedule_briefs(start.replace(hour=18, minute=50)) == 0
    assert not bot.run_one()
    assert bot.db.one('SELECT COUNT(*) n FROM feishu_agent_outbox')['n'] == 1


def test_operations_separates_receipt_model_and_official_state(client, services, photo):
    picture = photo()
    response = client.get('/api/operations')
    assert response.status_code == 200
    data = response.json()
    assert data['storage']['state'] == 'available'
    assert data['model']['state'] == 'unconfigured' and not data['platform']['connected']
    detail = client.get('/api/photos/' + picture['id']).json()['processing']
    assert detail['receipt'] == 'archived' and detail['ai'] is None
    assert client.put('/api/operations/policy', json={'rules_enabled': False}).status_code == 200
    assert services['photo_workflow'].run_once() == 0


def test_paused_historical_rules_and_automatic_review_do_not_scan_old_photos(services, photo, monkeypatch):
    configured(services)
    picture = photo(stage='survey', role='problem')
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey'")
        con.execute("UPDATE photos SET updated_at='2000-01-01T00:00:00+00:00'")
        con.execute("UPDATE photo_pipeline SET state='historical',dirty=0")
    services['followup'].save_config({'ai_review': True})
    run = services['followup'].request_review(automatic=True)
    services['followup'].review(run['id'])
    assert not services['db'].one('SELECT id FROM photo_ai_tasks')
    assert services['photo_workflow'].run_once() == 0
    assert not services['db'].one('SELECT * FROM platform_drafts')
    assert services['domain'].photo(picture['id'])['revision'] == picture['revision']


def test_ai_switch_preserves_existing_daily_window_and_rule_authorization(bot, services, project):
    before = bot.binding(bot.test_connector['id'])
    after = bot.save_binding(bot.test_connector['id'], {'ai_enabled': False})['config']
    assert before['revision'] == after['revision']
    assert before['activated_at'] == after['activated_at']
    assert after['daily_brief'] == before['daily_brief']
    assert after['save_drafts'] == before['save_drafts']
    assert after['images_enabled'] == before['images_enabled']
    assert not after['ai_enabled']


def test_rule_save_unknown_is_not_replayed_after_model_recovers(bot, platform, services, case, monkeypatch):
    from idi.platform_client import PlatformWriteError
    enter_stage(platform, case['id'], 'survey')
    mid = capture(bot, '案件编号：ZLBH20260001', images=1)
    bot.collect_messages()
    task = task_for(bot, mid)
    bot.domain.update_photo(task['input']['photo_ids'][0], {'case_id': case['id'], 'photo_group': 'survey_problem', 'confirm': True})
    rules = services['photo_workflow']
    rules.process_task(task)
    platform.client.write_enabled = True
    writes = []
    monkeypatch.setattr(platform.client, 'upload', lambda *args: remote_file('unknown-upload', 'photo.png'))
    def unknown(*args):
        writes.append(args)
        raise PlatformWriteError('请求后的连接中断，结果未知')
    monkeypatch.setattr(platform.client, 'save_draft_only', unknown)
    assert rules.save_one(task['id'])
    intent = services['db'].one('SELECT * FROM rule_draft_intents')
    assert intent['state'] == 'uncertain' and len(writes) == 1, intent
    services['model_runtime'].allow_retry()
    rules.process_task(task)
    assert not rules.save_one(task['id'])
    assert len(writes) == 1 and services['feishu_agent'].reviews.get(intent['review_id'])['state'] == 'uncertain'


def test_later_photo_reuses_existing_verified_upload_receipt(services, platform, case, photo, monkeypatch):
    from idi.draft_materials import prepare_materials
    enter_stage(platform, case['id'], 'survey')
    first = photo(color='red', stage='survey', role='problem')
    second = photo(color='green', stage='survey', role='problem')
    platform.client.write_enabled = True
    uploads = []
    def upload(path, filename, mime):
        uploads.append(path)
        return remote_file('file-' + str(len(uploads)), filename)
    def save(stage, payload):
        assert payload['state'] == 0
        platform.test_survey.update(payload)
        return {'code': 0}
    monkeypatch.setattr(platform.client, 'upload', upload)
    monkeypatch.setattr(platform.client, 'save_draft_only', save)
    reviews = services['reviews']
    for incoming in ([first], [second]):
        current = services['domain'].case(case['id'])
        prepared = prepare_materials(platform, current, incoming, actor='rule')
        if incoming == [second]:
            # A user explicitly includes the first photo again. Reuse its verified
            # receipt instead of re-uploading the same original in a new batch.
            draft = platform.draft(case['id'], 'survey')
            draft['files']['fileInfoList'].append(first['id'])
            plan = platform.save_draft(case['id'], 'survey', {
                'values': draft['values'], 'files': draft['files'],
                'revision': draft['revision'], 'link_revision': platform.link(case['id'])['revision']})
            prepared['draft_revision'] = plan['draft']['revision']
        record = reviews.prepare(case['id'], 'survey', prepared['draft_revision'])
        saved = reviews.save_for_agent(record['id'], record['fingerprint'])
        assert saved['state'] == 'save_verified'
    assert len(uploads) == 2  # One upload per original, across two saved batches.
    assert saved['result']['reused_uploads'] == [first['id']]
    ids = [v['id'] for v in platform.test_survey['fileInfoList']]
    assert ids.count('file-1') == ids.count('file-2') == 1


def test_backup_restore_pauses_new_workers_and_preserves_unknown_operations(bot, platform, services, case, tmp_path):
    from idi.cli import restore
    from idi.db import Database
    enter_stage(platform, case['id'], 'survey')
    mid = capture(bot, '案件编号：ZLBH20260001', images=1)
    bot.collect_messages()
    task = task_for(bot, mid)
    picture = bot.domain.update_photo(task['input']['photo_ids'][0], {'case_id': case['id'], 'photo_group': 'survey_problem', 'confirm': True})
    services['photo_workflow'].process_task(task)
    services['photo_ai'].classification(task, picture)
    with services['db'].tx() as con:
        con.execute("UPDATE rule_draft_intents SET state='running'")
    backup = services['reporter'].backup({}, lambda *args: None)
    target = tmp_path / 'restored'
    restore(services['settings'].data_dir / 'backups' / backup['filename'], target)
    restored = Database(target / 'archive.sqlite3')
    assert restored.setting('workflow_policy')['rules_enabled'] is False
    assert restored.setting('workflow_policy')['auto_drafts'] is False
    assert restored.one('SELECT state FROM photo_ai_tasks')['state'] == 'cancelled'
    assert restored.one('SELECT state FROM rule_draft_intents')['state'] == 'needs_check'
    assert restored.one('SELECT enabled FROM feishu_agent_bindings')['enabled'] == 0


def test_archive_failure_never_claims_photo_saved(services, monkeypatch):
    def full(*args):
        raise OSError('disk full')
    monkeypatch.setattr(services['storage'], '_atomic', full)
    with pytest.raises(OSError):
        services['importer'].add_file(image_bytes(), 'new.png')
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 0
    assert services['db'].one('SELECT COUNT(*) n FROM photo_sources')['n'] == 0


def test_recovered_quality_findings_are_internal_until_explicit_human_review(bot, services, photo, project, monkeypatch):
    configured(services)
    picture = photo()
    task = quality(services, picture)
    with services['db'].tx() as con:
        con.execute('UPDATE photo_ai_tasks SET deferred=1')
        con.execute('INSERT INTO followup_contacts VALUES(?,?,?,?,?,?,?)', (project['id'], bot.test_connector['id'], 'ou_owner', '负责人', 1, '2000-01-01T00:00:00+00:00', now()))
    findings = {'findings': [{'code': 'subject', 'certainty': 'clear', 'title': '墙面边缘被遮挡',
        'detail': '右侧木板挡住了维修墙面起皮的边缘位置。', 'action': '请从木板侧面拍摄墙面，让起皮边缘全部露出。'}]}
    mock_model(monkeypatch, lambda req: httpx.Response(200, json=envelope(findings)))
    assert services['photo_ai'].run_one()
    services['followup'].review()
    with services['db'].tx() as con:
        con.execute("UPDATE followup_issues SET changed_at='2026-01-01T00:00:00+00:00'")
    candidate = next(i for i in services['followup'].issues() if i['evidence']['source'] == 'ai')
    assert candidate['evidence']['recovered_analysis']
    assert services['followup'].preview_feedback(project['id'], automatic=True)['count'] == 0
    assert services['followup'].preview_feedback(project['id'], automatic=False)['count'] == 1


def test_model_config_repair_allows_explicit_retry_without_old_result_projection(services, photo, monkeypatch):
    configured(services)
    task = quality(services, photo())
    responses = [401, 200]
    calls = mock_model(monkeypatch, lambda req: httpx.Response(responses.pop(0), json=envelope()))
    assert services['photo_ai'].run_one()
    services['recognizer'].save_config({'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'repaired-model'})
    retried = services['photo_ai'].retry(task['id'])
    assert retried['id'] != task['id'] and retried['config_key'] != task['config_key']
    assert services['photo_ai'].run_one()
    result = services['followup'].review()
    assert result['ai_states'] == {'completed': 1} and len(calls) == 2
    assert services['db'].one('SELECT state FROM photo_ai_tasks WHERE id=?', (task['id'],))['state'] == 'cancelled'
    assert services['db'].one('SELECT model FROM photo_quality_reviews')['model'] == 'repaired-model'


def test_restart_recovers_running_analysis_without_resetting_attempts(services, photo, monkeypatch):
    configured(services)
    task = quality(services, photo())
    with services['db'].tx() as con:
        con.execute("UPDATE photo_ai_tasks SET state='running',attempts=2")
    calls = mock_model(monkeypatch, lambda req: httpx.Response(503, json={}))
    worker = PhotoAI(services['db'], services['domain'], services['storage'], services['recognizer'], services['followup'])
    monkeypatch.setattr(worker, '_loop', lambda: None)
    worker.start()
    try:
        assert worker.run_one()
    finally:
        worker.stop()
    recovered = services['db'].one('SELECT * FROM photo_ai_tasks WHERE id=?', (task['id'],))
    assert recovered['state'] == 'failed' and recovered['attempts'] == 3 and len(calls) == 1


def test_blocked_model_does_not_block_archive_or_official_read(services, platform, case, photo, monkeypatch):
    configured(services)
    enter_stage(platform, case['id'], 'survey')
    task = quality(services, photo())
    entered, release = threading.Event(), threading.Event()
    def slow(req):
        entered.set()
        assert release.wait(10)
        return httpx.Response(503, json={})
    mock_model(monkeypatch, slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(services['photo_ai'].run_one)
        assert entered.wait(5)
        try:
            added = services['importer'].add_file(image_bytes('coral'), 'during-outage.png')
            assert services['storage'].verify(services['domain'].photo(added['photo_id']))
            services['workflow'].refresh_case(case['id'])
            assert services['db'].one('SELECT state FROM photo_ai_tasks WHERE id=?', (task['id'],))['state'] == 'running'
        finally:
            release.set()
        assert running.result()
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 2


def test_group_auto_save_cannot_inherit_local_upload_selection(bot, platform, services, case, photo, monkeypatch):
    enter_stage(platform, case['id'], 'survey')
    manual = photo(color='orange', stage='survey', role='problem')
    manual = services['domain'].update_photo(manual['id'], {'confirm': True})
    rules = services['photo_workflow']
    rules.process(manual['id'])
    mid = capture(bot, '案件编号：ZLBH20260001', images=1)
    bot.collect_messages()
    task = task_for(bot, mid)
    services['domain'].update_photo(task['input']['photo_ids'][0], {'case_id': case['id'], 'photo_group': 'survey_problem', 'confirm': True})
    rules.process_task(task)
    platform.client.write_enabled = True
    monkeypatch.setattr(platform.client, 'upload', lambda *args: pytest.fail('Local selection is outside this group authorization'))
    assert rules.save_one(task['id'])
    intent = services['db'].one('SELECT * FROM rule_draft_intents')
    assert intent['state'] == 'stale' and '授权范围外' in json.loads(intent['result_json'])['reason']
    selected = platform.draft(case['id'], 'survey')['files']['fileInfoList']
    assert set(selected) == {manual['id'], task['input']['photo_ids'][0]}
