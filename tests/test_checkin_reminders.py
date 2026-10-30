"""Missing-check-in reminders use current official evidence and never submit."""
import json
import time

import pytest

from idi.domain import DomainError
from tests.conftest import image_bytes
from tests.test_automatic_uploads import setup_flow, tick
from tests.test_feishu_review_cards import business_submits, event, receive, walk
from tests.test_platform_preparation import platform, enter_stage, remote_file, draft_data
from tests.test_project_followup import followup, channel, bind
from tests.test_review_notifications import records


@pytest.fixture
def gap_flow(services, platform, case, photo, monkeypatch, followup, channel, project):
    def prepare(stage='repair', building=False, quiet=0):
        auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch, quiet_seconds=quiet)
        # Official attachment metadata is exercised through MockTransport; file
        # preview downloads must also stay inside the isolated fixture.
        monkeypatch.setattr(platform.client, 'download_attachment', lambda *args, **kwargs: image_bytes('gray'))
        bind(followup, channel, project)
        auto.configure({'notify_review_ready': True})
        platform.test_survey['defectDesc'] = '已核对现场原因'
        if stage == 'repair':
            platform.test_repair.update(approachStartWorkDate='2026-10-17 09:00:00',
                                        finishWorkDate='2026-10-17 10:00:00', repairCrewNumber=1)
        enter_stage(platform, case['id'], stage)
        add('green', 'survey' if stage == 'survey' else 'acceptance', 'problem' if stage == 'survey' else 'finished')
        if building:
            add('red', 'survey' if stage == 'survey' else 'work', 'building_checkin')
        row = tick(auto, case['id'])
        return auto, add, services['review_notifications'], channel[1], writes, row
    return prepare


def submit_button(card):
    return next(node for node in walk(card) if node.get('tag') == 'button'
                and node.get('text', {}).get('content') == '确认正式提交')


@pytest.mark.parametrize('stage', ['survey', 'repair'])
@pytest.mark.parametrize('building', [False, True])
def test_only_missing_checkins_notify_once_with_disabled_submit(gap_flow, services, case, stage, building):
    auto, _, notifier, feedback, writes, row = gap_flow(stage, building)
    assert row['state'] == 'awaiting_confirmation', row
    expected = ['houseFileInfoList'] if building else ['buildingFileInfoList', 'houseFileInfoList']
    assert [item['field'] for item in row['detail']['pending_checkins']] == expected
    review = services['reviews'].get(row['review_id'])
    assert review['snapshot']['review_only'] and not review['snapshot']['actions']
    assert notifier.run_once() == 1
    notice = records(notifier)[0]
    assert notice['purpose'] == 'checkin_gap' and notice['official_checked_at']
    body = feedback.sent[0][1]
    assert body['header']['title']['content'].endswith('资料待补齐')
    assert '请补充打卡资料' in json.dumps(body, ensure_ascii=False)
    assert submit_button(body)['disabled']
    before = list(writes)
    for action in ('save', 'submit'):
        with pytest.raises(DomainError):
            services['reviews'].confirm(review['id'], review['fingerprint'], action=action)
    assert writes == before and not business_submits(writes)
    notifier.recover()
    tick(auto, case['id'])
    notifier.run_once()
    notifier.run_once()
    assert len(feedback.sent) == 1 and len(records(notifier)) == 1


@pytest.mark.parametrize('stage', ['survey', 'repair'])
def test_official_checkins_count_without_any_local_copy(gap_flow, platform, services, case, stage):
    auto, _, notifier, feedback, writes, _ = gap_flow(stage)
    remote = platform.test_survey if stage == 'survey' else platform.test_repair
    remote.update(buildingFileInfoList=[remote_file('website-building')], houseFileInfoList=[remote_file('website-door')])
    row = tick(auto, case['id'])
    assert row['state'] == 'awaiting_confirmation'
    assert row['detail']['pending_checkins'] == []
    assert not services['db'].one("SELECT 1 FROM photos WHERE case_id=? AND role IN ('building_checkin','door_checkin')", (case['id'],))
    assert not services['db'].one("SELECT 1 FROM platform_media WHERE file_id LIKE 'website-%'")
    delivery = notifier.queue(case['id'])
    assert delivery['purpose'] == 'review'
    assert '资料待补齐' not in delivery['content']
    assert not feedback.sent and not business_submits(writes)


def test_official_one_checkin_suppresses_only_that_missing_item(gap_flow, platform, case):
    auto, _, notifier, _, _, _ = gap_flow()
    platform.test_repair['buildingFileInfoList'] = [remote_file('website-building')]
    row = tick(auto, case['id'])
    assert [item['field'] for item in row['detail']['pending_checkins']] == ['houseFileInfoList']
    body = json.loads(notifier.queue(case['id'])['content'])
    text = json.dumps(body, ensure_ascii=False)
    assert '待补：入户打卡照片' in text and '待补：楼栋' not in text
    assert '楼栋打卡照片 1 项（官网已有 1 · 本次选入 0）' in text


def test_website_changes_after_queue_cancel_stale_gap_before_send(gap_flow, platform, case, monkeypatch):
    auto, _, notifier, feedback, writes, _ = gap_flow()
    delivery = notifier.queue(case['id'])
    original_members = feedback.members
    def members(chat):
        platform.test_repair.update(buildingFileInfoList=[remote_file('late-building')],
                                    houseFileInfoList=[remote_file('late-door')])
        return original_members(chat)
    monkeypatch.setattr(feedback, 'members', members)
    notifier.deliver(delivery['id'])
    assert not feedback.sent and not business_submits(writes)
    row = records(notifier)[0]
    assert row['state'] == 'cancelled' and '官网本环节资料已更新' in row['error']
    assert tick(auto, case['id'])['detail']['pending_checkins'] == []
    renewed = notifier.queue(case['id'])
    assert renewed['id'] == delivery['id'] and '资料待补齐' not in renewed['content']


@pytest.mark.parametrize('failure', ['unavailable', 'wrong_case', 'omitted_list', 'null_list'])
def test_unverified_website_does_not_send_a_missing_notice(gap_flow, platform, monkeypatch, failure):
    _, _, notifier, feedback, writes, _ = gap_flow()
    delivery = notifier.queue(notifier.db.one('SELECT case_id FROM automatic_upload_cases')['case_id'])
    if failure == 'wrong_case':
        platform.test_repair['reportReceivingId'] = 'another-case'
    elif failure == 'omitted_list':
        platform.test_repair.pop('houseFileInfoList')
    elif failure == 'null_list':
        platform.test_repair['houseFileInfoList'] = None
    else:
        def unavailable(*args, **kwargs):
            raise DomainError('官网暂不可用', 502)
        monkeypatch.setattr(platform.client, 'snapshot', unavailable)
    notifier.deliver(delivery['id'])
    notice = records(notifier)[0]
    assert notice['state'] == 'queued' and notice['retry_at'] > time.time()
    assert '暂无法核对官网' in notice['error'] and not notice['official_checked_at']
    assert not feedback.sent and not business_submits(writes)
    assert notifier.run_once() == 0  # Back off instead of polling the website every two seconds.


def test_website_stage_completion_cancels_gap(gap_flow, platform):
    _, _, notifier, feedback, writes, _ = gap_flow()
    delivery = notifier.queue(notifier.db.one('SELECT case_id FROM automatic_upload_cases')['case_id'])
    platform.test_row['repairAcceptanceFlowStatus'] = '1'
    notifier.deliver(delivery['id'])
    assert records(notifier)[0]['state'] == 'cancelled'
    assert not feedback.sent and not business_submits(writes)


def test_survey_checkins_do_not_hide_missing_repair_checkins(gap_flow, platform, case):
    auto, _, _, _, _, _ = gap_flow()
    platform.test_survey.update(buildingFileInfoList=[remote_file('survey-only-building')],
                                houseFileInfoList=[remote_file('survey-only-door')])
    row = tick(auto, case['id'])
    assert [item['field'] for item in row['detail']['pending_checkins']] == ['buildingFileInfoList', 'houseFileInfoList']


def test_gap_updates_original_card_when_local_checkins_arrive(gap_flow, services, case, channel, admin_user):
    auto, add, notifier, feedback, writes, _ = gap_flow()
    services['feishu_identities'].save({'connector_id': channel[0]['id'], 'open_id': 'ou_lead', 'user_id': admin_user['id']})
    notifier.run_once()
    original = records(notifier)[0]
    old_action = event(original, 'edit')
    add('red', 'work', 'building_checkin')
    add('blue', 'work', 'door_checkin')
    row = tick(auto, case['id'])
    assert row['detail']['pending_checkins'] == []
    notifier.run_once()
    notifier.run_once()
    latest = records(notifier)[0]
    assert len(feedback.sent) == 1 and len(records(notifier)) == 1
    assert latest['remote_message_id'] == original['remote_message_id']
    assert latest['review_id'] == row['review_id'] and latest['card_token'] != original['card_token']
    assert feedback.updated_cards[-1][0] == original['remote_message_id']
    body = feedback.updated_cards[-1][1]
    assert body['header']['title']['content'] == '修复成果资料待核对'
    assert not submit_button(body).get('disabled')
    assert receive(services['review_cards'], original, old_action)['toast']['type'] == 'error'
    assert not business_submits(writes)


def test_remote_only_completion_rechecks_and_updates_without_resending(gap_flow, platform, services, case, monkeypatch):
    auto, _, notifier, feedback, writes, _ = gap_flow()
    notifier.run_once()
    original = records(notifier)[0]
    assert auto.get(case['id'])['retry_at'] > time.time()
    platform.test_repair.update(buildingFileInfoList=[remote_file('website-building')], houseFileInfoList=[remote_file('website-door')])
    # Preview downloads are independent of attachment existence. Even if the
    # image host is unavailable, the card must not ask to upload existing files.
    def preview_unavailable(*args, **kwargs):
        raise DomainError('图片预览暂不可用', 502)
    monkeypatch.setattr(platform.client, 'download_attachment', preview_unavailable)
    with auto.db.tx() as con:
        con.execute('UPDATE automatic_upload_cases SET retry_at=1 WHERE case_id=?', (case['id'],))
    assert auto.run_once() == 1
    notifier.run_once()
    notifier.run_once()
    assert len(feedback.sent) == 1 and len(records(notifier)) == 1
    assert feedback.updated_cards[-1][0] == original['remote_message_id']
    assert feedback.updated_cards[-1][1]['header']['title']['content'] == '修复成果资料待核对'
    assert '待补打卡资料' not in json.dumps(feedback.updated_cards[-1][1], ensure_ascii=False)
    assert not services['db'].one("SELECT 1 FROM photos WHERE role IN ('building_checkin','door_checkin')")
    assert not business_submits(writes)


def test_other_business_errors_do_not_get_gap_reminder(gap_flow, platform, case):
    auto, _, notifier, feedback, writes, _ = gap_flow()
    data = draft_data(platform, case['id'], 'repair')
    data['values']['repairCrewNumber'] = '0'
    platform.save_draft(case['id'], 'repair', data)
    row = tick(auto, case['id'])
    assert row['state'] == 'needs_fields'
    assert notifier.run_once() == 0 and not feedback.sent
    assert not business_submits(writes)


@pytest.mark.parametrize('attachment', [
    {'id': 'removed', 'fileUrl': '/removed.jpg', 'invalid': True},
    {'id': 'placeholder', 'fileName': '尚未上传.jpg'},
    {'noRequire': True},
])
def test_deleted_or_placeholder_checkins_still_count_as_missing(gap_flow, platform, case, attachment):
    auto, _, _, _, _, _ = gap_flow()
    platform.test_repair['buildingFileInfoList'] = [attachment]
    row = tick(auto, case['id'])
    assert row['state'] == 'awaiting_confirmation'
    assert [item['field'] for item in row['detail']['pending_checkins']] == ['buildingFileInfoList', 'houseFileInfoList']


def test_malformed_official_attachment_list_is_not_reported_as_missing(gap_flow, platform, case):
    auto, _, notifier, feedback, _, _ = gap_flow()
    platform.test_repair['buildingFileInfoList'] = {'unexpected': 'format'}
    assert tick(auto, case['id'])['state'] == 'needs_fields'
    assert notifier.run_once() == 0 and not feedback.sent


def test_callback_cannot_submit_while_checkins_are_missing(gap_flow, services, channel, admin_user):
    _, _, notifier, feedback, writes, _ = gap_flow()
    services['feishu_identities'].save({'connector_id': channel[0]['id'], 'open_id': 'ou_lead', 'user_id': admin_user['id']})
    notifier.run_once()
    delivery = records(notifier)[0]
    cards = services['review_cards']
    response = receive(cards, delivery, event(delivery))
    assert response['toast']['type'] == 'error'
    cards.run_once()
    assert not services['db'].one('SELECT state FROM feishu_review_actions')
    assert len(feedback.sent) == 1 and not business_submits(writes)


def test_gap_reminder_keeps_photo_quiet_window(gap_flow, case):
    auto, _, notifier, feedback, _, row = gap_flow(quiet=300)
    assert row['state'] == 'collecting_photos'
    assert notifier.run_once() == 0 and not feedback.sent
    with auto.db.tx() as con:
        con.execute('UPDATE automatic_upload_cases SET proposal_not_before=1 WHERE case_id=?', (case['id'],))
    assert tick(auto, case['id'])['state'] == 'awaiting_confirmation'
    assert notifier.run_once() == 1
