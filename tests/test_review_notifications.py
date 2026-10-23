import json

import pytest

from idi.connectors.feishu_feedback import UncertainDelivery
from idi.domain import DomainError
from tests.test_automatic_uploads import setup_flow, ready, tick
from tests.test_platform_preparation import platform, enter_stage, draft_data
from tests.test_project_followup import followup, channel, bind


@pytest.fixture
def notification_flow(services, platform, case, photo, monkeypatch, followup, channel, project):
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    bind(followup, channel, project)
    auto.configure({'notify_review_ready': True})
    row = ready(services, platform, case, add, auto)
    assert row['state'] == 'awaiting_confirmation'
    return services['review_notifications'], auto, channel[1], writes


def records(service):
    return service.db.all('SELECT * FROM review_notifications ORDER BY created_at,id')


def test_ready_proposal_mentions_bound_owner_once_across_refresh_and_restart(notification_flow, case):
    notifier, auto, client, writes = notification_flow
    previous_writes = list(writes)
    original_review = auto.get(case['id'])['review_id']
    assert notifier.run_once() == 1
    assert len(client.sent) == 1
    chat, content, delivery_id = client.sent[0]
    assert chat == 'oc_test'
    assert '<at user_id="ou_lead">项目负责人</at>' in content['text']
    assert '从化铂悦府' in content['text'] and 'ZLBH20260001' in content['text']
    assert '现场查勘资料待核对' in content['text'] and '核对拟提交信息' in content['text']
    assert '楼栋打卡图片 1 项' in content['text']
    assert records(notifier)[0]['state'] == 'sent'
    assert auto.get(case['id'])['notification']['state'] == 'sent'
    assert notifier.run_once() == 0
    notifier.recover()
    tick(auto, case['id'])
    assert auto.get(case['id'])['review_id'] != original_review
    assert notifier.run_once() == 0
    auto.configure({'notify_review_ready': False})
    notifier.run_once()
    auto.configure({'notify_review_ready': True})
    assert notifier.run_once() == 0
    assert len(client.sent) == 1 and client.sent[0][2] == delivery_id
    assert writes == previous_writes and all(payload['state'] == 0 for _, payload in writes)


def test_incomplete_materials_never_send_notification(services, platform, case, photo, monkeypatch, followup, channel, project):
    auto, add, _, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    bind(followup, channel, project)
    auto.configure({'notify_review_ready': True})
    add('red', 'survey', 'building_checkin')
    assert tick(auto, case['id'])['state'] == 'needs_fields'
    notifier = services['review_notifications']
    assert notifier.run_once() == 0
    assert not records(notifier) and not channel[1].sent
    assert all(payload['state'] == 0 for _, payload in writes)


def test_complete_photos_notify_pending_fields_once_then_require_fresh_review(services, platform, case, photo, monkeypatch, followup, channel, project):
    auto, add, _, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    bind(followup, channel, project)
    auto.configure({'notify_review_ready': True})
    platform.test_survey['repairMethod'] = ''
    row = ready(services, platform, case, add, auto)
    assert row['state'] == 'awaiting_confirmation'
    review = services['reviews'].get(row['review_id'])
    assert review['snapshot']['review_only'] and review['snapshot']['actions'] == []
    assert [f['field'] for f in row['detail']['pending_fields']] == ['repairMethod']
    notifier, feedback = services['review_notifications'], channel[1]
    assert notifier.run_once() == 1
    assert '待管理员确认：维修方式' in feedback.sent[0][1]['text']
    assert '已收照片已合并' in feedback.sent[0][1]['text']
    tick(auto, case['id'])
    notifier.recover()
    assert notifier.run_once() == 0 and len(feedback.sent) == 1
    assert all(payload['state'] == 0 for _, payload in writes)
    plan = platform.draft(case['id'], 'survey')
    platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', {**plan['values'], 'repairMethod': '2'}))
    row = tick(auto, case['id'])
    complete = services['reviews'].get(row['review_id'])
    assert not complete['snapshot'].get('review_only')
    assert notifier.run_once() == 1 and len(feedback.sent) == 2
    assert '待管理员确认：' not in feedback.sent[-1][1]['text']
    assert services['reviews'].confirm(complete['id'], complete['fingerprint'])['state'] == 'verified'
    assert [payload['state'] for _, payload in writes].count(1) == 1


def test_missing_owner_stays_visible_and_sends_after_binding(notification_flow, case, project, followup, channel):
    notifier, auto, client, _ = notification_flow
    with notifier.db.tx() as con:
        con.execute('DELETE FROM followup_contacts WHERE project_id=?', (project['id'],))
    assert notifier.run_once() == 0 and not client.sent
    status = auto.get(case['id'])['notification']
    assert status['state'] == 'blocked' and '尚未绑定' in status['message']
    bind(followup, channel, project)
    assert notifier.run_once() == 1 and len(client.sent) == 1


@pytest.mark.parametrize('change', ['policy', 'case', 'stage', 'materials'])
def test_queued_notification_rechecks_current_readiness(notification_flow, case, platform, change):
    notifier, auto, client, _ = notification_flow
    delivery = notifier.queue(case['id'])
    if change == 'policy':
        auto.configure({'notify_review_ready': False})
    elif change == 'case':
        auto.enroll(case['id'], False)
    elif change == 'stage':
        enter_stage(platform, case['id'], 'repair')
    else:
        data = draft_data(platform, case['id'], 'survey')
        data['values']['defectDesc'] = ''
        platform.save_draft(case['id'], 'survey', data)
    notifier.deliver(delivery['id'])
    assert records(notifier)[0]['state'] == 'cancelled'
    assert not client.sent


def test_reassigned_owner_receives_new_notice_and_old_recipient_is_cancelled(notification_flow, case, project, followup, channel):
    notifier, _, client, _ = notification_flow
    old = notifier.queue(case['id'])
    client.people.append({'open_id': 'ou_newlead', 'name': '新负责人'})
    followup.save_contact(project['id'], {'connector_id': channel[0]['id'], 'open_id': 'ou_newlead'})
    notifier.deliver(old['id'])
    assert records(notifier)[0]['state'] == 'cancelled' and not client.sent
    notifier.run_once()
    assert len(client.sent) == 1 and 'user_id="ou_newlead"' in client.sent[0][1]['text']
    assert 'user_id="ou_lead"' not in client.sent[0][1]['text']


def test_changed_business_fields_get_one_new_notification(notification_flow, case, platform):
    notifier, auto, client, _ = notification_flow
    notifier.run_once()
    data = draft_data(platform, case['id'], 'survey')
    data['values']['defectDesc'] = '重新现场核实的具体缺陷原因'
    platform.save_draft(case['id'], 'survey', data)
    tick(auto, case['id'])
    notifier.run_once()
    assert len(client.sent) == 2
    assert len({row['dedupe_key'] for row in records(notifier)}) == 2
    assert notifier.run_once() == 0


def test_member_removal_fails_without_send_and_explicit_retry_uses_same_uuid(notification_flow, case, client):
    notifier, auto, feedback, _ = notification_flow
    feedback.people = []
    notifier.run_once()
    row = records(notifier)[0]
    assert row['state'] == 'failed' and not feedback.sent
    assert auto.get(case['id'])['notification']['error'] == '项目负责人已不在群中，请重新绑定。'
    feedback.people = [{'open_id': 'ou_lead', 'name': '项目负责人'}]
    assert client.post(f"/api/workflow/uploads/notifications/{row['id']}/retry").status_code == 200
    notifier.run_once()
    assert len(feedback.sent) == 1 and feedback.sent[0][2] == row['id']
    assert records(notifier)[0]['state'] == 'sent'


def test_materials_changing_during_member_lookup_cancel_send(notification_flow, case, platform, monkeypatch):
    notifier, _, feedback, _ = notification_flow
    delivery = notifier.queue(case['id'])

    def changed(_chat_id):
        data = draft_data(platform, case['id'], 'survey')
        data['values']['defectDesc'] = '核对时发生变化'
        platform.save_draft(case['id'], 'survey', data)
        return feedback.people

    monkeypatch.setattr(feedback, 'members', changed)
    notifier.deliver(delivery['id'])
    assert not feedback.sent and records(notifier)[0]['state'] == 'cancelled'


@pytest.mark.parametrize('received', [True, False])
def test_uncertain_delivery_requires_confirmation_before_retry(notification_flow, case, client, received):
    notifier, _, feedback, _ = notification_flow
    feedback.error = UncertainDelivery('发送结果不确定')
    notifier.run_once()
    row = records(notifier)[0]
    assert row['state'] == 'unknown' and len(feedback.sent) == 1
    assert notifier.run_once() == 0
    assert client.post(f"/api/workflow/uploads/notifications/{row['id']}/retry").status_code == 409
    assert client.post(f"/api/workflow/uploads/notifications/{row['id']}/confirm", json={'received': 'yes'}).status_code == 400
    feedback.error = None
    response = client.post(f"/api/workflow/uploads/notifications/{row['id']}/confirm", json={'received': received})
    assert response.status_code == 200
    notifier.run_once()
    assert len(feedback.sent) == (1 if received else 2)
    assert records(notifier)[0]['state'] == 'sent'
    assert all(item[2] == row['id'] for item in feedback.sent)


def test_interrupted_send_is_unknown_and_never_replayed(notification_flow, case):
    notifier, _, feedback, _ = notification_flow
    delivery = notifier.queue(case['id'])
    with notifier.db.tx() as con:
        con.execute("UPDATE review_notifications SET state='sending' WHERE id=?", (delivery['id'],))
    notifier.recover()
    assert records(notifier)[0]['state'] == 'unknown'
    assert notifier.run_once() == 0 and not feedback.sent


def test_pending_notification_is_cancelled_when_paused_and_reused_on_resume(notification_flow, case):
    notifier, auto, feedback, _ = notification_flow
    delivery = notifier.queue(case['id'])
    auto.configure({'notify_review_ready': False})
    notifier.run_once()
    assert records(notifier)[0]['state'] == 'cancelled' and not feedback.sent
    auto.configure({'notify_review_ready': True})
    notifier.run_once()
    assert feedback.sent[0][2] == delivery['id'] and len(records(notifier)) == 1


def test_notification_settings_do_not_reenroll_or_resave_cases(notification_flow, case):
    _, auto, _, _ = notification_flow
    before = auto.signature(case['id'])
    activated = auto.policy()['activated_at']
    auto.configure({'notify_review_ready': False})
    assert auto.signature(case['id']) == before
    assert auto.policy()['enabled'] and auto.policy()['activated_at'] == activated
    with pytest.raises(DomainError):
        auto.configure({'notify_review_ready': 'true'})


def test_user_text_cannot_inject_extra_mentions(notification_flow, case):
    notifier, _, _, _ = notification_flow
    _, plan = notifier.current(case['id'])
    plan['case']['title'] = '<at user_id="all">所有人</at>'
    person = notifier.recipient(plan['case']['project_id'])
    person['name'] = '<at user_id="all">伪造</at>'
    text = json.loads(notifier.message(plan, person))['text']
    assert text.count('<at ') == 1 and '<at user_id="ou_lead">' in text
    assert '＜at user_id="all"＞' in text


@pytest.mark.parametrize('state,expected', [('queued', 'cancelled'), ('sending', 'unknown'), ('sent', 'sent')])
def test_backup_restore_pauses_notices_and_preserves_delivery_evidence(notification_flow, case, services, tmp_path, state, expected):
    from idi.cli import restore
    from idi.db import Database

    notifier, _, _, _ = notification_flow
    delivery = notifier.queue(case['id'])
    with notifier.db.tx() as con:
        con.execute('UPDATE review_notifications SET state=? WHERE id=?', (state, delivery['id']))
    backup = services['reporter'].backup({}, lambda *args: None)
    target = tmp_path / 'restored-notifications'
    restore(services['settings'].data_dir / 'backups' / backup['filename'], target)
    restored = Database(target / 'archive.sqlite3')
    policy = restored.setting('automatic_upload_policy')
    assert not policy['enabled'] and not policy.get('notify_review_ready', False)
    row = restored.one('SELECT * FROM review_notifications WHERE id=?', (delivery['id'],))
    assert row['state'] == expected and row['content'] == delivery['content']
