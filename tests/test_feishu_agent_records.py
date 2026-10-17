from datetime import datetime, timedelta, timezone

from idi.db import dump
from idi.feishu_agent import stamp
from tests.test_feishu_agent import bot, capture, task_for


def test_history_shows_source_reply_and_sender_without_external_requests(bot, client, monkeypatch):
    mid = capture(bot, '@_user_1 请查询案件 JMDHS-A3-3001')
    bot.collect_messages()
    task = task_for(bot, mid)
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_agent_tasks SET result_json=? WHERE id=?',
            (dump({'text': '当前处于查勘阶段。\n资料待核对。', 'internal_value': 'not-for-history'}), task['id']))
    monkeypatch.setattr(bot.followup, 'client', lambda *_: (_ for _ in ()).throw(AssertionError('history must stay local')))
    before = bot.db.all('SELECT * FROM feishu_agent_tasks')
    row = client.get('/api/feishu-agent').json()['tasks'][0]
    assert row['source_excerpt'] == '请查询案件 JMDHS-A3-3001'
    assert row['reply_excerpt'] == '当前处于查勘阶段。 资料待核对。'
    assert row['chat_name'] == '项目群' and row['connector_id'] == bot.test_connector['id']
    assert row['sender_label'].startswith('成员') and not row['sender_name_known']
    assert row['conversation_mode'] == 'member' and not row['mention_only']
    assert not {'input_json', 'result_json', 'sender_id', 'bot_open_id', 'mentions', 'captured_sender_name', 'contact_name'} & row.keys()
    assert 'not-for-history' not in dump(row)
    assert bot.db.all('SELECT * FROM feishu_agent_tasks') == before
    assert not bot.test_client.sent


def test_history_distinguishes_mentions_image_counts_and_known_names(bot, project):
    only_at = capture(bot, '@_user_1')
    image_mid = capture(bot, '', images=1, mention=False, sender='ou_owner')
    bot.collect_messages()
    bot.followup.save_contact(project['id'], {'connector_id': bot.test_connector['id'], 'open_id': 'ou_owner', 'auto_feedback': False})
    rows = {r['source_message_id']: r for r in bot.tasks()}
    assert rows[only_at]['source_excerpt'] == '' and rows[only_at]['mention_only']
    assert rows[image_mid]['image_count'] == 1 and not rows[image_mid]['mention_only']
    assert rows[image_mid]['sender_label'] == '项目负责人' and rows[image_mid]['sender_name_known']


def test_history_is_bounded_and_preview_keeps_truncation_visible(bot):
    binding = bot.binding(bot.test_connector['id'])
    start = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with bot.db.tx() as con:
        for index in range(65):
            bot._insert_task(con, binding, 'brief', f'test-history:{index}',
                {'text': '长消息' * 100, 'date': '2026-10-15'}, created_at=stamp(start + timedelta(minutes=index)))
        con.execute('UPDATE feishu_agent_tasks SET result_json=?', (dump({'text': '较长回复' * 100}),))
    rows = bot.tasks()
    assert len(rows) == 60
    assert rows[0]['created_at'] == stamp(start + timedelta(minutes=64))
    assert rows[-1]['created_at'] == stamp(start + timedelta(minutes=5))
    assert len(rows[0]['source_excerpt']) == 181 and rows[0]['source_excerpt'].endswith('…')
    assert len(rows[0]['reply_excerpt']) == 241 and rows[0]['reply_excerpt'].endswith('…')
    assert rows[0]['sender_label'] == '定时任务' and rows[0]['brief_date'] == '2026-10-15'
