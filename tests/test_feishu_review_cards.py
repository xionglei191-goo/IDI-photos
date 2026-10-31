import json
import time
from copy import deepcopy

import httpx
import pytest

from idi.connectors.feishu_feedback import FeishuFeedbackClient
from idi.db import dump, uid
from idi.domain import DomainError
from idi.identity import acting_as
from idi.platform_client import PlatformWriteError
from idi.platform_contract import STAGES, visible
from idi.review_card_content import build_card
from tests.test_review_notifications import notification_flow, records
from tests.test_automatic_uploads import setup_flow, ready, tick
from tests.test_platform_preparation import platform, draft_data
from tests.test_project_followup import followup, channel, bind


def event(delivery, action='submit', *, open_id='ou_lead', form=None, **value):
    result = {'schema': '2.0',
              'header': {'event_id': uid(), 'event_type': 'card.action.trigger', 'app_id': delivery['app_id'], 'tenant_key': 'tenant'},
              'event': {'operator': {'open_id': open_id, 'tenant_key': 'tenant'}, 'host': 'im_message',
                        'context': {'open_chat_id': delivery['chat_id'], 'open_message_id': delivery['remote_message_id']},
                        'action': {'tag': 'button', 'value': {'notification_id': delivery['id'], 'card_token': delivery['card_token'],
                                                             'action': action, 'acknowledged': True, **value}}}}
    if form is not None:
        result['event']['action']['form_value'] = form
    return result


def receive(cards, delivery, payload):
    return cards.receive_callback(delivery['connector_id'], delivery['app_id'], payload)


def actions(cards):
    return cards.db.all('SELECT * FROM feishu_review_actions ORDER BY created_at,id')


def business_submits(writes):
    return [p for _, p in writes if p['state'] == 1]


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    if isinstance(value, list):
        for child in value:
            yield from walk(child)


@pytest.fixture
def bound_cards(notification_flow, services, admin_user, channel):
    notifier, auto, feedback, writes = notification_flow
    services['feishu_identities'].save({'connector_id': channel[0]['id'], 'open_id': 'ou_lead', 'user_id': admin_user['id']})
    assert notifier.run_once() == 1
    return services['review_cards'], records(notifier)[0], feedback, writes


def test_card_contains_fields_preview_images_and_explicit_confirmation(bound_cards):
    cards, delivery, feedback, writes = bound_cards
    body = feedback.cards[0][1]
    assert body['schema'] == '2.0' and body['config']['update_multi']
    assert len(feedback.card_images) == 3
    assert len([n for n in walk(body) if n.get('tag') == 'img']) == 3
    # Live Feishu rejects size='large' with fit_horizontal (230099/10002).
    # Leave dimensions to the complete, uncropped horizontal layout.
    assert all(n['scale_type'] == 'fit_horizontal' and 'size' not in n
               for n in walk(body) if n.get('tag') == 'img')
    submit = next(n for n in walk(body) if n.get('tag') == 'button' and n.get('text', {}).get('content') == '确认正式提交')
    assert submit['confirm'] and not submit.get('disabled')
    assert '实际现场查勘时间' in dump(body) and '缺陷原因分析' in dump(body)
    assert not business_submits(writes)
    assert json.loads(delivery['card_view'])['direct_allowed']


def test_human_submit_is_durable_at_most_once_and_updates_original_card(bound_cards):
    cards, delivery, feedback, writes = bound_cards
    payload = event(delivery)
    assert receive(cards, delivery, payload)['toast']['type'] == 'info'
    assert not business_submits(writes)  # SDK thread has no official writes.
    assert receive(cards, delivery, payload)['toast']['type'] == 'info'
    assert receive(cards, delivery, event(delivery))['toast']['type'] == 'error'
    assert len(actions(cards)) == 1
    cards.run_once()
    job = actions(cards)[0]
    assert job['state'] == 'done', job
    assert len(business_submits(writes)) == 1
    current = cards.delivery(delivery['id'])
    view = json.loads(current['card_view'])
    assert view['verified'] and '确认人员：测试管理员' in view['notice']
    assert len(feedback.updated_cards) >= 2
    assert all(mid == delivery['remote_message_id'] for mid, _ in feedback.updated_cards)
    assert receive(cards, delivery, payload)['toast']['type'] == 'info'
    assert receive(cards, delivery, event(delivery))['toast']['type'] == 'error'
    cards.run_once()
    assert len(business_submits(writes)) == 1
    assert not current['card_dirty']


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('change', ['app', 'chat', 'message', 'token', 'unbound', 'no_acknowledgement', 'host'])
def test_callback_rejects_forged_context_and_unbound_people(bound_cards, change, external):
    cards, delivery, _, writes = bound_cards
    payload = event(delivery)
    if external:
        payload['event']['operator']['tenant_key'] = 'external-tenant'
    if change == 'app':
        payload['header']['app_id'] = 'cli_other'
    elif change == 'chat':
        payload['event']['context']['open_chat_id'] = 'oc_other'
    elif change == 'message':
        payload['event']['context']['open_message_id'] = 'om_forwarded'
    elif change == 'token':
        payload['event']['action']['value']['card_token'] = 'stale'
    elif change == 'unbound':
        payload['event']['operator']['open_id'] = 'ou_stranger'
    elif change == 'no_acknowledgement':
        payload['event']['action']['value']['acknowledged'] = False
    else:
        payload['event']['host'] = 'preview'
    assert receive(cards, delivery, payload)['toast']['type'] == 'error'
    assert not actions(cards) and not business_submits(writes)


@pytest.mark.parametrize('header_tenant', ['app-tenant', None, 'missing'])
def test_external_project_manager_submits_once_after_attachment_timestamp_refresh(
        bound_cards, services, platform, project, header_tenant):
    from tests.test_project_access import make_person

    cards, delivery, _, writes = bound_cards
    manager = make_person(services, project['id'], role='manager', name='external-manager')
    services['feishu_identities'].save({'connector_id': delivery['connector_id'], 'open_id': 'ou_lead',
                                        'user_id': manager['id']})
    payload = event(delivery)
    payload['event']['operator']['tenant_key'] = 'external-tenant'
    if header_tenant == 'missing':
        payload['header'].pop('tenant_key')
    else:
        payload['header']['tenant_key'] = header_tenant
    for value in platform.test_survey.values():
        if isinstance(value, list):
            for item in value:
                item['updateTime'] = '2026-10-31T10:00:01'
    platform.test_survey['updateTime'] = '2026-10-31T10:00:11'
    assert receive(cards, delivery, payload)['toast']['type'] == 'info'
    assert receive(cards, delivery, payload)['toast']['type'] == 'info'
    assert len(actions(cards)) == 1 and not business_submits(writes)
    cards.run_once()
    assert actions(cards)[0]['state'] == 'done', actions(cards)
    assert actions(cards)[0]['user_id'] == manager['id']
    assert len(business_submits(writes)) == 1
    assert json.loads(cards.delivery(delivery['id'])['card_view'])['verified']
    assert receive(cards, delivery, payload)['toast']['type'] == 'info'
    cards.run_once()
    assert len(business_submits(writes)) == 1


def test_upgrade_rebuilds_old_review_and_reuses_original_card_without_submitting(
        bound_cards, services, platform, monkeypatch):
    from idi import automatic_uploads, review_batches

    cards, delivery, feedback, writes = bound_cards
    notifier, auto = cards.notifications, cards.notifications.automatic
    for value in platform.test_survey.values():
        if isinstance(value, list):
            for item in value:
                item['updateTime'] = '2026-10-31T09:36:25'
    platform.test_survey['updateTime'] = '2026-10-31T09:36:25'
    services['workflow'].refresh_case(delivery['case_id'])
    # Reproduce an already-sent review made by the previous comparison rules.
    with monkeypatch.context() as old:
        old.setattr(automatic_uploads, 'REVIEW_RULES_VERSION', 2)
        old.setattr(review_batches, 'attachment_version_data', lambda items: items)
        auto.process(auto.get(delivery['case_id']))
        notifier.run_once()
        cards.synchronize(force=True)
        old_delivery = cards.delivery(delivery['id'])
        old_review = cards.reviews.get(old_delivery['review_id'])
        assert old_review['state'] == 'prepared'
        assert json.loads(old_delivery['card_view'])['mode'] == 'ready'
    assert auto.run_once() >= 1
    notifier.run_once()
    cards.run_once()
    updated = cards.delivery(delivery['id'])
    view = json.loads(updated['card_view'])
    assert updated['review_id'] != old_review['id'] and view['mode'] == 'ready'
    assert cards.reviews.get(old_review['id'])['state'] == 'stale'
    assert view['fingerprint'] == cards.reviews.get(updated['review_id'])['fingerprint']
    assert updated['remote_message_id'] == old_delivery['remote_message_id']
    assert len(feedback.cards) == 1 and not actions(cards) and not business_submits(writes)
    # The new proposal still needs a fresh, explicit human confirmation.
    payload = event(updated)
    payload['event']['operator']['tenant_key'] = 'external-tenant'
    assert receive(cards, updated, payload)['toast']['type'] == 'info'
    cards.run_once()
    assert actions(cards)[0]['state'] == 'done', actions(cards)
    assert len(business_submits(writes)) == 1


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('change', ['unbind', 'disabled', 'member_left', 'policy', 'material', 'binding_revision'])
def test_queued_submission_rechecks_permission_and_materials(bound_cards, services, platform, change, external):
    cards, delivery, feedback, writes = bound_cards
    payload = event(delivery)
    if external:
        payload['event']['operator']['tenant_key'] = 'external-tenant'
    assert receive(cards, delivery, payload)['toast']['type'] == 'info'
    binding = services['db'].one('SELECT * FROM feishu_identity_bindings')
    if change == 'unbind':
        services['feishu_identities'].remove(binding['id'])
    elif change == 'disabled':
        services['access'].save_user({'username': 'backup-admin', 'display_name': '其他管理员',
                                     'password': 'isolated-backup-password', 'system_role': 'admin'})
        services['access'].save_user({'enabled': False}, binding['user_id'])
    elif change == 'member_left':
        feedback.people = []
    elif change == 'policy':
        cards.notifications.automatic.configure({'notify_review_ready': False})
    elif change == 'material':
        data = draft_data(platform, delivery['case_id'], 'survey')
        data['values']['defectDesc'] = '新的现场情况'
        platform.save_draft(delivery['case_id'], 'survey', data)
    else:
        services['feishu_identities'].save({'connector_id': delivery['connector_id'], 'open_id': binding['open_id'], 'user_id': binding['user_id']})
    cards.run_once()
    assert actions(cards)[0]['state'] == 'rejected'
    assert not business_submits(writes)


@pytest.mark.parametrize('external', [False, True])
def test_member_can_view_but_cannot_submit_and_other_project_manager_is_denied(bound_cards, services, project, external):
    cards, delivery, _, writes = bound_cards
    user = services['access'].save_user({'username': 'member-card', 'display_name': '项目成员',
                                       'password': 'isolated-member-password', 'system_role': 'user'})
    services['access'].save_members(project['id'], {'members': [{'user_id': user['id'], 'role': 'member'}]})
    services['feishu_identities'].save({'connector_id': delivery['connector_id'], 'open_id': 'ou_lead', 'user_id': user['id']})
    payload = event(delivery)
    if external:
        payload['event']['operator']['tenant_key'] = 'external-tenant'
    assert receive(cards, delivery, payload)['toast']['type'] == 'error'
    other = services['domain'].save_project({'name': '另一个项目'})
    services['access'].save_members(project['id'], {'members': []})
    services['access'].save_members(other['id'], {'members': [{'user_id': user['id'], 'role': 'manager'}]})
    assert receive(cards, delivery, payload)['toast']['type'] == 'error'
    assert not actions(cards) and not business_submits(writes)


def test_native_edit_then_preview_then_submit_preserves_the_confirmed_values(bound_cards, services):
    cards, delivery, feedback, writes = bound_cards
    original = json.loads(delivery['content'])
    original_fields = [n['element_id'] for n in walk(original) if n.get('element_id', '').startswith('review_field_')]
    original_images = [n['img_key'] for n in walk(original) if n.get('tag') == 'img']
    assert receive(cards, delivery, event(delivery, 'edit'))['toast']['type'] == 'info'
    cards.run_once()
    delivery = cards.delivery(delivery['id'])
    assert json.loads(delivery['card_view'])['mode'] == 'edit'
    form = next(n for n in walk(json.loads(delivery['content'])) if n.get('tag') == 'form')
    next_step = next(n for n in walk(form) if n.get('form_action_type') == 'submit')
    assert next_step['text']['content'] == '下一步：提交前核对'
    edited = json.loads(delivery['content'])
    assert [n['element_id'] for n in walk(edited) if n.get('element_id', '').startswith('review_field_')] == original_fields
    assert [n['img_key'] for n in walk(edited) if n.get('tag') == 'img'] == original_images
    assert len(feedback.card_images) == 3  # Switching to edit reuses image keys.
    assert '联系方式' in dump(edited)
    assert not any(n.get('name') == 'repairPersonPhone' for n in walk(form))
    snapshot = cards.reviews.get(delivery['review_id'])['snapshot']
    assert next(n for n in walk(form) if n.get('name') == 'defectDesc')['default_value'] == snapshot['values']['defectDesc']
    assert receive(cards, delivery, event(delivery, 'preview', form={'defectDesc': '人工核对：墙面局部起皮'}))['toast']['type'] == 'info'
    cards.run_once()
    delivery = cards.delivery(delivery['id'])
    view = json.loads(delivery['card_view'])
    assert view['mode'] == 'preview' and view['plan']['values']['defectDesc'] == '人工核对：墙面局部起皮'
    preview = json.loads(delivery['content'])
    assert '2/2 核对并提交' in preview['header']['title']['content']
    elements = preview['body']['elements']
    submit = next(n for n in elements if n.get('text', {}).get('content') == '保存修改并正式提交')
    assert not submit.get('disabled') and submit['confirm']
    assert elements.index(submit) < next(i for i, n in enumerate(elements) if n.get('tag') == 'img')
    assert not business_submits(writes)
    assert services['platform'].draft(delivery['case_id'], 'survey')['values']['defectDesc'] != '人工核对：墙面局部起皮'
    assert receive(cards, delivery, event(delivery))['toast']['type'] == 'info'
    cards.run_once()
    assert actions(cards)[-1]['state'] == 'done', actions(cards)[-1]
    assert business_submits(writes)[0]['defectDesc'] == '人工核对：墙面局部起皮'


def test_missing_division_is_explained_and_filling_it_reenables_submission(bound_cards, services):
    cards, delivery, _, writes = bound_cards
    original_draft = services['platform'].draft(delivery['case_id'], 'survey')

    def act(action, **kwargs):
        current = cards.delivery(delivery['id'])
        assert receive(cards, current, event(current, action, **kwargs))['toast']['type'] == 'info'
        cards.run_once()
        assert actions(cards)[-1]['state'] == 'done', actions(cards)[-1]
        return cards.delivery(delivery['id'])

    act('edit')
    current = act('preview', form={'divisionWorksCode': '__empty__', 'defectDesc': '用户修改后待核对的说明'})
    view = json.loads(current['card_view'])
    assert view['mode'] == 'preview' and not view['direct_allowed']
    elements = json.loads(current['content'])['body']['elements']
    submit = next(n for n in elements if n.get('text', {}).get('content') == '保存修改并正式提交')
    issue = next(n for n in elements if '暂不能正式提交：缺少分部工程' in n.get('text', {}).get('content', ''))
    assert submit['disabled'] and '缺少分部工程' in submit['disabled_tips']['content']
    assert elements.index(issue) < elements.index(submit) < next(i for i, n in enumerate(elements) if n.get('tag') == 'img')
    assert receive(cards, current, event(current))['toast']['type'] == 'error'
    assert services['platform'].draft(delivery['case_id'], 'survey') == original_draft
    assert not business_submits(writes)

    current = act('edit')
    form = next(n for n in walk(json.loads(current['content'])) if n.get('tag') == 'form')
    assert '提交前请补齐：缺少分部工程' in dump(form)
    assert next(n for n in walk(form) if n.get('name') == 'defectDesc')['default_value'] == '用户修改后待核对的说明'
    current = act('preview', form={'divisionWorksCode': 'part_engineering_03'})
    view = json.loads(current['card_view'])
    assert view['direct_allowed'] and not view['plan']['error_count']
    submit = next(n for n in walk(json.loads(current['content'])) if n.get('text', {}).get('content') == '保存修改并正式提交')
    assert not submit.get('disabled')
    assert not business_submits(writes)
    act('submit')
    submitted = business_submits(writes)
    assert len(submitted) == 1
    assert submitted[0]['divisionWorksCode'] == 'part_engineering_03'
    assert submitted[0]['defectDesc'] == '用户修改后待核对的说明'


def test_reopen_preview_retains_changes_and_cancel_restores_original(bound_cards, services):
    cards, delivery, feedback, writes = bound_cards
    original = cards.reviews.get(delivery['review_id'])['snapshot']['values']['defectDesc']
    draft = services['platform'].draft(delivery['case_id'], 'survey')

    def act(action, **kwargs):
        current = cards.delivery(delivery['id'])
        assert receive(cards, current, event(current, action, **kwargs))['toast']['type'] == 'info'
        cards.run_once()
        assert actions(cards)[-1]['state'] == 'done'
        return cards.delivery(delivery['id'])

    act('edit')
    act('preview', form={'defectDesc': '未保存的修改说明'})
    current = act('edit')
    control = next(n for n in walk(json.loads(current['content'])) if n.get('name') == 'defectDesc')
    assert control['default_value'] == '未保存的修改说明'
    assert len([n for n in walk(json.loads(current['content'])) if n.get('tag') == 'img']) == 3
    current = act('refresh')
    view = json.loads(current['card_view'])
    assert view['mode'] == 'ready' and not view.get('values')
    assert original in dump(json.loads(current['content']))
    assert '未保存的修改说明' not in current['content']
    assert services['platform'].draft(delivery['case_id'], 'survey') == draft
    assert not business_submits(writes)
    assert all(mid == delivery['remote_message_id'] for mid, _ in feedback.updated_cards)


@pytest.mark.parametrize('stage,values', [
    ('intake', {'acceptanceResult': '1', 'modifySurveyTimeMark': '1'}),
    ('survey', {'repairMethod': '2'}), ('survey', {'repairMethod': '3'}),
    ('repair', {'repairMethod': '2'}), ('acceptance', {}),
])
def test_display_and_edit_share_web_field_order_and_visibility(stage, values):
    plan = {**deepcopy(STAGES[stage]), 'values': values, 'file_fields': [], 'issues': [],
            'case': {'project_name': '示例项目'}, 'personnel': []}
    expected = [f['label'] for f in plan['fields'] if f['kind'] != 'files' and visible(f, values)]
    delivery = {'id': 'demo', 'open_id': 'demo', 'card_token': 'demo'}
    for mode in ('ready', 'edit', 'preview'):
        card = build_card(delivery, plan, {'mode': mode}, {})
        fields = [n for n in walk(card) if n.get('element_id', '').startswith('review_field_')]
        assert [n['elements'][0]['text']['content'].removesuffix('（必填）') for n in fields] == expected
        assert all(n['flex_mode'] == 'stretch' for n in walk(card) if n.get('tag') == 'column_set')
    assert ('零注拒原因' in expected) == (stage == 'survey' and values.get('repairMethod') == '3')


def test_inline_form_preserves_unknown_codes_named_values_and_large_fields():
    plan = {'label': '现场查勘', 'case': {'project_name': '示例项目'}, 'issues': [], 'file_fields': [],
            'personnel': [{'userId': 'known', 'name': '现有人员'}],
            'values': {'code': 'old', 'codeName': '原业务名称', 'people': 'known,removed', 'long': 'x' * 1001},
            'fields': [{'key': 'code', 'label': '类型', 'kind': 'dictionary', 'dictionary': 'types', 'name_key': 'codeName'},
                       {'key': 'people', 'label': '维修人员', 'kind': 'personnel'},
                       {'key': 'long', 'label': '原因说明', 'kind': 'textarea'}]}
    delivery = {'id': 'demo', 'card_token': 'demo'}
    dictionaries = {'types': [{'code': 'new', 'name': '新的类型'}]}
    card = build_card(delivery, plan, {'mode': 'edit'}, dictionaries)
    controls = {n['name']: n for n in walk(card) if n.get('name') in ('code', 'people', 'long')}
    assert controls['code']['initial_option'] == 'old'
    assert any(o['value'] == 'old' for o in controls['code']['options'])
    assert controls['people']['selected_values'] == ['known', 'removed']
    assert 'long' not in controls and 'x' * 1001 in dump(card)
    display = build_card(delivery, plan, {'mode': 'preview'}, dictionaries)
    assert '原业务名称' in dump(display)
    # A clearing choice and an unknown current code also consume option slots.
    dictionaries['types'] = [{'code': str(i), 'name': str(i)} for i in range(99)]
    card = build_card(delivery, plan, {'mode': 'edit'}, dictionaries)
    assert not any(n.get('name') == 'code' for n in walk(card))
    assert '原业务名称' in dump(card)


def test_pending_fields_disable_submit_until_native_form_is_complete(services, platform, case, photo, monkeypatch, followup, channel, project, admin_user):
    auto, add, _, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    bind(followup, channel, project)
    auto.configure({'notify_review_ready': True})
    platform.test_survey['repairMethod'] = ''
    ready(services, platform, case, add, auto)
    services['feishu_identities'].save({'connector_id': channel[0]['id'], 'open_id': 'ou_lead', 'user_id': admin_user['id']})
    notifier, cards = services['review_notifications'], services['review_cards']
    notifier.run_once()
    delivery = records(notifier)[0]
    submit = next(n for n in walk(json.loads(delivery['content'])) if n.get('tag') == 'button' and n.get('text', {}).get('content') == '确认正式提交')
    assert submit['disabled']
    receive(cards, delivery, event(delivery, 'edit')); cards.run_once()
    delivery = cards.delivery(delivery['id'])
    receive(cards, delivery, event(delivery, 'preview', form={'repairMethod': '2'})); cards.run_once()
    delivery = cards.delivery(delivery['id'])
    view = json.loads(delivery['card_view'])
    # Existing evidence-backed personnel/date values are retained by the shared
    # preview implementation, but absent values still need another form pass.
    if view['plan']['error_count']:
        assert not business_submits(writes)
        assert any(n.get('disabled') for n in walk(json.loads(delivery['content'])) if n.get('tag') == 'button')
    else:
        receive(cards, delivery, event(delivery)); cards.run_once()
        assert business_submits(writes)[0]['repairMethod'] == '2'


@pytest.mark.parametrize('values', [{'state': '1'}, {'repairPersonPhone': 'fake'}, {'defectDesc': ['invalid']},
                                    {'defectDesc': 'x' * 1001}])
def test_form_rejects_hidden_readonly_and_invalid_payloads(bound_cards, values):
    cards, delivery, _, writes = bound_cards
    receive(cards, delivery, event(delivery, 'edit')); cards.run_once()
    delivery = cards.delivery(delivery['id'])
    assert receive(cards, delivery, event(delivery, 'preview', form=values))['toast']['type'] == 'error'
    assert len(actions(cards)) == 1 and not business_submits(writes)


def test_card_update_retry_never_replays_official_submission(bound_cards):
    cards, delivery, feedback, writes = bound_cards
    feedback.update_error = DomainError('更新失败')
    receive(cards, delivery, event(delivery)); cards.run_once()
    current = cards.delivery(delivery['id'])
    assert json.loads(current['card_view'])['verified']
    assert current['card_dirty'] and current['card_error']
    assert len(business_submits(writes)) == 1
    feedback.update_error = None
    with cards.db.tx() as con:
        con.execute('UPDATE review_notifications SET card_retry_at=0 WHERE id=?', (delivery['id'],))
    cards.run_once()
    assert not cards.delivery(delivery['id'])['card_dirty']
    assert len(business_submits(writes)) == 1


def test_uncertain_official_write_only_offers_readback(bound_cards, platform, monkeypatch):
    cards, delivery, _, writes = bound_cards
    attempted = []
    def timeout(stage, payload):
        attempted.append(payload)
        raise PlatformWriteError('网络超时，结果未知', rejected=False)
    monkeypatch.setattr(platform.client, 'submit', timeout)
    receive(cards, delivery, event(delivery)); cards.run_once()
    current = cards.delivery(delivery['id'])
    view = json.loads(current['card_view'])
    assert view['mode'] == 'result' and view['can_readback'] and not view['verified']
    assert len(attempted) == 1
    assert receive(cards, current, event(current))['toast']['type'] == 'error'
    cards.run_once()
    assert len(attempted) == 1


def test_restart_never_replays_a_running_job(bound_cards):
    cards, delivery, _, writes = bound_cards
    receive(cards, delivery, event(delivery))
    with cards.db.tx() as con:
        con.execute("UPDATE feishu_review_actions SET state='running'")
    cards.recover()
    cards.run_once()
    assert actions(cards)[0]['state'] == 'interrupted'
    assert not business_submits(writes)
    assert cards.delivery(delivery['id'])['pending_action_id'] is None


def test_callback_does_not_wait_for_official_write_lock(bound_cards):
    cards, delivery, _, _ = bound_cards
    con = cards.db.connect()
    con.execute('BEGIN IMMEDIATE')
    started = time.monotonic()
    try:
        response = receive(cards, delivery, event(delivery))
    finally:
        con.rollback(); con.close()
    assert time.monotonic() - started < 2.5
    assert response['toast']['type'] == 'warning'
    assert not actions(cards)


def test_large_card_has_full_review_link_and_no_submit_button(bound_cards):
    cards, delivery, _, _ = bound_cards
    plan = deepcopy(cards.reviews.get(delivery['review_id'])['snapshot'])
    plan['values']['defectDesc'] = '长内容' * 3000
    view = {'mode': 'ready'}
    result = build_card(delivery, plan, view, {}, url='https://example.invalid/?review=demo')
    assert not view['direct_allowed']
    assert '完整资料' in dump(result)
    assert not any(n.get('action') == 'submit' for n in walk(result))


def test_card_http_client_uses_interactive_type_update_and_image_upload():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/internal'):
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'synthetic', 'expire': 7200})
        if request.url.path.endswith('/images'):
            return httpx.Response(200, json={'code': 0, 'data': {'image_key': 'img_synthetic'}})
        return httpx.Response(200, json={'code': 0, 'data': {'message_id': 'om_synthetic'}})
    client = FeishuFeedbackClient('cli_synthetic', 'synthetic', transport=httpx.MockTransport(handler))
    assert client.send_card('oc_synthetic', '{"schema":"2.0"}', 'notice') == 'om_synthetic'
    client.update_card('om_synthetic', '{"schema":"2.0"}')
    assert client.upload_card_image(b'image-bytes') == 'img_synthetic'
    assert json.loads(requests[1].content)['msg_type'] == 'interactive'
    assert requests[2].method == 'PATCH' and 'content' in json.loads(requests[2].content)
    assert b'name="image_type"' in requests[3].content
    client.close()


def test_sdk_callback_returns_a_typed_response_and_only_enqueues(bound_cards):
    from types import SimpleNamespace
    from idi.connectors.feishu_worker import create_ws
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    cards, delivery, _, writes = bound_cards
    capture = SimpleNamespace(connector_id=delivery['connector_id'])
    ws = create_ws(capture, delivery['app_id'], 'synthetic-no-live-secret', cards)
    response = ws._event_handler._do_without_validation(dump(event(delivery)).encode())
    assert isinstance(response, P2CardActionTriggerResponse)
    assert response.toast.type == 'info'
    assert len(actions(cards)) == 1 and not business_submits(writes)


def test_shared_application_callback_can_arrive_on_another_group_connection(bound_cards, services):
    cards, delivery, _, writes = bound_cards
    other = services['connectors'].save({'kind': 'feishu', 'name': '同应用另一群', 'enabled': True,
                                        'app_secret': 'synthetic', 'config': {'app_id': delivery['app_id'], 'chat_id': 'oc_other'}})
    response = cards.receive_callback(other['id'], delivery['app_id'], event(delivery))
    assert response['toast']['type'] == 'info' and len(actions(cards)) == 1
    assert not business_submits(writes)


def test_identity_management_is_admin_only_and_requires_current_group_member(bound_cards, services, client, app, project):
    from tests.test_project_access import make_person, login_person
    cards, delivery, feedback, _ = bound_cards
    endpoint = '/api/access/feishu-bindings'
    response = client.get(endpoint)
    assert response.status_code == 200 and len(response.json()['bindings']) == 1
    assert 'password' not in response.text
    binding = response.json()['bindings'][0]
    assert client.get(endpoint + '/groups/' + delivery['connector_id'] + '/members').json()[0]['open_id'] == 'ou_lead'
    assert client.put(endpoint, json={'connector_id': delivery['connector_id'], 'open_id': 'ou_not_member', 'user_id': binding['user_id']}).status_code == 400
    feedback.people.append({'open_id': 'ou_second', 'name': '另一个飞书账号'})
    assert client.put(endpoint, json={'connector_id': delivery['connector_id'], 'open_id': 'ou_second', 'user_id': binding['user_id']}).status_code == 409
    make_person(services, project['id'], role='manager', name='card-manager')
    manager = login_person(app, 'card-manager')
    for method, path, kwargs in [
        ('get', endpoint, {}), ('get', endpoint + '/groups/' + delivery['connector_id'] + '/members', {}),
        ('put', endpoint, {'json': {'connector_id': delivery['connector_id'], 'open_id': 'ou_lead', 'user_id': binding['user_id']}}),
        ('delete', endpoint + '/' + binding['id'], {}),
    ]:
        assert getattr(manager, method)(path, **kwargs).status_code == 403
    assert client.delete(endpoint + '/' + binding['id']).status_code == 200
    assert not services['db'].all('SELECT * FROM feishu_identity_bindings')
    manager.close()


def test_another_manager_cannot_submit_someone_elses_edited_preview(bound_cards, services, project):
    from tests.test_project_access import make_person
    cards, delivery, feedback, writes = bound_cards
    second = make_person(services, project['id'], role='manager', name='second-card-manager')
    feedback.people.append({'open_id': 'ou_second', 'name': '第二位管理员'})
    services['feishu_identities'].save({'connector_id': delivery['connector_id'], 'open_id': 'ou_second', 'user_id': second['id']})
    receive(cards, delivery, event(delivery, 'edit')); cards.run_once()
    delivery = cards.delivery(delivery['id'])
    receive(cards, delivery, event(delivery, 'preview', form={'defectDesc': '第一位管理员核对的内容'})); cards.run_once()
    delivery = cards.delivery(delivery['id'])
    assert receive(cards, delivery, event(delivery, open_id='ou_second'))['toast']['type'] == 'error'
    assert not business_submits(writes)


def test_changed_materials_disable_old_card_and_send_one_new_card(bound_cards, services, platform):
    cards, old, feedback, writes = bound_cards
    data = draft_data(platform, old['case_id'], 'survey')
    data['values']['defectDesc'] = '新一轮核对的内容'
    platform.save_draft(old['case_id'], 'survey', data)
    tick(services['automatic_uploads'], old['case_id'])
    cards.notifications.run_once()
    old = cards.delivery(old['id'])
    assert json.loads(old['card_view'])['mode'] == 'stale'
    assert receive(cards, old, event(old))['toast']['type'] == 'error'
    assert len(feedback.cards) == 2 and not business_submits(writes)


def test_restore_does_not_replay_queued_card_submissions(bound_cards, services, tmp_path):
    from idi.cli import restore
    from idi.db import Database
    cards, delivery, _, writes = bound_cards
    receive(cards, delivery, event(delivery))
    backup = services['reporter'].backup({}, lambda *args: None)
    target = tmp_path / 'restored-card-actions'
    restore(services['settings'].data_dir / 'backups' / backup['filename'], target)
    db = Database(target / 'archive.sqlite3')
    assert db.one('SELECT state FROM feishu_review_actions')['state'] == 'interrupted'
    restored = db.one('SELECT card_token,pending_action_id FROM review_notifications WHERE id=?', (delivery['id'],))
    assert restored == {'card_token': '', 'pending_action_id': None}
    assert not business_submits(writes)
