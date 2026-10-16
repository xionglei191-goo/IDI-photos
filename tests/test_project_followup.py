import io
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from PIL import Image

from idi.connectors.feishu_feedback import FeishuFeedbackClient, UncertainDelivery
from idi.connectors.secret_store import SecretStore, VisionSecretStore
from idi.db import dump, now
from idi.domain import DomainError


class FakeFeedback:
    def __init__(self):
        self.people = [{'open_id': 'ou_lead', 'name': '项目负责人'}]
        self.sent = []
        self.error = None

    def members(self, chat):
        return self.people

    def send_feedback(self, chat, content, delivery_id):
        self.sent.append((chat, json.loads(content), delivery_id))
        if self.error:
            raise self.error
        return 'om_feedback_verified'

    def close(self):
        pass


@pytest.fixture
def followup(services):
    return services['followup']


@pytest.fixture
def case(case, services):
    stage(services, case, 'survey')
    return services['domain'].case(case['id'])


@pytest.fixture
def channel(services, monkeypatch, followup):
    monkeypatch.setattr(SecretStore, 'save', lambda *args: None)
    monkeypatch.setattr(SecretStore, 'exists', lambda *args: True)
    connector = services['connectors'].save({'kind': 'feishu', 'name': '照片收集群', 'enabled': True, 'app_secret': 'test-no-live-secret',
                                          'config': {'app_id': 'cli_test', 'chat_id': 'oc_test', 'chat_name': '照片收集群'}})
    client = FakeFeedback()
    monkeypatch.setattr(followup, 'client', lambda *args: client)
    return connector, client


def bind(followup, channel, project, automatic=True):
    return followup.save_contact(project['id'], {'connector_id': channel[0]['id'], 'open_id': 'ou_lead', 'auto_feedback': automatic})


def stage(services, case, value):
    with services['db'].tx() as con:
        con.execute('UPDATE cases SET workflow_stage=?,updated_at=?,revision=revision+1 WHERE id=?', (value, now(), case['id']))


def confirm_photo_issue(followup, photo_id=None, **overrides):
    issue = next(i for i in followup.issues() if i['photo_id'] and i['issue_key'].endswith(':resolution') and
                 (not photo_id or i['photo_id'] == photo_id))
    data = {'title': '维修部位近照细节不清', 'detail': '主卧墙面的起皮边缘在原图中无法分辨。',
            'action': '请重发这张主卧墙面照片的原图，保留起皮边缘细节。',
            'context_token': issue['feedback']['context_token'], **overrides}
    followup.confirm_issue(issue['id'], data)
    return issue['id']


def test_progress_comes_from_cases_not_finished_photo(services, followup, case, project, photo):
    stage(services, case, 'repair')
    picture = photo(stage='acceptance', role='finished')
    before = services['db'].one('SELECT * FROM cases WHERE id=?', (case['id'],))
    followup.review()
    summary = followup.summary()['projects'][0]
    assert summary['stages'] == {'repair': 1}
    assert summary['photos']['ai_checked'] == 0
    assert before == services['db'].one('SELECT * FROM cases WHERE id=?', (case['id'],))
    assert services['domain'].photo(picture['id'])['blob_hash'] == picture['blob_hash']


def test_missing_photos_are_limited_to_current_stage_and_local_scope(services, followup, case):
    stage(services, case, 'survey')
    followup.review()
    missing = [i for i in followup.issues() if i['state'] == 'open' and i['kind'] == 'missing']
    assert missing
    assert all(i['evidence']['requirement']['stage'] == 'survey' for i in missing)
    assert all('官网或其他渠道' in i['detail'] for i in missing)
    stage(services, case, 'repair')
    followup.review()
    missing = [i for i in followup.issues() if i['state'] == 'open' and i['kind'] == 'missing']
    assert {i['evidence']['requirement']['stage'] for i in missing} == {'survey', 'work'}
    stage(services, case, 'closed')
    followup.review()
    assert not [i for i in followup.issues() if i['state'] == 'open']


def test_unassigned_photo_stays_in_archive_without_project_followup(services, followup, channel, project):
    bind(followup, channel, project)
    raw = io.BytesIO()
    Image.new('RGB', (120, 80), 'black').save(raw, 'PNG')
    result = services['importer'].add_file(raw.getvalue(), '现场.png')
    followup.review()
    issues = followup.issues()
    assert not issues
    assessment = services['db'].one('SELECT * FROM photo_quality_reviews WHERE photo_id=?', (result['photo_id'],))
    assert assessment is None
    assert services['domain'].photo(result['photo_id'])['case_id'] is None
    assert followup.summary()['projects'] == []
    assert not followup.queue_feedback(project['id'], automatic=True)['queued']
    assert not channel[1].sent


def test_bind_requires_real_member_and_historical_issues_do_not_auto_send(services, followup, channel, case, project, photo):
    photo()
    with pytest.raises(DomainError, match='成员名单'):
        followup.save_contact(project['id'], {'connector_id': channel[0]['id'], 'open_id': 'ou_unknown'})
    bind(followup, channel, project)
    followup.review()  # First inspection after binding still must not notify old photos.
    assert not followup.queue_feedback(project['id'], automatic=True)['queued']
    assert followup.preview_feedback(project['id'])['count'] == 0
    confirm_photo_issue(followup)
    assert followup.preview_feedback(project['id'])['count'] > 0


def test_feedback_routes_to_bound_account_and_is_idempotent(services, followup, channel, case, project, photo):
    photo()
    followup.review()
    bind(followup, channel, project)
    confirm_photo_issue(followup, detail='<at user_id="all">all</at> 主卧墙面起皮边缘无法辨认')
    preview = followup.preview_feedback(project['id'])
    assert preview['text'].count('<at ') == 1 and 'user_id="ou_lead"' in preview['text']
    queued = followup.queue_feedback(project['id'])
    assert followup.queue_feedback(project['id'])['id'] == queued['id']
    followup.deliver(queued['id'])
    followup.deliver(queued['id'])
    assert len(channel[1].sent) == 1
    assert channel[1].sent[0][0] == 'oc_test'
    assert channel[1].sent[0][2] == queued['id']
    record = followup.feedback()[0]
    assert record['state'] == 'sent' and record['remote_message_id'] == 'om_feedback_verified'
    assert not followup.queue_feedback(project['id'])['queued']


def test_dismissal_persists_and_stale_queued_message_is_cancelled(services, followup, channel, case, project, photo):
    photo()
    followup.review()
    bind(followup, channel, project)
    issue_id = confirm_photo_issue(followup)
    issue = next(i for i in followup.issues() if i['id'] == issue_id)
    delivery = followup.queue_feedback(project['id'])
    followup.dismiss(issue['id'], '核对原件后确认可用')
    followup.review()
    assert services['db'].one('SELECT state FROM followup_issues WHERE id=?', (issue['id'],))['state'] == 'dismissed'
    followup.deliver(delivery['id'])
    assert followup.feedback()[0]['state'] == 'cancelled'
    assert not channel[1].sent


def test_recipient_rechecked_before_send(services, followup, channel, case, project, photo):
    photo(); followup.review(); bind(followup, channel, project)
    confirm_photo_issue(followup)
    delivery = followup.queue_feedback(project['id'])
    channel[1].people = []
    followup.deliver(delivery['id'])
    assert followup.feedback()[0]['state'] == 'failed'
    assert not channel[1].sent


def test_uncertain_delivery_needs_human_reconciliation(services, followup, channel, case, project, photo):
    photo(); followup.review(); bind(followup, channel, project)
    confirm_photo_issue(followup)
    delivery = followup.queue_feedback(project['id'])
    channel[1].error = UncertainDelivery('模拟连接中断')
    followup.deliver(delivery['id'])
    assert followup.feedback()[0]['state'] == 'unknown'
    with pytest.raises(DomainError, match='结果不确定'):
        followup.retry_feedback(delivery['id'])
    followup.recover(); followup.deliver(delivery['id'])
    assert len(channel[1].sent) == 1
    followup.confirm_feedback(delivery['id'], True)
    assert followup.feedback()[0]['state'] == 'sent'
    assert not followup.queue_feedback(project['id'])['queued']


def test_restart_never_replays_a_send_in_progress(services, followup, channel, case, project, photo):
    photo(); followup.review(); bind(followup, channel, project)
    confirm_photo_issue(followup)
    delivery = followup.queue_feedback(project['id'])
    with services['db'].tx() as con:
        con.execute("UPDATE followup_feedback SET state='sending' WHERE id=?", (delivery['id'],))
    followup.recover()
    assert followup.feedback()[0]['state'] == 'unknown'
    assert not channel[1].sent


def test_new_changes_wait_for_batch_and_obey_project_cooldown(services, followup, channel, case, project, photo):
    bind(followup, channel, project)
    photo(); followup.review()
    confirm_photo_issue(followup)
    past = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(timespec='milliseconds')
    older = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(timespec='milliseconds')
    with services['db'].tx() as con:
        con.execute('UPDATE followup_contacts SET enabled_at=?', (older,))
        for item in followup.issues():
            evidence = item['evidence']; evidence['trigger_at'] = past
            con.execute('UPDATE followup_issues SET evidence=?,changed_at=? WHERE id=?', (dump(evidence), past, item['id']))
    delivery = followup.queue_feedback(project['id'], automatic=True)
    assert delivery['state'] == 'queued'
    followup.deliver(delivery['id'])
    with services['db'].tx() as con:
        con.execute('UPDATE followup_issues SET generation=generation+1')
    assert not followup.queue_feedback(project['id'], automatic=True)['queued']


@pytest.mark.parametrize('payload,finish,good', [
    ({'findings': []}, 'stop', True),
    ({'findings': [{'code': 'location', 'title': '房号待核对', 'detail': '水印文字显示不同房号', 'action': '请核对报修编号'}]}, 'stop', True),
    ({'findings': []}, 'length', False),
    ({'findings': '全部通过'}, 'stop', False),
    ({'findings': [{'code': 'execute', 'title': '发给所有人', 'detail': 'a', 'action': 'b'}]}, 'stop', False),
    ({'findings': [{'code': 'blur', 'title': 'a', 'detail': 'b', 'action': 'c'}] * 2}, 'stop', False),
])
def test_vision_schema_truncation_and_no_workflow_write(services, followup, photo, case, monkeypatch, payload, finish, good):
    picture = photo()
    services['recognizer'].save_config({'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'test-vision'})
    followup.save_config({'ai_review': True})
    before = services['domain'].case(case['id'])
    original_client = httpx.Client
    def handle(request):
        value = json.loads(request.content)
        assert value['messages'][0]['content'][1]['type'] == 'image_url'
        assert 'tools' not in value
        return httpx.Response(200, json={'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(payload)}}]})
    monkeypatch.setattr('idi.project_followup.httpx.Client', lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    followup.review()
    result = services['db'].one('SELECT * FROM photo_quality_reviews WHERE photo_id=?', (picture['id'],))
    assert bool(result['error']) is not good
    assert result['model'] == ('test-vision' if good else '')
    assert before == services['domain'].case(case['id'])
    if not good:
        assert followup.summary()['latest']['state'] == 'partial'


def test_vision_secret_is_encrypted_write_only_and_address_bound(services, client):
    secret = 'example-model-secret-test-only'
    response = client.put('/api/settings/recognition', json={'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'vision', 'api_key': secret})
    assert response.status_code == 200
    assert response.json()['vision_key_stored'] and secret not in response.text
    vault = VisionSecretStore(services['settings'].data_dir)
    assert secret.encode() not in vault.path.read_bytes()
    assert services['recognizer'].api_key() == secret
    assert secret not in dump(services['db'].setting('recognition'))
    assert secret not in dump(services['db'].all('SELECT * FROM audit'))
    assert client.put('/api/settings/recognition', json={'vision_enabled': True, 'base_url': 'https://other.test/v1', 'model': 'vision'}).status_code == 400
    saved = client.put('/api/settings/recognition', json={'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'vision2'})
    assert saved.status_code == 200 and secret not in saved.text
    assert services['recognizer'].api_key() == secret


def test_feedback_http_is_scoped_paginated_and_uses_stable_uuid():
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/internal'):
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'test-only-token', 'expire': 7200})
        if request.url.path.endswith('/members'):
            next_page = request.url.params.get('page_token')
            assert request.url.params['member_id_type'] == 'open_id'
            return httpx.Response(200, json={'code': 0, 'data': {'items': [{'member_id': 'ou_second' if next_page else 'ou_lead', 'name': '负责人'}], 'has_more': not next_page, 'page_token': 'next'}})
        assert request.url.params['receive_id_type'] == 'chat_id'
        body = json.loads(request.content)
        assert body['uuid'] == 'fixed-attempt' and body['receive_id'] == 'oc_test'
        return httpx.Response(200, json={'code': 0, 'data': {'message_id': 'om_sent'}})
    client = FeishuFeedbackClient('cli_test', 'test-only-secret', transport=httpx.MockTransport(handle))
    assert len(client.members('oc_test')) == 2
    assert client.send_feedback('oc_test', '{"text":"test"}', 'fixed-attempt') == 'om_sent'
    assert len([r for r in requests if r.method == 'POST' and r.url.path.endswith('/messages')]) == 1
    client.close()


def test_routes_report_configuration_without_enabling_model_or_sending(client, services):
    assert client.get('/api/followup').json()['config']['ai_review'] is False
    assert client.put('/api/followup/config', json={'ai_review': True}).status_code == 400
    assert client.post('/api/followup/review').status_code == 200
    assert services['db'].one('SELECT COUNT(*) n FROM followup_feedback')['n'] == 0


def test_feedback_preview_cannot_silently_change_recipient_or_issue_list(followup, channel, case, project, photo):
    photo(); followup.review(); bind(followup, channel, project)
    issue_id = confirm_photo_issue(followup)
    preview = followup.preview_feedback(project['id'])
    followup.dismiss(issue_id, '已核对')
    with pytest.raises(DomainError, match='重新预览'):
        followup.queue_feedback(project['id'], preview_token=preview['preview_token'])
    assert not followup.feedback()


def test_source_change_before_send_requires_new_review(services, followup, channel, case, project, photo):
    picture = photo(); followup.review(); bind(followup, channel, project)
    confirm_photo_issue(followup)
    delivery = followup.queue_feedback(project['id'])
    with services['db'].tx() as con:
        con.execute('UPDATE photos SET revision=revision+1 WHERE id=?', (picture['id'],))
    followup.deliver(delivery['id'])
    assert followup.feedback()[0]['state'] == 'failed'
    assert not channel[1].sent
    assert followup.summary()['latest']['state'] == 'queued'


def test_backup_restore_never_resumes_group_notifications(services, followup, channel, case, project, photo, tmp_path):
    from idi.cli import restore
    from idi.db import Database
    photo(); followup.review(); bind(followup, channel, project)
    confirm_photo_issue(followup)
    followup.queue_feedback(project['id'])
    backup = services['reporter'].backup({'backup_id': 'followup-test'}, lambda *a: None)
    target = tmp_path / 'restored'
    restored = restore(services['settings'].data_dir / 'backups' / backup['filename'], target)
    db = Database(target / 'archive.sqlite3')
    assert not restored['connectors_enabled']
    assert db.one('SELECT auto_feedback FROM followup_contacts')['auto_feedback'] == 0
    assert db.one('SELECT state FROM followup_feedback')['state'] == 'cancelled'
    assert db.setting('followup')['ai_review'] is False
    assert db.setting('followup')['auto_review'] is False
    assert not (target / 'secrets').exists()


def test_case_specific_requirements_take_precedence(services, followup, case):
    template = services['domain'].save_template({'name': '本案查勘要求', 'requirements': [
        {'stage': 'survey', 'role': 'problem', 'minimum': 3, 'per_visit': False, 'part': '卫生间'}]})
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey',insurer_template_id=?,revision=revision+1 WHERE id=?", (template['id'], case['id']))
    followup.review()
    issues = followup.issues()
    assert len(issues) == 1
    assert issues[0]['evidence']['template_id'] == template['id']
    assert issues[0]['evidence']['requirement']['minimum'] == 3


def test_cancelled_unsent_feedback_can_be_explicitly_regenerated(followup, channel, case, project, photo):
    photo(); followup.review(); bind(followup, channel, project)
    confirm_photo_issue(followup)
    delivery = followup.queue_feedback(project['id'])
    bind(followup, channel, project, automatic=False)
    assert followup.feedback()[0]['state'] == 'cancelled'
    retry = followup.queue_feedback(project['id'])
    assert retry['state'] == 'queued' and retry['id'] == delivery['id']
    followup.deliver(retry['id'])
    assert len(channel[1].sent) == 1


def test_disabling_ai_does_not_claim_previous_model_issue_resolved(services, followup, photo, monkeypatch):
    photo()
    services['recognizer'].save_config({'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'vision'})
    followup.save_config({'ai_review': True})
    monkeypatch.setattr(followup, '_vision', lambda *args: [{'code': 'ai_subject', 'source': 'ai', 'title': '主体待核对', 'detail': '画面主体不完整', 'action': '补拍全景'}])
    followup.review()
    followup.save_config({'ai_review': False})
    followup.review()
    model_issue = next(i for i in followup.issues() if i['evidence']['source'] == 'ai')
    assert model_issue['state'] == 'open'


def test_official_case_without_short_code_has_identity_and_never_sends_local_archive_gaps(services, followup, channel, case, project):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET short_code=NULL,report_number=NULL,platform_report_code='ZLBH209901010000000003',title='主卫柜门门铰损坏',building='',room='',location_detail='振业颐隽花园小区1/未划分/9F/901',workflow_stage='repair' WHERE id=?", (case['id'],))
    followup.review(); bind(followup, channel, project)
    observations = followup.issues()
    assert observations and all(i['kind'] == 'missing' for i in observations)
    assert all(i['case_context']['code'] == 'ZLBH209901010000000003' and '901' in i['case_context']['location'] for i in observations)
    preview = followup.preview_feedback(project['id'])
    assert preview['count'] == 0 and preview['text'] == ''
    assert preview['excluded']['internal'] == len(observations)
    assert followup.summary()['projects'][0]['feedback_ready'] == 0
    assert not followup.queue_feedback(project['id'])['queued']
    for issue in observations:
        with pytest.raises(DomainError, match='不能证明'):
            followup.confirm_issue(issue['id'], {'title': '补资料', 'detail': '本机未归档', 'action': '补拍', 'context_token': issue['feedback']['context_token']})
    assert not channel[1].sent


def test_image_metrics_are_not_automatically_treated_as_defects(services, followup, channel, case, project, photo):
    bind(followup, channel, project); photo(color='black'); followup.review()
    older = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(timespec='milliseconds')
    past = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(timespec='milliseconds')
    with services['db'].tx() as con:
        con.execute('UPDATE followup_contacts SET enabled_at=?', (older,))
        for issue in followup.issues():
            evidence = {**issue['evidence'], 'trigger_at': past}
            con.execute('UPDATE followup_issues SET evidence=?,changed_at=? WHERE id=?', (dump(evidence), past, issue['id']))
    assert followup.preview_feedback(project['id'])['count'] == 0
    assert not followup.queue_feedback(project['id'], automatic=True)['queued']
    assert all(i['feedback']['state'] == ('review' if i['kind'] == 'photo' else 'internal') for i in followup.issues())
    assert not channel[1].sent


@pytest.mark.parametrize('certainty,code,ready', [('clear', 'subject', True), ('uncertain', 'subject', False), ('clear', 'stage', False)])
def test_only_specific_ai_evidence_is_eligible(services, followup, channel, project, photo, monkeypatch, certainty, code, ready):
    photo()
    services['recognizer'].save_config({'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'vision'})
    followup.save_config({'ai_review': True})
    monkeypatch.setattr(followup, '_vision', lambda *args: [{'code': 'ai_' + code, 'source': 'ai', 'certainty': certainty,
        'title': '墙面维修边缘被遮挡', 'detail': '画面右侧的柜体挡住了主卧墙面起皮边缘。', 'action': '请从柜体左侧拍摄墙面近照，完整露出起皮边缘。'}])
    followup.review(); bind(followup, channel, project)
    preview = followup.preview_feedback(project['id'])
    assert preview['count'] == int(ready)
    if ready:
        assert '主卧、客厅墙面油漆起皮' in preview['text'] and '1栋' in preview['text'] and '201房' in preview['text']
        assert 'AI 可见证据建议' in preview['text'] and '从柜体左侧' in preview['text']


def test_feedback_groups_photo_problems_under_one_concrete_case(services, followup, channel, case, project, photo):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET short_code=NULL,platform_report_code='ZLBH-TEST-901',title='主卫柜门门铰损坏',location_detail='1栋901' WHERE id=?", (case['id'],))
    first = photo(name='柜门近照.png')
    second = photo(name='柜门全景.png', color='black')
    followup.review(); bind(followup, channel, project)
    confirm_photo_issue(followup, first['id'], title='门铰螺钉位置不清', detail='门铰与柜体连接处的螺钉在原图中无法辨认。', action='请重发柜门近照原图，保留螺钉位置细节。')
    confirm_photo_issue(followup, second['id'], title='柜门连接处过暗', detail='柜门内侧连接处处于阴影中，无法辨认连接状态。', action='请补光后拍摄柜门内侧门铰与柜体的连接处。')
    preview = followup.preview_feedback(project['id'])
    assert preview['count'] == 2
    assert preview['text'].count('案件：ZLBH-TEST-901') == 1
    for part in ('1栋901', '主卫柜门门铰损坏', '柜门近照.png', '柜门全景.png', '连接处'):
        assert part in preview['text']
    assert '案件待关联' not in preview['text'] and '本机归档' not in preview['text']


def test_confirmation_is_invalidated_by_photo_or_case_changes(services, followup, channel, case, project, photo):
    picture = photo(); followup.review(); bind(followup, channel, project)
    issue_id = confirm_photo_issue(followup)
    assert followup.preview_feedback(project['id'])['count'] == 1
    old_issue = next(i for i in followup.issues() if i['id'] == issue_id)
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET location_detail='另一位置',revision=revision+1 WHERE id=?", (case['id'],))
    followup.review()
    assert followup.preview_feedback(project['id'])['count'] == 0
    with pytest.raises(DomainError, match='重新打开原图'):
        followup.confirm_issue(issue_id, {'title': '门铰细节不清', 'detail': '连接处模糊', 'action': '补拍连接处', 'context_token': old_issue['feedback']['context_token']})
    confirm_photo_issue(followup)
    with services['db'].tx() as con:
        con.execute('UPDATE photos SET revision=revision+1 WHERE id=?', (picture['id'],))
    followup.review()
    assert followup.preview_feedback(project['id'])['count'] == 0


def test_changed_human_action_cancels_queued_content_and_requires_new_preview(followup, channel, case, project, photo):
    photo(); followup.review(); bind(followup, channel, project)
    confirm_photo_issue(followup)
    old_preview = followup.preview_feedback(project['id'])
    delivery = followup.queue_feedback(project['id'], preview_token=old_preview['preview_token'])
    confirm_photo_issue(followup, action='请补拍墙面全景，包含窗框与起皮位置。')
    with pytest.raises(DomainError, match='重新预览'):
        followup.queue_feedback(project['id'], preview_token=old_preview['preview_token'])
    followup.deliver(delivery['id'])
    assert followup.feedback()[0]['state'] == 'cancelled'
    assert not channel[1].sent
    assert followup.queue_feedback(project['id'])['id'] != delivery['id']


def test_legacy_queued_feedback_is_not_delivered_after_upgrade(services, followup, channel, case, project, photo):
    photo(); followup.review(); bind(followup, channel, project); confirm_photo_issue(followup)
    delivery = followup.queue_feedback(project['id'])
    with services['db'].tx() as con:
        con.execute('UPDATE followup_feedback SET policy_version=1 WHERE id=?', (delivery['id'],))
    followup.recover(); followup.deliver(delivery['id'])
    assert followup.feedback()[0]['state'] == 'cancelled'
    assert not channel[1].sent


def test_unlocated_case_cannot_produce_feedback(services, followup, channel, case, project, photo):
    photo()
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET building='',unit='',room='',location_detail='' WHERE id=?", (case['id'],))
    followup.review(); bind(followup, channel, project)
    assert all(i['feedback']['state'] == ('blocked' if i['kind'] == 'photo' else 'internal') for i in followup.issues())
    assert followup.preview_feedback(project['id'])['count'] == 0
    with pytest.raises(DomainError, match='位置'):
        confirm_photo_issue(followup)


def test_ai_generic_request_is_not_actionable_even_if_marked_clear(services, followup, channel, project, photo, monkeypatch):
    photo()
    services['recognizer'].save_config({'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'vision'})
    followup.save_config({'ai_review': True})
    monkeypatch.setattr(followup, '_vision', lambda *args: [{'code': 'ai_subject', 'source': 'ai', 'certainty': 'clear',
        'title': '资料需要核对', 'detail': '当前照片还需要进一步检查确认。', 'action': '请按要求补充相关资料'}])
    followup.review(); bind(followup, channel, project)
    assert followup.preview_feedback(project['id'])['count'] == 0
    assert next(i for i in followup.issues() if i['evidence']['source'] == 'ai')['feedback']['state'] == 'review'


def set_official_lists(services, case, scopes):
    with services['db'].tx() as con:
        con.execute("INSERT OR IGNORE INTO platform_sync_runs(id,state,started_at) VALUES('scope-test','completed',?)", (now(),))
        con.execute("UPDATE cases SET workflow_source='platform' WHERE id=?", (case['id'],))
        con.execute('DELETE FROM platform_list_entries WHERE case_id=?', (case['id'],))
        for scope in scopes:
            con.execute('INSERT INTO platform_list_entries VALUES(?,?,?,?,?,?,?)',
                        (scope, case['id'], case['id'], 1, '{}', 'scope-test', now()))


@pytest.mark.parametrize('value,eligible', [
    ('survey', True), ('repair', True), ('acceptance', True), ('settlement', True), ('closed', True),
    ('pending_intake', False), ('insurer_intake', False), ('insurer_review', False),
    ('historical_unverified', False), ('direct_payment', False), ('noncoverage', False),
])
def test_local_followup_starts_at_survey_and_keeps_later_repair_stages(services, followup, case, value, eligible):
    stage(services, case, value)
    assert services['domain'].case(case['id'])['followup_eligible'] is eligible
    summary = followup.summary()
    assert summary['scope'] == 'repair'
    assert summary['case_count'] == int(eligible)
    assert len(summary['projects']) == int(eligible)


def test_official_repair_list_controls_scope_even_when_stage_has_not_changed(services, followup, case):
    stage(services, case, 'repair')
    set_official_lists(services, case, ['intake'])
    excluded = followup.signature()
    assert followup.summary()['case_count'] == 0
    assert services['domain'].case(case['id'])['followup_eligible'] is False
    set_official_lists(services, case, ['intake', 'repair'])
    assert followup.signature() != excluded
    assert followup.summary()['projects'][0]['stages'] == {'repair': 1}
    assert services['domain'].case(case['id'])['followup_eligible'] is True


def test_scope_filters_projects_photos_and_ai_without_removing_archive_or_contacts(services, followup, channel, project, case, photo, monkeypatch):
    picture = photo()
    set_official_lists(services, case, ['intake', 'repair'])
    excluded_project = services['domain'].save_project({'name': '仅理赔受理项目'})
    excluded_case = services['domain'].save_case({'project_id': excluded_project['id'], 'title': '待受理事项'})
    stage(services, excluded_case, 'repair')
    set_official_lists(services, excluded_case, ['intake'])
    raw = io.BytesIO()
    Image.new('RGB', (80, 60), 'black').save(raw, 'PNG')
    excluded_photo = services['importer'].add_file(raw.getvalue(), '受理照片.png', case_id=excluded_case['id'])
    bind(followup, channel, excluded_project)
    contact_before = services['db'].one('SELECT * FROM followup_contacts WHERE project_id=?', (excluded_project['id'],))
    services['recognizer'].save_config({'vision_enabled': True, 'base_url': 'https://model.test/v1', 'model': 'vision'})
    followup.save_config({'ai_review': True})
    checked = []
    monkeypatch.setattr(followup, '_vision', lambda p, c: checked.append(p['id']) or [])
    result = followup.review()
    assert checked == [picture['id']]
    assert result['cases'] == 1 and result['photos'] == 1
    summary = followup.summary()
    assert [p['id'] for p in summary['projects']] == [project['id']]
    assert summary['projects'][0]['photos']['n'] == 1
    assert all(i['case_id'] == case['id'] for i in followup.issues())
    assert not followup.queue_feedback(excluded_project['id'], automatic=True)['queued']
    assert services['db'].one('SELECT * FROM followup_contacts WHERE project_id=?', (excluded_project['id'],)) == contact_before
    assert services['domain'].photo(excluded_photo['photo_id'])['case_id'] == excluded_case['id']
    assert len(services['domain'].cases()) == 2
    assert not channel[1].sent


def test_leaving_repair_scope_blocks_stale_confirmation_preview_and_delivery(services, followup, channel, case, project, photo):
    photo()
    set_official_lists(services, case, ['intake', 'repair'])
    followup.review()
    bind(followup, channel, project)
    issue_id = confirm_photo_issue(followup)
    issue = next(i for i in followup.issues() if i['id'] == issue_id)
    preview = followup.preview_feedback(project['id'])
    delivery = followup.queue_feedback(project['id'])
    old_issue = services['db'].one('SELECT * FROM followup_issues WHERE id=?', (issue_id,))
    set_official_lists(services, case, ['intake'])
    assert not followup.issues()
    assert followup.preview_feedback(project['id'])['count'] == 0
    with pytest.raises(DomainError, match='重新预览'):
        followup.queue_feedback(project['id'], preview_token=preview['preview_token'])
    with pytest.raises(DomainError, match='刷新后核对'):
        followup.confirm_issue(issue_id, {'title': '原图问题', 'detail': '原图中维修部位细节不清', 'action': '重发维修部位的原图',
                                        'context_token': issue['feedback']['context_token']})
    followup.deliver(delivery['id'])
    assert followup.feedback()[0]['state'] == 'cancelled'
    assert '查勘维修' in followup.feedback()[0]['error']
    followup.review()
    assert services['db'].one('SELECT * FROM followup_issues WHERE id=?', (issue_id,)) == old_issue
    assert not channel[1].sent


def test_out_of_scope_photo_changes_do_not_trigger_followup_until_case_enters_repair(services, followup, case, photo):
    stage(services, case, 'pending_intake')
    before = followup.signature()
    picture = photo()
    assert followup.signature() == before
    assert followup.review()['photos'] == 0
    stage(services, case, 'survey')
    assert followup.signature() != before
    assert followup.review()['photos'] == 1
    assert any(i['photo_id'] == picture['id'] for i in followup.issues())
