"""Event/deadline scheduling retains live validation and restart recovery."""
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from idi.feishu_agent import CHINA, stamp
from tests.test_feishu_agent import bot, capture, task_for
from tests.test_feishu_review_cards import bound_cards, event, receive
from tests.test_review_notifications import notification_flow, records
from tests.test_project_followup import followup, channel
from tests.test_platform_preparation import platform, draft_data
from tests.test_automatic_uploads import setup_flow, ready


def test_feishu_idle_skips_collection_and_daily_brief_checks_but_wakes_for_message(bot, monkeypatch):
    original = bot.collect_messages
    calls = []
    monkeypatch.setattr(bot, 'collect_messages', lambda: (calls.append(True), original())[1])
    instant = datetime(2026, 10, 30, 17, 59, tzinfo=CHINA)
    bot.schedule_once(instant)
    for _ in range(8):
        bot.schedule_once(instant)
    assert len(calls) == 1
    assert bot.db.runtime.snapshot()['tasks']['feishu.schedule']['brief_checks'] == 1
    mid = capture(bot, '请查询当前情况')
    bot.schedule_once(instant)
    assert task_for(bot, mid)['state'] == 'queued'
    assert len(calls) == 2


def test_feishu_brief_deadline_restart_and_config_change(bot, project):
    start = datetime(2026, 10, 30, 0, 0, tzinfo=CHINA)
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_agent_bindings SET activated_at=?', (stamp(start),))
        con.execute('INSERT INTO followup_contacts VALUES(?,?,?,?,?,?,?)',
                    (project['id'], bot.test_connector['id'], 'ou_owner', '负责人', 0, stamp(start), stamp(start)))
    assert bot.schedule_once(start.replace(hour=17, minute=59, second=59)) == 1
    assert not bot.db.all("SELECT id FROM feishu_agent_tasks WHERE kind='brief'")
    bot.schedule_once(start.replace(hour=18))
    assert len(bot.db.all("SELECT id FROM feishu_agent_tasks WHERE kind='brief'")) == 1
    bot._brief_config, bot._brief_next = None, 0
    bot.schedule_once(start.replace(hour=18, minute=2))
    assert len(bot.db.all("SELECT id FROM feishu_agent_tasks WHERE kind='brief'")) == 1
    with bot.db.tx() as con:
        con.execute("UPDATE feishu_agent_bindings SET brief_time='19:00'")
    bot.schedule_once(start.replace(hour=18, minute=2))
    assert bot._brief_next == start.replace(hour=19).timestamp()
    bot._brief_config, bot._brief_next = None, 0
    bot.schedule_once(start + timedelta(days=1, hours=21))
    assert bot.db.one("SELECT count(*) n FROM feishu_agent_tasks WHERE state='missed'")['n'] == 1


def test_feishu_failed_brief_check_is_retried(bot, monkeypatch):
    original = bot.schedule_briefs
    monkeypatch.setattr(bot, 'schedule_briefs', lambda *a: (_ for _ in ()).throw(ValueError('temporary')))
    with pytest.raises(ValueError):
        bot.schedule_once()
    assert bot._brief_config is None and bot._brief_next == 0
    monkeypatch.setattr(bot, 'schedule_briefs', original)
    bot.schedule_once()
    assert bot._brief_next > time.time()


def test_feishu_fallback_rechecks_and_write_during_collection_is_not_lost(bot, monkeypatch):
    bot.schedule_once()
    bot._collect_at -= 16
    original, calls = bot.collect_messages, []
    def collect():
        calls.append(True)
        result = original()
        if len(calls) == 1:
            capture(bot, '处理期间刚到达')
        return result
    monkeypatch.setattr(bot, 'collect_messages', collect)
    bot.schedule_once()
    bot.schedule_once()
    assert len(calls) == 2
    assert bot.db.one("SELECT count(*) n FROM feishu_agent_tasks WHERE state='queued'")['n'] == 1


def test_feishu_idle_worker_never_takes_writer_lock(bot, monkeypatch):
    monkeypatch.setattr(bot.db, 'tx', lambda: pytest.fail('idle worker opened writer transaction'))
    assert bot.run_one() is False


def test_disabled_automatic_worker_has_bounded_wait(services):
    auto = services['automatic_uploads']
    auto._scan_at = time.monotonic() - 100
    auto.run_once()
    assert auto.next_delay() > 14


def test_automatic_idle_scan_and_retry_deadline(services, platform, case, photo, monkeypatch):
    auto, add, _, _, _ = setup_flow(services, platform, case, photo, monkeypatch)
    ready(services, platform, case, add, auto)
    auto.run_once()
    auto.run_once()
    calls = []
    original = auto._discover
    monkeypatch.setattr(auto, '_discover', lambda: (calls.append(True), original())[1])
    auto.run_once()
    assert calls == []
    auto._retry_at = 1
    auto.run_once()
    assert calls == [True]
    with auto.db.tx() as con:
        con.execute('UPDATE cases SET title=? WHERE id=?', ('新资料', case['id']))
    auto.run_once()
    assert len(calls) == 2


def test_cards_reuse_unchanged_observation_but_recheck_expired_cache(bound_cards, monkeypatch):
    cards, delivery, _, _ = bound_cards
    cards.synchronize(force=True)
    original, calls = cards.notifications._matches, []
    monkeypatch.setattr(cards.notifications, '_matches', lambda row: (calls.append(True), original(row))[1])
    cards.synchronize(force=True)
    assert not calls
    observed, checked = cards._sync_checks[delivery['id']]
    cards._sync_checks[delivery['id']] = observed, checked - 61
    cards.synchronize(force=True)
    assert calls == [True]


def test_cards_missing_upload_record_is_stale_not_worker_crash(bound_cards):
    cards, delivery, _, _ = bound_cards
    with cards.db.tx() as con:
        con.execute('DELETE FROM automatic_upload_cases WHERE case_id=?', (delivery['case_id'],))
    cards.synchronize(force=True)
    assert json.loads(cards.delivery(delivery['id'])['card_view'])['mode'] == 'stale'


def test_card_cache_hits_do_not_postpone_full_revalidation(bound_cards, monkeypatch):
    from types import SimpleNamespace
    cards, delivery, _, _ = bound_cards
    cards.synchronize(force=True)
    checked = cards.delivery(delivery['id'])['card_checked_at']
    original, calls = cards.notifications._matches, []
    monotonic = time.monotonic()
    clock = SimpleNamespace(now=checked + 55, monotonic=monotonic + 55)
    monkeypatch.setattr('idi.feishu_review_cards.time', SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.monotonic))
    monkeypatch.setattr(cards.notifications, '_matches', lambda row: (calls.append(True), original(row))[1])
    cards.synchronize(force=True)
    assert not calls
    cards._poll_token = cards.notifications._signal.token()
    clock.now, clock.monotonic = checked + 61, monotonic + 61
    cards.run_once()
    assert calls == [True]


def test_cached_card_cannot_submit_after_business_change(bound_cards, platform):
    cards, delivery, _, writes = bound_cards
    cards.synchronize(force=True)
    data = draft_data(platform, delivery['case_id'], 'survey')
    data['values']['defectDesc'] = '新的现场核实内容'
    platform.save_draft(delivery['case_id'], 'survey', data)
    receive(cards, delivery, event(delivery))
    cards.run_once()
    assert not [payload for _, payload in writes if payload.get('state') == 1]


def test_heavy_slot_defers_automatic_work_but_manual_checks_still_run(services, monkeypatch):
    archive, followup = services['archive_tree'], services['followup']
    calls = []
    monkeypatch.setattr(archive, 'sync', lambda **kwargs: calls.append('archive') or {'error_count': 0})
    with services['db'].runtime.slot('occupied'):
        assert archive.background_sync() is None
        assert followup.run_once() == 1
        request = followup.request_review()
        monkeypatch.setattr(followup, 'review', lambda run_id: calls.append(run_id))
        assert followup.run_once() == 30
        assert calls == [request['id']]
        archive.sync()
    assert archive.background_sync() == {'error_count': 0}


def test_admin_diagnostics_deny_member_and_anonymous(client, services, app, project):
    from tests.test_project_access import make_person, login_person
    endpoint = '/api/diagnostics/background'
    assert client.get(endpoint).status_code == 200
    make_person(services, project['id'], role='member', name='metrics-member')
    member = login_person(app, 'metrics-member')
    try:
        assert member.get(endpoint).status_code == 403
    finally:
        member.close()
    with TestClient(app, base_url='http://localhost') as anonymous:
        assert anonymous.get(endpoint).status_code == 401
