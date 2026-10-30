import json
import pytest

from idi.db import uid
from idi.feishu_card_demos import FeishuCardDemos, render_demo
from tests.test_project_followup import channel, followup


@pytest.fixture
def demo(services, admin_user, project, channel):
    source = channel[0]
    identities = services['feishu_identities']
    identities.save({'connector_id': source['id'], 'open_id': 'ou_lead', 'user_id': admin_user['id']})
    cards = services['review_cards']
    session = cards.demos.create(source['id'], 'ou_lead', project['id'], ['img_demo_1', 'img_demo_2', 'img_demo_3'])
    cards.demos.bind_message(session['id'], 'om_demo')
    return cards, cards.demos.get(session['id'])


def payload(session, action, form=None):
    return {'header': {'event_id': uid(), 'event_type': 'card.action.trigger', 'app_id': session['app_id'], 'tenant_key': 't'},
            'event': {'host': 'im_message', 'operator': {'open_id': session['open_id'], 'tenant_key': 't'},
                      'context': {'open_chat_id': session['chat_id'], 'open_message_id': session['message_id']},
                      'action': {'value': {'demo_session': session['id'], 'notification_id': 'demo:' + session['id'],
                                           'card_token': session['token'], 'action': action}, 'form_value': form or {}}}}


def click(cards, session, operation, form=None):
    response = cards.receive_callback(session['connector_id'], session['app_id'], payload(session, operation, form))
    assert response['toast']['type'] == 'info', response
    assert response['card']['type'] == 'raw'
    return cards.demos.get(session['id']), response


def test_native_demo_edit_preview_reopen_cancel_and_simulate_never_write_business(demo, services):
    cards, session = demo
    before = {t: services['db'].all('SELECT * FROM ' + t) for t in
              ('cases', 'platform_drafts', 'platform_review_batches', 'review_notifications', 'feishu_review_actions')}
    session, _ = click(cards, session, 'edit')
    session, response = click(cards, session, 'preview', {'defectDesc': '演示修改内容', 'defectTypeCode': 'demo-leak'})
    assert session['mode'] == 'preview' and '演示修改内容' in json.dumps(response, ensure_ascii=False)
    assert '渗漏' in json.dumps(response, ensure_ascii=False)
    session, response = click(cards, session, 'edit')
    assert '演示修改内容' in json.dumps(response, ensure_ascii=False)
    session, _ = click(cards, session, 'refresh')
    assert session['mode'] == 'ready' and session['values'] == {}
    session, response = click(cards, session, 'simulate')
    assert session['mode'] == 'result' and '未提交官网' in response['toast']['content']
    session, _ = click(cards, session, 'refresh')
    assert session['mode'] == 'ready'
    assert {t: services['db'].all('SELECT * FROM ' + t) for t in before} == before
    rebuilt = FeishuCardDemos(cards.followup, cards.identities)
    assert rebuilt.get(session['id']) == session


@pytest.mark.parametrize('field', ['app', 'chat', 'message', 'person', 'tenant', 'notification', 'host', 'binding', 'expired'])
def test_demo_rejects_wrong_context_and_changed_identity(demo, services, field):
    cards, session = demo
    data = payload(session, 'edit')
    if field == 'app': data['header']['app_id'] = 'cli_other'
    elif field == 'chat': data['event']['context']['open_chat_id'] = 'oc_other'
    elif field == 'message': data['event']['context']['open_message_id'] = 'om_other'
    elif field == 'person': data['event']['operator']['open_id'] = 'ou_other'
    elif field == 'tenant': data['event']['operator']['tenant_key'] = 'other'
    elif field == 'notification': data['event']['action']['value']['notification_id'] = 'real-business-id'
    elif field == 'host': data['event']['host'] = 'preview'
    elif field == 'binding':
        with services['db'].tx() as con:
            con.execute('UPDATE feishu_identity_bindings SET revision=?', ('changed',))
    else:
        session['expires_at'] = 0
        with cards.demos.connect() as con:
            con.execute('UPDATE sessions SET body=? WHERE id=?', (json.dumps(session), session['id']))
    response = cards.receive_callback(session['connector_id'], session['app_id'], data)
    assert response['toast']['type'] == 'error'
    assert cards.demos.get(session['id'])['mode'] == 'ready'
    assert not services['db'].all('SELECT * FROM feishu_review_actions')


@pytest.mark.parametrize('form', [{'repairPersonPhone': 'readonly'}, {'zeroRejectReason': 'hidden'},
                                 {'defectDesc': 'a' * 1001}, {'defectTypeCode': 'unknown'}, {'defectDesc': {}}])
def test_demo_rejects_invalid_form_without_changing_session(demo, form):
    cards, session = demo
    session, _ = click(cards, session, 'edit')
    response = cards.receive_callback(session['connector_id'], session['app_id'], payload(session, 'preview', form))
    assert response['toast']['type'] == 'error'
    assert cards.demos.get(session['id']) == session


def test_demo_deduplicates_and_refreshes_stale_cards_without_replaying(demo):
    cards, session = demo
    data = payload(session, 'edit')
    first = cards.receive_callback(session['connector_id'], session['app_id'], data)
    assert first['toast']['type'] == 'info'
    saved = cards.demos.get(session['id'])
    second = cards.receive_callback(session['connector_id'], session['app_id'], data)
    assert second['card'] == first['card'] and cards.demos.get(session['id']) == saved
    stale = cards.receive_callback(session['connector_id'], session['app_id'], payload(session, 'simulate'))
    assert stale['toast']['type'] == 'warning' and cards.demos.get(session['id']) == saved


def test_demo_card_has_only_simulation_actions_and_sdk_preserves_raw_response(demo):
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    from idi.feishu_card_demos import walk
    cards, session = demo
    card = render_demo(session)
    actions = [b['value']['action'] for n in walk(card) for b in n.get('behaviors', []) if b['type'] == 'callback']
    assert set(actions) == {'edit', 'simulate', 'refresh'}
    assert '确认正式提交' not in json.dumps(card, ensure_ascii=False)
    result = cards.demos.response(session, '演示已更新')
    assert json.loads(lark.JSON.marshal(P2CardActionTriggerResponse(result))) == result
