"""Additional-stage card acceptance using isolated databases and fake remotes."""
import json
from copy import deepcopy

import pytest

from idi.db import dump
from idi.review_card_content import build_card, editable_fields
from tests.test_automatic_uploads import setup_flow, tick
from tests.test_feishu_review_cards import actions, business_submits, event, receive, walk
from tests.test_platform_preparation import draft_data, enter_stage, platform
from tests.test_project_followup import bind, channel, followup
from tests.test_review_notifications import records


@pytest.fixture
def repair_cards(request, services, platform, case, photo, monkeypatch, followup, channel, project, admin_user):
    options = getattr(request, 'param', {})
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    bind(followup, channel, project)
    auto.configure({'notify_review_ready': True})
    method = options.get('method', '2')
    platform.test_row['repairMethod'] = method
    platform.test_survey['repairMethod'] = method
    platform.test_repair.update(preRepairFileInfo=[], approachStartWorkDate='2026-10-20 09:00:00',
                               finishWorkDate='2026-10-21 16:00:00', repairCrewNumber=1)
    enter_stage(platform, case['id'], 'repair')
    add('red', 'work', 'building_checkin')
    add('blue', 'work', 'door_checkin')
    for color in ('green', 'yellow', 'orange', 'purple', 'silver', 'black', 'pink', 'cyan')[:options.get('finished', 1)]:
        add(color, 'acceptance', 'finished')
    row = tick(auto, case['id'])
    if row['state'] == 'queued':
        row = tick(auto, case['id'])
    assert row['state'] == 'awaiting_confirmation', row
    services['feishu_identities'].save({'connector_id': channel[0]['id'], 'open_id': 'ou_lead', 'user_id': admin_user['id']})
    notifier = services['review_notifications']
    assert notifier.run_once() == 1
    delivery = records(notifier)[0]
    assert delivery['stage'] == 'repair' and delivery['state'] == 'sent'
    return services['review_cards'], delivery, channel[1], writes, uploads


def act(cards, delivery, operation, **kwargs):
    current = cards.delivery(delivery['id'])
    assert receive(cards, current, event(current, operation, **kwargs))['toast']['type'] == 'info'
    cards.run_once()
    assert actions(cards)[-1]['state'] == 'done', actions(cards)[-1]
    return cards.delivery(delivery['id'])


def controls(delivery):
    return {n['name']: n for n in walk(json.loads(delivery['content'])) if n.get('name')}


def submit_button(delivery):
    return next(n for n in walk(json.loads(delivery['content']))
                if n.get('tag') == 'button' and n['text']['content'] == '确认正式提交')


@pytest.mark.parametrize('repair_cards', [{'method': '1'}, {'method': '2'}], indirect=True)
def test_repair_card_edit_preview_cancel_then_confirm_once(repair_cards, services, platform):
    cards, delivery, feedback, writes, uploads = repair_cards
    original = deepcopy(platform.draft(delivery['case_id'], 'repair'))
    before_uploads = len(uploads)
    content = delivery['content']
    assert '修复成果资料待核对' in content and '修复后照片/视频' in content
    assert ('补充材料' in content) == (platform.preview(delivery['case_id'], 'repair')['values']['repairMethod'] == '2')
    edited = act(cards, delivery, 'edit')
    assert controls(edited)['repairCrewNumber']['default_value'] == '1'
    assert not {'repairUnit', 'repairPersonName', 'timeLimit', 'estimateRestorationCost'} & controls(edited).keys()
    values = {'repairCrewNumber': '3', 'finishWorkDate': '2026-10-22 16:00:00'}
    preview = act(cards, delivery, 'preview', form=values)
    view = json.loads(preview['card_view'])
    assert view['plan']['values']['repairCrewNumber'] == 3
    assert view['plan']['values']['timeLimit'] == 3
    assert not submit_button(preview).get('disabled')
    assert platform.draft(delivery['case_id'], 'repair') == original and not business_submits(writes)
    reopened = act(cards, delivery, 'edit')
    assert controls(reopened)['repairCrewNumber']['default_value'] == '3'
    cancelled = act(cards, delivery, 'refresh')
    assert not json.loads(cancelled['card_view']).get('values')
    assert platform.draft(delivery['case_id'], 'repair') == original
    act(cards, delivery, 'edit')
    preview = act(cards, delivery, 'preview', form=values)
    payload = event(preview)
    assert receive(cards, preview, payload)['toast']['type'] == 'info'
    assert receive(cards, preview, payload)['toast']['type'] == 'info'
    cards.run_once()
    result = cards.delivery(delivery['id'])
    assert json.loads(result['card_view'])['verified'], result
    assert len(business_submits(writes)) == 1
    assert business_submits(writes)[0]['repairCrewNumber'] == 3
    assert business_submits(writes)[0]['timeLimit'] == 3
    assert 'acceptanceDate' not in business_submits(writes)[0]
    assert services['domain'].case(delivery['case_id'])['workflow_stage'] == 'acceptance'
    assert len(uploads) == before_uploads
    assert all(mid == delivery['remote_message_id'] for mid, _ in feedback.updated_cards)


@pytest.mark.parametrize('form', [
    {'repairCrewNumber': '0'}, {'repairCrewNumber': '1.5'}, {'repairCrewNumber': '10001'},
    {'approachStartWorkDate': 'tomorrow'}, {'finishWorkDate': '2026-10-19 10:00:00'},
    {'finishWorkDate': ''},
])
def test_repair_invalid_fields_disable_confirmation_and_leave_draft_untouched(repair_cards, platform, form):
    cards, delivery, _, writes, _ = repair_cards
    original = deepcopy(platform.draft(delivery['case_id'], 'repair'))
    act(cards, delivery, 'edit')
    preview = act(cards, delivery, 'preview', form=form)
    assert json.loads(preview['card_view'])['plan']['error_count'] > 0
    assert submit_button(preview)['disabled']
    assert receive(cards, preview, event(preview))['toast']['type'] == 'error'
    assert platform.draft(delivery['case_id'], 'repair') == original and not business_submits(writes)
    act(cards, delivery, 'refresh')
    assert not submit_button(cards.delivery(delivery['id'])).get('disabled')


@pytest.mark.parametrize('key', ['repairUnit', 'repairPersonName', 'timeLimit', 'estimateRestorationCost', 'acceptanceDate'])
def test_repair_callback_rejects_readonly_and_other_stage_fields(repair_cards, key):
    cards, delivery, _, writes, _ = repair_cards
    edited = act(cards, delivery, 'edit')
    before = len(actions(cards))
    assert receive(cards, edited, event(edited, 'preview', form={key: 'forged'}))['toast']['type'] == 'error'
    assert len(actions(cards)) == before and not business_submits(writes)


@pytest.mark.parametrize('repair_cards', [{'finished': 8}], indirect=True)
def test_repair_photo_pagination_preserves_preview_and_does_not_upload_twice(repair_cards):
    cards, delivery, feedback, writes, _ = repair_cards
    assert '共 10 项' in delivery['content']
    assert len([n for n in walk(json.loads(delivery['content'])) if n.get('tag') == 'img']) == 6
    second = act(cards, delivery, 'photos', page=1)
    assert '附件预览 2/2 页' in second['content']
    assert len([n for n in walk(json.loads(second['content'])) if n.get('tag') == 'img']) == 4
    assert len(feedback.card_images) == 10
    act(cards, delivery, 'edit')
    preview = act(cards, delivery, 'preview', form={'repairCrewNumber': '4'})
    assert json.loads(preview['card_view'])['page'] == 0
    second = act(cards, delivery, 'photos', page=1)
    assert json.loads(second['card_view'])['plan']['values']['repairCrewNumber'] == 4
    assert len(feedback.card_images) == 10 and not business_submits(writes)


@pytest.mark.parametrize('result,modify,expected', [
    ('1', '0', {'acceptanceResult', 'modifySurveyTimeMark', 'reasonDescription'}),
    ('1', '1', {'acceptanceResult', 'modifySurveyTimeMark', 'modifiedSurveyTime', 'modifiedSurveyTimeEnd', 'reasonDescription'}),
    ('2', '1', {'acceptanceResult', 'reasonDescription'}),
    ('3', '1', {'acceptanceResult', 'reasonDescription'}),
])
def test_intake_card_conditions_use_real_business_validation(platform, case, result, modify, expected):
    enter_stage(platform, case['id'], 'intake')
    values = {'acceptanceResult': result, 'modifySurveyTimeMark': modify, 'reasonDescription': '模拟受理说明',
              'modifiedSurveyTime': '2026-10-29 10:00:00', 'modifiedSurveyTimeEnd': '2026-10-29 11:00:00'}
    data = draft_data(platform, case['id'], 'intake', values)
    plan = platform.preview(case['id'], 'intake', data)
    assert plan['error_count'] == 0
    assert {f['key'] for f in editable_fields(plan, {})} == expected
    card = build_card({'id': 'test', 'card_token': 'test'}, plan, {'mode': 'edit'}, {})
    actual = {n['name'] for n in walk(card) if n.get('tag') in ('input', 'select_static')}
    assert actual == expected
    values['modifiedSurveyTimeEnd'] = '2026-10-29 09:00:00'
    invalid = platform.preview(case['id'], 'intake', data)
    assert bool(invalid['error_count']) == (result == '1' and modify == '1')


def test_acceptance_card_requires_pdf_and_preserves_non_image_attachment_prompt(platform, services, case, photo):
    enter_stage(platform, case['id'], 'acceptance')
    picture = photo(stage='acceptance', role='finished')
    data = draft_data(platform, case['id'], 'acceptance', {'acceptanceDate': '2026-10-29 15:00:00'},
                      {'acceptanceCheckFileInfo': [picture['id']]})
    invalid = platform.preview(case['id'], 'acceptance', data)
    assert invalid['error_count'] > 0
    invalid_card = build_card({'id': 'test', 'card_token': 'test', 'open_id': 'test'}, invalid, {}, {})
    assert next(n for n in walk(invalid_card) if n.get('tag') == 'button' and n['text']['content'] == '确认正式提交')['disabled']
    pdf = services['importer'].add_file(b'%PDF-1.4\n% isolated synthetic acceptance material\n%%EOF', '模拟业主验收.pdf', case_id=case['id'])
    data['files']['acceptanceCheckFileInfo'] = [pdf['photo_id']]
    plan = platform.preview(case['id'], 'acceptance', data)
    assert plan['error_count'] == 0
    image_entry = {'field_key': 'acceptanceCheckFileInfo', 'label': '业主验收材料 · 模拟业主验收.pdf'}
    card = build_card({'id': 'test', 'card_token': 'test', 'open_id': 'test'}, plan, {}, {}, [image_entry])
    assert not any(n.get('tag') == 'img' for n in walk(card))
    assert '模拟业主验收.pdf' in dump(card) and '此附件请打开完整资料查看原件' in dump(card)
    assert {f['key'] for f in editable_fields(plan, {})} == {'acceptanceDate'}
    data['values']['acceptanceDate'] = 'not-a-date'
    assert platform.preview(case['id'], 'acceptance', data)['error_count'] > 0


@pytest.mark.parametrize('stage', ['intake', 'acceptance'])
def test_intake_and_acceptance_do_not_claim_automatic_card_delivery(services, platform, case, photo, monkeypatch, followup, channel, project, stage):
    auto, _, _, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    bind(followup, channel, project)
    auto.configure({'notify_review_ready': True})
    enter_stage(platform, case['id'], stage)
    row = tick(auto, case['id'])
    assert row['state'] == 'waiting_stage'
    assert services['review_notifications'].run_once() == 0
    assert not channel[1].sent and not writes
