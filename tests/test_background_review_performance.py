"""Idle reminders must stay cheap without weakening change/submission guards."""
import pytest

from idi.db import now
from idi.domain import DomainError
from idi.review_notifications import QUEUE_RECHECK_SECONDS
from tests.test_checkin_reminders import gap_flow
from tests.test_platform_preparation import platform, draft_data
from tests.test_project_followup import followup, channel, bind, stage
from tests.test_review_notifications import notification_flow, records


def count_connections(db, monkeypatch):
    calls = []
    original = db.connect

    def connect():
        calls.append(True)
        return original()

    monkeypatch.setattr(db, 'connect', connect)
    return calls


def test_followup_ignores_readback_audit_changes_and_case_sort_order(services, followup, case, project):
    stage(services, case, 'repair')
    other = services['domain'].save_case({'project_id': project['id'], 'title': '另一案件'})
    stage(services, other, 'repair')
    before = followup.signature()
    with services['db'].tx() as con:
        con.execute('UPDATE cases SET revision=revision+1,workflow_verified_at=?,updated_at=?,remote_updated_at=? WHERE id=?',
                    (now(), now(), now(), case['id']))
    assert followup.signature() == before


@pytest.mark.parametrize('field,value', [('title', '现场事项已变化'), ('room', '1203'),
                                        ('repair_parts', '["外墙"]'), ('workflow_stage', 'acceptance'),
                                        ('insurer', '新的保司')])
def test_followup_still_invalidates_business_changes(services, followup, case, field, value):
    stage(services, case, 'repair')
    before = followup.signature()
    with services['db'].tx() as con:
        con.execute(f'UPDATE cases SET {field}=? WHERE id=?', (value, case['id']))
    assert followup.signature() != before


def test_followup_signature_uses_one_connection(services, followup, case, monkeypatch):
    stage(services, case, 'repair')
    calls = count_connections(services['db'], monkeypatch)
    followup.signature()
    assert len(calls) == 1


def test_actual_unchanged_official_recheck_does_not_restart_followup(gap_flow, services, followup, case):
    auto, _, _, _, _, _ = gap_flow()
    before = followup.signature()
    verified = services['domain'].case(case['id'])['workflow_verified_at']
    auto.workflow.refresh_case(case['id'])
    assert services['domain'].case(case['id'])['workflow_verified_at'] != verified
    assert followup.signature() == before


def warm_queue(notifier, monkeypatch):
    # Card synchronization has its own uncached validation every 15 seconds.
    monkeypatch.setattr(notifier.cards, 'run_once', lambda: None)
    notifier.run_once()
    notifier.run_once()  # Observe the delivery created on the first tick.


def test_sent_proposal_idle_ticks_do_not_rebuild_or_reconnect_per_read(notification_flow, case, monkeypatch):
    notifier, _, client, writes = notification_flow
    warm_queue(notifier, monkeypatch)
    before = list(writes)
    calls = count_connections(notifier.db, monkeypatch)
    monkeypatch.setattr(notifier.reviews, 'snapshot', lambda *a, **k: pytest.fail('idle proposal rebuilt'))
    for _ in range(10):
        assert notifier.run_once() == 0
    assert len(calls) <= 40  # Policy, candidate list, one observation, delivery list per tick.
    assert len(client.sent) == 1 and writes == before


def test_full_current_validation_uses_one_connection(notification_flow, case, monkeypatch):
    notifier, _, _, _ = notification_flow
    calls = count_connections(notifier.db, monkeypatch)
    notifier.current(case['id'])
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['business', 'draft', 'owner', 'dictionary', 'review'])
def test_queue_cache_observes_changes_immediately(notification_flow, case, project, platform, monkeypatch, change):
    notifier, _, _, _ = notification_flow
    warm_queue(notifier, monkeypatch)
    calls = []
    original = notifier.queue
    monkeypatch.setattr(notifier, 'queue', lambda case_id: (calls.append(case_id), original(case_id))[1])
    if change == 'draft':
        data = draft_data(platform, case['id'], 'survey')
        data['values']['defectDesc'] = '新的现场核实内容'
        platform.save_draft(case['id'], 'survey', data)
    elif change == 'dictionary':
        notifier.db.set_setting('platform_dictionaries', {'items': {}, 'version': 'changed'})
    else:
        with notifier.db.tx() as con:
            if change == 'business':
                con.execute('UPDATE cases SET title=? WHERE id=?', ('新的事项', case['id']))
            elif change == 'owner':
                con.execute('UPDATE followup_contacts SET open_id=? WHERE project_id=?', ('ou_newowner', project['id']))
            else:
                con.execute("UPDATE platform_review_batches SET state='stale' WHERE id=(SELECT review_id FROM automatic_upload_cases WHERE case_id=?)", (case['id'],))
    notifier.run_once()
    assert calls == [case['id']]


def test_bounded_cache_retries_and_does_not_hide_changes_during_queue(notification_flow, case, monkeypatch):
    notifier, _, _, _ = notification_flow
    warm_queue(notifier, monkeypatch)
    key, stamp = notifier._queue_checks[case['id']]
    notifier._queue_checks[case['id']] = (key, stamp - QUEUE_RECHECK_SECONDS - 1)
    calls = []
    original = notifier.queue

    def queue(case_id):
        calls.append(case_id)
        result = original(case_id)
        if len(calls) == 1:
            with notifier.db.tx() as con:
                con.execute('UPDATE cases SET title=? WHERE id=?', ('排队期间修改', case_id))
        return result

    monkeypatch.setattr(notifier, 'queue', queue)
    notifier.run_once()
    notifier.run_once()
    assert calls == [case['id'], case['id']]


def test_poll_cache_never_authorizes_send_after_material_change(notification_flow, case, platform, monkeypatch):
    notifier, _, client, _ = notification_flow
    warm_queue(notifier, monkeypatch)
    delivery = records(notifier)[0]
    with notifier.db.tx() as con:
        con.execute("UPDATE review_notifications SET state='queued' WHERE id=?", (delivery['id'],))
    data = draft_data(platform, case['id'], 'survey')
    data['values']['defectDesc'] = ''
    platform.save_draft(case['id'], 'survey', data)
    notifier.deliver(delivery['id'])
    assert records(notifier)[0]['state'] == 'cancelled'
    assert len(client.sent) == 1
    with pytest.raises(DomainError):
        notifier.current(case['id'])
