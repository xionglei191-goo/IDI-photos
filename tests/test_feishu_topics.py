import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from idi.context_memory import identity, personal_identity
from idi.connectors.feishu_feedback import FeishuFeedbackClient, UncertainDelivery
from idi.db import dump
from idi.domain import DomainError
from idi.feishu_topics import FeishuTopics
from idi.workspace_chat import owner_hash
from tests.test_feishu_agent import bot, capture, task_for, response, arrange_model, organized_args


def complete(bot, mid, answer='原问题已核对。'):
    task = task_for(bot, mid)
    with bot.db.tx() as con:
        con.execute("UPDATE feishu_agent_tasks SET state='completed',result_json=? WHERE id=?", (dump({'text': answer}), task['id']))
    return task


def test_reply_topic_shares_multiple_speakers_and_unmentioned_context(bot, monkeypatch):
    private = capture(bot, 'SENDER_OTHER_TOPIC 不应引入整段发言人历史')
    bot.collect_messages(); complete(bot, private)
    root = capture(bot, '本话题核对案件 ZLBH20261000001111，约定每周五检查。')
    bot.collect_messages(); complete(bot, root, 'ROOT_ANSWER 原问题的公开答复。')
    capture(bot, 'UNMENTIONED_UPDATE 我补充了现场原件。', sender='ou_owner', mention=False, root_id=root, parent_id=root)
    current = capture(bot, '接着概括大家刚才的安排。', sender='ou_owner', root_id=root, parent_id=root)
    assert bot.collect_messages() == 1
    calls = arrange_model(bot, monkeypatch, [response(text='已核对同话题记录。')])
    bot.run_one()
    task = task_for(bot, current)
    text = dump(calls)
    assert task['input']['conversation']['mode'] == 'topic' and task['conversation_key']
    assert 'ZLBH20261000001111' in text and 'ROOT_ANSWER' in text and 'UNMENTIONED_UPDATE' in text
    assert 'SENDER_OTHER_TOPIC' not in text
    assert len(bot.db.all('SELECT * FROM feishu_agent_tasks')) == 3


def test_distinct_topics_and_unthreaded_member_context_stay_separate(bot, monkeypatch):
    first = capture(bot, 'TOPIC_ONE 这宗先查原件', thread_id='omt_first')
    second = capture(bot, 'TOPIC_TWO 另一宗待核对', thread_id='omt_second')
    bot.collect_messages(); complete(bot, first); complete(bot, second)
    current = capture(bot, '继续第一个话题', sender='ou_owner', root_id=first, thread_id='omt_first')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='接续第一个。')])
    bot.run_one()
    assert 'TOPIC_ONE' in dump(calls) and 'TOPIC_TWO' not in dump(calls)
    own = capture(bot, 'MEMBER_PRIVATE 只有我的无话题提问')
    bot.collect_messages(); complete(bot, own)
    another = capture(bot, '新提问，不引用任何话题', sender='ou_owner')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='没有共享话题。')])
    bot.run_one()
    assert 'MEMBER_PRIVATE' not in dump(calls) and 'TOPIC_ONE' not in dump(calls)
    assert task_for(bot, another)['conversation_key'] == ''


def test_parent_only_reply_chain_and_later_native_thread_use_one_context(bot, monkeypatch):
    root = capture(bot, 'CHAIN_ROOT 原始安排', mention=False)
    first = capture(bot, '第一位回复者', parent_id=root)
    bot.collect_messages(); task1 = complete(bot, first)
    second = capture(bot, '第二位回复者', sender='ou_owner', parent_id=first)
    bot.collect_messages(); task2 = complete(bot, second)
    third = capture(bot, '现在转入原生话题', root_id=root, thread_id='omt_native')
    bot.collect_messages(); task3 = task_for(bot, third)
    assert task1['conversation_key'] == task2['conversation_key'] == task3['conversation_key']
    assert identity(task1, 'feishu') == identity(task2, 'feishu')
    calls = arrange_model(bot, monkeypatch, [response(text='沿原链继续。')])
    bot.run_one()
    assert 'CHAIN_ROOT' in dump(calls) and '第二位回复者' in dump(calls)


def test_personal_preferences_are_owned_by_speaker_not_shared_topic(bot, project):
    quote = '请记住我的偏好：每次先写下一步核对事项。'
    first = capture(bot, quote, thread_id='omt_preferences')
    second = capture(bot, '我来接着查询', sender='ou_owner', root_id=first, thread_id='omt_preferences')
    bot.collect_messages()
    a, b = task_for(bot, first), task_for(bot, second)
    assert identity(a, 'feishu') == identity(b, 'feishu')
    assert personal_identity(a, 'feishu') != personal_identity(b, 'feishu')
    record = bot.memory.propose(a, 'feishu', [project['id']], {'scope': 'personal', 'kind': 'preference', 'title': '答复偏好', 'quote': quote})
    bot.memory.update(owner_hash('test-only-management-' * 2), record['memory_id'], {'revision': 1, 'action': 'confirm'}, project_id=project['id'])
    assert record['memory_id'] in dump(bot.memory.for_prompt(a, 'feishu', [project['id']]))
    assert record['memory_id'] not in dump(bot.memory.for_prompt(b, 'feishu', [project['id']]))


def test_shared_summary_sources_clear_and_backup_preserve_scope(bot, project, monkeypatch, tmp_path):
    root = capture(bot, 'ORIGINAL_TOPIC 约定周五核对', mention=False, thread_id='omt_summary')
    for index in range(7):
        capture(bot, f'成员补充安排 {index}', sender='ou_owner' if index % 2 == 0 else 'ou_member', mention=False, root_id=root, thread_id='omt_summary')
    foreign = capture(bot, 'FOREIGN_TOPIC 不应出现', mention=False, thread_id='omt_foreign')
    current = capture(bot, '概括共同安排', sender='ou_owner', root_id=root, thread_id='omt_summary')
    bot.collect_messages()
    def summarize(messages, definitions, timeout):
        if definitions[0]['function']['name'] != 'finish_context_summary':
            return response(text='共同核对安排已整理。')
        body = json.loads(messages[-1]['content'])
        return response(('finish_context_summary', {'focus': [{'text': '两位成员共同约定周五核对', 'sources': [t['source'] for t in body['turns'][:2]]}], 'agreements': [], 'pending': []}))
    monkeypatch.setattr(bot, 'request_model', summarize)
    bot.run_one()
    key = identity(task_for(bot, current), 'feishu')[0]
    owner = owner_hash('test-only-management-' * 2)
    summary = bot.memory.managed_summary(owner, key, project_id=project['id'])
    assert summary['conversation_mode'] == 'topic' and summary['source_count'] == 4
    assert len({r['speaker'] for r in summary['source_turns']}) == 2
    assert 'ORIGINAL_TOPIC' in dump(summary) and 'FOREIGN_TOPIC' not in dump(summary)
    assert '共享话题' in bot.memory.list(owner, project_id=project['id'])['summary_options'][0]['label']
    row = bot.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=?', (key,))
    foreign_id = bot.db.one('SELECT id FROM feishu_topic_messages WHERE source_message_id=?', (foreign,))['id']
    damaged = {**row, 'summary_json': dump({'focus': [{'text': '损坏引用', 'sources': ['T:' + foreign_id]}]})}
    assert bot.memory.context.public(damaged)['source_turns'] == []
    before = bot.db.all('SELECT * FROM feishu_topic_messages ORDER BY rowid')
    cleared = bot.memory.context.edit(row, {'revision': row['revision'], 'clear': True})
    assert cleared['sections'] == {}
    with pytest.raises(DomainError, match='其他操作修改'):
        bot.memory.context.edit(row, {'revision': row['revision'], 'text': '旧修订不得覆盖'})
    capture(bot, '另起一段核对', sender='ou_member', parent_id=current)
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='从这里重新接续。')])
    bot.run_one()
    assert 'ORIGINAL_TOPIC' not in dump(calls) and '成员补充安排' not in dump(calls)
    assert bot.db.all('SELECT * FROM feishu_topic_messages ORDER BY rowid')[:len(before)] == before
    from idi.db import Database
    backup = tmp_path / 'restored.sqlite3'
    bot.db.backup(backup)
    restored = Database(backup); restored.initialize()
    for table in ('feishu_topic_messages', 'feishu_topic_aliases', 'agent_context_summaries'):
        assert restored.all('SELECT * FROM ' + table + ' ORDER BY rowid') == bot.db.all('SELECT * FROM ' + table + ' ORDER BY rowid')


@pytest.mark.parametrize('metadata', [{'parent_id': 'bad-parent'}, {'thread_id': 'om_wrong-type'}, {'root_id': ['om_wrong-type']}])
def test_invalid_reply_metadata_isolated_not_member_fallback(bot, monkeypatch, metadata):
    prior = capture(bot, 'PRIVATE_CONTEXT 不应回退到个人历史')
    bot.collect_messages(); complete(bot, prior)
    current = capture(bot, '异常引用的新问题', **metadata)
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='仅处理当前问题。')])
    bot.run_one()
    task = task_for(bot, current)
    assert task['input']['conversation']['mode'] == 'isolated' and task['state'] == 'partial'
    assert 'PRIVATE_CONTEXT' not in dump(calls)
    assert '回复标识无效' in task['result']['text']


def test_conflicting_established_topics_never_merge(bot, monkeypatch):
    first = capture(bot, 'FIRST_PRIVATE', thread_id='omt_conflict_one')
    second = capture(bot, 'SECOND_PRIVATE', thread_id='omt_conflict_two')
    bot.collect_messages(); complete(bot, first); complete(bot, second)
    current = capture(bot, '冲突引用', root_id=first, thread_id='omt_conflict_two')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='请核对回复位置。')])
    bot.run_one()
    assert task_for(bot, current)['input']['conversation']['mode'] == 'isolated'
    assert 'FIRST_PRIVATE' not in dump(calls) and 'SECOND_PRIVATE' not in dump(calls)


def test_missing_or_pre_activation_root_reports_incomplete_context(bot, monkeypatch):
    old = capture(bot, 'BEFORE_ACTIVATION', mention=False)
    payload = json.loads(bot.db.one('SELECT payload FROM feishu_inbox WHERE message_id=?', (old,))['payload'])
    payload['create_time'] = str(int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000))
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_inbox SET payload=? WHERE message_id=?', (dump(payload), old))
    current = capture(bot, '回复旧消息', root_id=old, parent_id='om_missing')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='仅核对现有内容。')])
    bot.run_one()
    task = task_for(bot, current)
    assert task['input']['conversation']['mode'] == 'topic' and task['state'] == 'partial'
    assert 'BEFORE_ACTIVATION' not in dump(calls)
    assert '未在当前群设置生效后收录' in task['result']['text']


def test_same_reference_is_separate_for_group_and_binding_revision(bot):
    root = capture(bot, '话题原文', thread_id='omt_scope')
    bot.collect_messages()
    binding = bot.binding(bot.test_connector['id'])
    payload = json.loads(bot.db.one('SELECT payload FROM feishu_inbox WHERE message_id=?', (root,))['payload'])
    index = FeishuTopics(bot.db)
    original = task_for(bot, root)['conversation_key']
    other_group = index.capture({**binding, 'chat_id': 'oc_other'}, {**payload, 'chat_id': 'oc_other'})
    next_revision = index.capture({**binding, 'revision': binding['revision'] + 1}, payload)
    assert len({original, other_group['key'], next_revision['key']}) == 3
    assert bot.collect_messages() == 0
    assert bot.db.one('SELECT COUNT(*) n FROM feishu_topic_messages')['n'] == 3


def test_reply_to_bot_answer_recovers_only_original_question(bot, monkeypatch):
    root = capture(bot, 'BOT_ROOT 原问题约定周五')
    bot.collect_messages()
    arrange_model(bot, monkeypatch, [response(text='BOT_ANSWER 约定已记录。')]); bot.run_one(); bot.deliver_one()
    assert bot.test_client.sent[0]['reply_to'] == root and not bot.test_client.sent[0]['in_thread']
    private = capture(bot, 'LATER_PRIVATE 其他无话题内容')
    bot.collect_messages(); complete(bot, private)
    current = capture(bot, '接着机器人的这条答复', sender='ou_owner', parent_id='om_sent')
    bot.collect_messages()
    calls = arrange_model(bot, monkeypatch, [response(text='按原话题继续。')]); bot.run_one(); bot.deliver_one()
    assert 'BOT_ROOT' in dump(calls) and 'BOT_ANSWER' in dump(calls) and 'LATER_PRIVATE' not in dump(calls)
    assert bot.test_client.sent[-1]['reply_to'] == current and bot.test_client.sent[-1]['in_thread']


def test_fast_queued_other_member_clarification_gets_only_same_topic_images(bot, case, monkeypatch):
    root = capture(bot, images=1, mention=False)
    child = capture(bot, '我来补充，这张属于 ' + case['short_code'], sender='ou_owner', root_id=root)
    bot.collect_messages()
    assert task_for(bot, child)['input']['photo_ids'] == []  # First image task has not finished yet.
    arrange_model(bot, monkeypatch, [response(('read_images', {'photo_refs': ['P001']})),
        response(('organize_image', organized_args(case_ref='', certainty='uncertain', photo_group='unknown', role='unknown'))),
        response(text='请在这个话题补充完整案件号。')])
    bot.run_one()
    first = task_for(bot, root)
    assert first['images'][0]['state'] == 'needs_check'
    calls = arrange_model(bot, monkeypatch, [response(('read_images', {'photo_refs': ['P001']})), response(text='已看到同话题的待核对照片。')])
    bot.run_one()
    followed = task_for(bot, child)
    assert followed['input']['photo_ids'] == first['input']['photo_ids']
    assert followed['input']['photo_source_message_id'] == root
    assert followed['steps'][0]['tool'] == 'read_images'
    other = capture(bot, '另一个话题的图片', sender='ou_owner', thread_id='omt_other_images')
    bot.collect_messages()
    arrange_model(bot, monkeypatch, [response(text='这个话题暂无图片。')]); bot.run_one()
    assert not task_for(bot, other)['input']['photo_ids']


@pytest.mark.parametrize('destination', ['oc_expected', 'oc_foreign', 'deleted', 'missing'])
def test_reply_http_verifies_destination_and_preserves_uuid(destination):
    sent = []
    def handle(request):
        if request.url.path.endswith('/internal'):
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'test-only', 'expire': 7200})
        if request.method == 'GET':
            items = [] if destination == 'missing' else [{'message_id': 'om_original', 'chat_id': 'oc_expected' if destination == 'deleted' else destination, 'deleted': destination == 'deleted'}]
            return httpx.Response(200, json={'code': 0, 'data': {'items': items}})
        assert request.url.path.endswith('/messages/om_original/reply')
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'code': 0, 'data': {'message_id': 'om_reply', 'root_id': 'om_original', 'thread_id': 'omt_reply'}})
    client = FeishuFeedbackClient('cli_test', 'test-only', transport=httpx.MockTransport(handle))
    try:
        if destination == 'oc_expected':
            assert client.reply_feedback('oc_expected', 'om_original', '{"text":"test"}', 'stable-id', in_thread=True)['message_id'] == 'om_reply'
            assert sent == [{'msg_type': 'text', 'content': '{"text":"test"}', 'uuid': 'stable-id', 'reply_in_thread': True}]
        else:
            with pytest.raises(DomainError, match='原消息已移除或不在当前群'):
                client.reply_feedback('oc_expected', 'om_original', '{"text":"test"}', 'stable-id')
            assert sent == []
    finally:
        client.close()


def test_reply_http_timeout_is_unknown_without_automatic_resend():
    posts = []
    def handle(request):
        if request.url.path.endswith('/internal'):
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'test-only', 'expire': 7200})
        if request.method == 'GET':
            return httpx.Response(200, json={'code': 0, 'data': {'items': [{'message_id': 'om_original', 'chat_id': 'oc_expected'}]}})
        posts.append(request)
        raise httpx.ReadTimeout('test timeout', request=request)
    client = FeishuFeedbackClient('cli_test', 'test-only', transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(UncertainDelivery):
            client.reply_feedback('oc_expected', 'om_original', '{"text":"test"}', 'stable-id', in_thread=True)
        assert len(posts) == 1
    finally:
        client.close()
