import json
from datetime import datetime, timedelta, timezone

import pytest

from idi.context_memory import MEMBER_CONTEXT_LIMIT, HISTORY_BUDGET, identity
from idi.db import dump
from idi.domain import DomainError
from idi.feishu_agent import stamp
from tests.test_feishu_agent import bot, capture, task_for, response, arrange_model
from tests.test_feishu_topics import complete


def accept_since(bot, instant):
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_agent_bindings SET activated_at=? WHERE connector_id=?',
                    (stamp(instant), bot.test_connector['id']))
        con.execute('UPDATE feishu_state SET started_at=? WHERE connector_id=?',
                    (instant.timestamp(), bot.test_connector['id']))


def test_followup_includes_own_unmentioned_question_without_triggering_a_reply(bot, monkeypatch):
    instant = datetime.now(timezone.utc)
    accept_since(bot, instant - timedelta(days=2))
    capture(bot, 'OLDER_REQUEST 昨晚另一个项目的问题。', mention=False,
            timestamp=int((instant - timedelta(hours=6)).timestamp() * 1000))
    first = capture(bot, '你现在是什么模型？', timestamp=int((instant - timedelta(hours=5)).timestamp() * 1000))
    bot.collect_messages()
    complete(bot, first, '我是项目群助手。')
    missed = capture(bot, '你可以为我提供哪些服务？', mention=False,
                     timestamp=int((instant - timedelta(hours=4, minutes=59)).timestamp() * 1000))
    assert bot.collect_messages() == 0
    assert bot.db.one('SELECT id FROM feishu_agent_tasks WHERE source_message_id=?', (missed,)) is None
    current = capture(bot, '看到我的问题了么？')
    assert bot.collect_messages() == 1
    calls = arrange_model(bot, monkeypatch, [response(text='看到了，您问的是可以提供哪些服务。')])
    assert bot.run_one()
    prompt = dump(calls[-1])
    assert '你可以为我提供哪些服务？' in prompt
    assert 'OLDER_REQUEST' not in prompt
    background = next(json.loads(m['content']) for m in calls[-1]
                      if m['role'] == 'user' and m['content'].startswith('{') and 'recent_member_messages' in json.loads(m['content']))
    assert background['timezone'] == 'Asia/Shanghai'
    assert background['recent_member_messages'][0]['sent_at'].endswith('+08:00')
    assert prompt.index('我是项目群助手。') < prompt.index('你可以为我提供哪些服务？') < prompt.index('看到我的问题了么？')
    task = task_for(bot, current)
    assert task['state'] == 'completed'
    assert task['usage']['context']['member_messages'] == 1
    assert len(bot.db.all('SELECT id FROM feishu_agent_tasks')) == 2


def change_payload(bot, mid, **values):
    row = bot.db.one('SELECT payload FROM feishu_inbox WHERE message_id=?', (mid,))
    payload = {**json.loads(row['payload']), **values}
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_inbox SET payload=? WHERE message_id=?', (dump(payload), mid))


def test_background_stays_with_the_current_member_group_and_unthreaded_messages(bot, monkeypatch):
    capture(bot, 'OWN_BACKGROUND 请说明可以提供哪些服务。', mention=False)
    capture(bot, 'OTHER_MEMBER 另一成员的个人安排。', mention=False, sender='ou_owner')
    foreign = capture(bot, 'OTHER_GROUP 另一群的消息。', mention=False)
    change_payload(bot, foreign, chat_id='oc_other')
    capture(bot, 'OTHER_TOPIC 同一个人的另一个话题。', mention=False, thread_id='omt_other')
    root = capture(bot, 'TOPIC_ROOT 后来成为共享话题的根消息。', mention=False)
    capture(bot, 'TOPIC_REPLY', mention=False, sender='ou_owner', parent_id=root)
    current = capture(bot, '看到我的补充了吗？')
    assert bot.collect_messages() == 1
    calls = arrange_model(bot, monkeypatch, [response(text='已看到您本人的补充。')])
    bot.run_one()
    prompt = dump(calls)
    assert 'OWN_BACKGROUND' in prompt
    for excluded in ('OTHER_MEMBER', 'OTHER_GROUP', 'OTHER_TOPIC', 'TOPIC_ROOT', 'TOPIC_REPLY'):
        assert excluded not in prompt
    assert task_for(bot, current)['usage']['context']['member_messages'] == 1


def test_explicit_topic_does_not_borrow_unthreaded_member_background(bot, monkeypatch):
    capture(bot, 'PRIVATE_BACKGROUND 不应进入共享话题。', mention=False)
    root = capture(bot, 'SHARED_TOPIC 共同安排。', mention=False, thread_id='omt_shared')
    current = capture(bot, '概括这个话题。', parent_id=root, thread_id='omt_shared')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='已核对共同安排。')])
    bot.run_one()
    assert 'SHARED_TOPIC' in dump(calls) and 'PRIVATE_BACKGROUND' not in dump(calls)
    assert task_for(bot, current)['usage']['context']['member_messages'] == 0


def test_background_uses_the_trigger_time_and_received_snapshot(bot, monkeypatch):
    instant = datetime.now(timezone.utc)
    accept_since(bot, instant - timedelta(days=2))
    capture(bot, 'EXPIRED 过期普通消息。', mention=False,
            timestamp=int((instant - timedelta(hours=25)).timestamp() * 1000))
    capture(bot, 'RECENT 当前范围的普通消息。', mention=False,
            timestamp=int((instant - timedelta(hours=4)).timestamp() * 1000))
    capture(bot, 'FUTURE 时间在本次提问之后。', mention=False,
            timestamp=int((instant + timedelta(hours=1)).timestamp() * 1000))
    current = capture(bot, '看到我的补充了吗？')
    capture(bot, 'LATE 提问之后才收到的旧时间消息。', mention=False,
            timestamp=int((instant - timedelta(hours=1)).timestamp() * 1000))
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='仅核对提问时已收到的近期补充。')])
    bot.run_one()
    assert 'RECENT' in dump(calls)
    assert all(text not in dump(calls) for text in ('EXPIRED', 'FUTURE', 'LATE'))
    assert task_for(bot, current)['usage']['context']['member_messages'] == 1


def test_rebinding_and_non_user_or_deleted_messages_are_not_reintroduced(bot, monkeypatch):
    instant = datetime.now(timezone.utc)
    accept_since(bot, instant - timedelta(hours=1))
    capture(bot, 'PREVIOUS_BINDING 旧绑定的消息。', mention=False,
            timestamp=int((instant - timedelta(minutes=30)).timestamp() * 1000))
    assert bot.collect_messages() == 0
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_agent_bindings SET revision=revision+1,activated_at=? WHERE connector_id=?',
                    (stamp(instant - timedelta(minutes=1)), bot.test_connector['id']))
    deleted = capture(bot, 'DELETED_MESSAGE', mention=False)
    change_payload(bot, deleted, deleted=True)
    robot = capture(bot, 'BOT_MESSAGE', mention=False)
    change_payload(bot, robot, sender={'sender_type': 'app', 'sender_id': {'open_id': 'ou_member'}})
    capture(bot, 'NEW_BINDING 新绑定的补充。', mention=False)
    current = capture(bot, '请核对我的补充。')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='按当前群设置核对。')])
    bot.run_one()
    assert 'NEW_BINDING' in dump(calls)
    assert all(text not in dump(calls) for text in ('PREVIOUS_BINDING', 'DELETED_MESSAGE', 'BOT_MESSAGE'))
    assert task_for(bot, current)['usage']['context']['member_messages'] == 1


def test_clear_skips_existing_member_messages_but_keeps_later_supplements(bot, monkeypatch):
    capture(bot, 'FORGOTTEN_BACKGROUND 原来的个人补充。', mention=False)
    first = capture(bot, '请核对原来的补充。')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='已核对原来的补充。')])
    bot.run_one()
    assert 'FORGOTTEN_BACKGROUND' in dump(calls)
    key = identity(task_for(bot, first), 'feishu')[0]
    summary = bot.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=?', (key,))
    bot.memory.context.edit(summary, {'revision': summary['revision'], 'clear': True})
    capture(bot, 'AFTER_CLEAR 清空后的新补充。', mention=False)
    current = capture(bot, '从新补充继续。')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='仅接续清空后的内容。')])
    bot.run_one()
    assert 'AFTER_CLEAR' in dump(calls) and 'FORGOTTEN_BACKGROUND' not in dump(calls)
    assert task_for(bot, current)['usage']['context']['member_messages'] == 1
    changed = bot.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=?', (key,))
    assert changed['member_context_cursor'] > 0
    bot.memory.context.edit(changed, {'revision': changed['revision'], 'text': '之后的人工更正。'})
    later = capture(bot, '继续确认。')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='继续使用新补充。')])
    bot.run_one()
    assert 'AFTER_CLEAR' in dump(calls) and 'FORGOTTEN_BACKGROUND' not in dump(calls)
    assert task_for(bot, later)['state'] == 'completed'


def test_background_limits_keep_latest_whole_messages_and_report_omissions(bot, monkeypatch):
    for index in range(MEMBER_CONTEXT_LIMIT + 3):
        capture(bot, f'BACKGROUND_{index:02} 普通补充。', mention=False)
    capture(bot, 'TOO_LARGE ' + '长消息' * 4000, mention=False)
    current = capture(bot, '概括刚才的补充。')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='根据已提供的补充概括。')])
    bot.run_one()
    prompt = dump(calls)
    assert 'BACKGROUND_22' in prompt and 'BACKGROUND_00' not in prompt and 'TOO_LARGE' not in prompt
    task = task_for(bot, current)
    metrics = task['usage']['context']
    assert metrics['member_messages'] == MEMBER_CONTEXT_LIMIT - 1
    assert metrics['member_messages_omitted'] == 5
    assert metrics['estimated_history_tokens'] < HISTORY_BUDGET
    assert task['state'] == 'partial' and '还有 5 条您的群消息' in task['result']['text']


def test_background_does_not_authorize_a_memory_write(bot, monkeypatch):
    quote = '请记住我的偏好：以后先写处理结果。'
    capture(bot, quote, mention=False)
    current = capture(bot, '看到了吗？')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='已看到补充，尚未保存偏好。')])
    bot.run_one()
    assert quote in dump(calls)
    task = task_for(bot, current)
    with pytest.raises(DomainError, match='本次用户消息'):
        bot.memory.propose(task, 'feishu', task['input']['allowed_project_ids'],
            {'scope': 'personal', 'kind': 'preference', 'title': '答复顺序', 'quote': quote})
    assert bot.db.one('SELECT COUNT(*) n FROM agent_memories')['n'] == 0


def test_upgrade_preserves_a_preexisting_clear_boundary(bot, monkeypatch):
    capture(bot, 'BEFORE_OLD_CLEAR 普通补充。', mention=False)
    first = capture(bot, '先核对。')
    bot.collect_messages()
    arrange_model(bot, monkeypatch, [response(text='已核对。')])
    bot.run_one()
    key = identity(task_for(bot, first), 'feishu')[0]
    summary = bot.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=?', (key,))
    bot.memory.context.edit(summary, {'revision': summary['revision'], 'clear': True})
    # Recreate the previous schema; migration must honor an already-cleared summary.
    with bot.db.tx() as con:
        con.execute('DROP INDEX feishu_inbox_member_context')
        con.execute('ALTER TABLE agent_context_summaries DROP COLUMN member_context_cursor')
        con.execute('DELETE FROM schema_version WHERE version=23')
    bot.db.initialize()
    current = capture(bot, '升级后继续。')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='从清空后的对话继续。')])
    bot.run_one()
    assert 'BEFORE_OLD_CLEAR' not in dump(calls)
    assert task_for(bot, current)['usage']['context']['member_messages'] == 0
