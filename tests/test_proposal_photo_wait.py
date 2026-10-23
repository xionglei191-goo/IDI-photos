from types import SimpleNamespace

import pytest

from idi.automatic_uploads import AutomaticUploads
from idi.domain import DomainError
from tests.test_automatic_uploads import setup_flow, ready
from tests.test_platform_preparation import platform, draft_data
from tests.test_project_followup import followup, channel, bind


@pytest.fixture
def waiting_flow(services, platform, case, photo, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr('idi.automatic_uploads.time', SimpleNamespace(time=lambda: clock.now))
    auto, add, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch, quiet_seconds=300)
    assert auto.policy()['proposal_quiet_seconds'] == 300
    row = ready(services, platform, case, add, auto)
    assert row['state'] == 'collecting_photos'
    assert row['proposal_not_before'] == 1300
    assert row['review_id'] is None
    return auto, add, uploads, writes, clock


def proposals(auto, case_id):
    return auto.db.all('SELECT * FROM platform_review_batches WHERE case_id=? AND save_only=0', (case_id,))


def test_waits_full_300_seconds_then_generates_one_combined_proposal(waiting_flow, case):
    auto, _, uploads, writes, clock = waiting_flow
    assert len(uploads) == 3 and len(writes) == 1
    assert all(payload['state'] == 0 for _, payload in writes)
    assert not proposals(auto, case['id'])
    clock.now = 1299
    assert auto.run_once() == 0
    assert not proposals(auto, case['id'])
    clock.now = 1300
    assert auto.run_once() == 1
    row = auto.get(case['id'])
    assert row['state'] == 'awaiting_confirmation'
    assert len(proposals(auto, case['id'])) == 1
    assert row['detail']['saved_count'] == 3
    clock.now = 1301
    assert auto.run_once() == 0
    assert auto.get(case['id'])['review_id'] == row['review_id']
    assert len(writes) == 1


def test_late_photo_restarts_wait_and_restart_preserves_remaining_time(waiting_flow, services, case):
    auto, add, uploads, writes, clock = waiting_flow
    clock.now = 1299
    add('yellow', 'survey', 'problem')
    assert auto.run_once() == 1
    assert len(uploads) == 4 and len(writes) == 2
    assert auto.get(case['id'])['proposal_not_before'] == 1599
    resumed = AutomaticUploads(services['workflow'], services['reviews'], services['photo_workflow'])
    for stamp in (1300, 1500, 1598):
        clock.now = stamp
        assert resumed.run_once() == 0
        assert not proposals(auto, case['id'])
    clock.now = 1599
    assert resumed.run_once() == 1
    assert resumed.get(case['id'])['state'] == 'awaiting_confirmation'
    assert resumed.get(case['id'])['detail']['saved_count'] == 4
    assert len(proposals(auto, case['id'])) == 1
    assert len(writes) == 2


def test_field_edits_and_rechecking_do_not_restart_photo_wait(waiting_flow, platform, case):
    auto, _, _, _, clock = waiting_flow
    clock.now = 1100
    platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', {'defectDesc': '人工核实后的原因'}))
    assert auto.run_once() == 1
    assert auto.get(case['id'])['proposal_not_before'] == 1300
    clock.now = 1200
    auto.enroll(case['id'])
    assert auto.run_once() == 1
    assert auto.get(case['id'])['proposal_not_before'] == 1300
    clock.now = 1300
    assert auto.run_once() == 1
    assert auto.get(case['id'])['state'] == 'awaiting_confirmation'
    assert len(proposals(auto, case['id'])) == 1


def test_photo_after_ready_invalidates_old_review_and_waits_again(waiting_flow, services, case):
    auto, add, _, writes, clock = waiting_flow
    clock.now = 1300
    auto.run_once()
    review = services['reviews'].get(auto.get(case['id'])['review_id'])
    clock.now = 1301
    add('yellow', 'survey', 'problem')
    with pytest.raises(DomainError, match='新资料'):
        services['reviews'].confirm(review['id'], review['fingerprint'])
    auto.run_once()
    row = auto.get(case['id'])
    assert row['state'] == 'collecting_photos' and row['review_id'] is None
    assert services['reviews'].get(review['id'])['state'] == 'stale'
    assert row['proposal_not_before'] == 1601
    clock.now = 1600
    assert auto.run_once() == 0
    clock.now = 1601
    assert auto.run_once() == 1
    assert auto.get(case['id'])['review_id'] != review['id']
    assert all(payload['state'] == 0 for _, payload in writes)


def test_no_notification_during_wait_and_one_after_combined_proposal(waiting_flow, services, followup, channel, project):
    auto, add, _, _, clock = waiting_flow
    bind(followup, channel, project)
    auto.configure({'notify_review_ready': True})
    notifier = services['review_notifications']
    assert notifier.run_once() == 0
    clock.now = 1299
    add('yellow', 'survey', 'problem')
    auto.run_once()
    assert notifier.run_once() == 0
    assert not channel[1].sent
    clock.now = 1599
    auto.run_once()
    assert notifier.run_once() == 1
    assert len(channel[1].sent) == 1
    assert notifier.run_once() == 0
    assert len(channel[1].sent) == 1
