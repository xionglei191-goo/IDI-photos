import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from idi.context_memory import HISTORY_BUDGET, SUMMARY_INPUT_BUDGET, identity, token_estimate, bounded_tool_result
from idi.db import dump, uid
from idi.domain import DomainError
from idi.project_agent_tools import ProjectAgentTools
from idi.workspace_chat import owner_hash
from tests.test_feishu_agent import bot, capture, task_for, response
from tests.test_workspace_chat import chat, send, model, KEY, OTHER


def manual(memory, project, **extra):
    return memory.create(owner_hash(KEY), {'scope': 'project', 'project_id': project['id'], 'kind': 'note',
        'title': '资料约定', 'content': '每周五统一核对已归档资料。', 'request_id': uid(), **extra})


def current_task(chat, result):
    row = chat.db.one('SELECT * FROM workspace_chat_turns WHERE id=?', (result['turn_id'],))
    thread = chat.db.one('SELECT * FROM workspace_chat_threads WHERE id=?', (result['thread_id'],))
    return {**row, 'project_ids': thread['project_ids'], 'context_case_id': thread['case_id'], 'project_id': thread['project_id'],
            'kind': 'chat', 'input': {'text': row['question']}, 'source_message_id': '', 'chat_id': ''}


def seed_history(chat, project, count=10, prefix='记住案件 ZLBH202600012345，每周五核对原件。', long=False):
    thread_id, turns = None, []
    for index in range(count):
        question = prefix if index == 0 else f'第{index + 1}轮补充：请继续核对该案资料。'
        if long:
            question = (question + '这是需要核对的具体历史资料。' * 200)[:5900]
        result = send(chat, question, **({'thread_id': thread_id} if thread_id else {'project_id': project['id']}))
        thread_id = result['thread_id']
        with chat.db.tx() as con:
            con.execute("UPDATE workspace_chat_turns SET state='completed',answer=?,finished_at=created_at WHERE id=?", ('已记录为待核验事项，业务阶段需要重新查询。', result['turn_id']))
        turns.append(result['turn_id'])
    pending = send(chat, '我最开始提出的案件和核对时间是什么？', thread_id=thread_id)
    return pending, turns


def summarizer(calls, invalid=None):
    def request(messages, definitions, timeout):
        calls.append(deepcopy(messages))
        names = {t['function']['name'] for t in definitions}
        if 'finish_context_summary' not in names:
            return response(text='请按最初约定在周五核对 ZLBH202600012345 的原件，当前阶段需重新查询。')
        if invalid:
            return invalid()
        body = json.loads(messages[-1]['content'])
        previous = body['previous_summary']
        focus = previous.get('focus') or [{'text': body['turns'][0]['user'][:220], 'sources': [body['turns'][0]['source']]}]
        return response(('finish_context_summary', {'focus': focus, 'agreements': previous.get('agreements', []), 'pending': previous.get('pending', [])}))
    return request


def test_manual_memory_scope_and_private_owner_isolation(chat, services, project, case):
    memory = chat.memory
    private = manual(memory, project, scope='browser', project_id=None, kind='preference', content='回答先写案件编号，再写结论。')
    shared = manual(memory, project)
    other_project = services['domain'].save_project({'name': '隔离项目'})
    hidden = manual(memory, other_project, title='不应带入', content='隔离项目的独立约定。')
    result = send(chat, project_id=project['id'])
    task = current_task(chat, result)
    values = memory.for_prompt(task, 'workspace', [project['id']], '资料约定')
    assert {r['id'] for r in values['items']} == {private['id'], shared['id']}
    assert hidden['id'] not in dump(values)
    other_values = memory.list(owner_hash(OTHER), project_id=project['id'])
    assert {r['id'] for r in other_values['items']} == {shared['id']}
    with pytest.raises(DomainError, match='当前范围'):
        memory.update(owner_hash(OTHER), private['id'], {'revision': 1, 'action': 'delete'})


def test_case_scope_and_frozen_thread_exclude_unrelated_memories(chat, services, project, case):
    second_case = services['domain'].save_case({'project_id': project['id'], 'title': '另一宗案件'})
    mine = manual(chat.memory, project, scope='case', case_id=case['id'], title='当前案件待办')
    other = manual(chat.memory, project, scope='case', case_id=second_case['id'], title='另一案件待办')
    result = send(chat, case_id=case['id'])
    task = current_task(chat, result)
    items = chat.memory.for_prompt(task, 'workspace', [project['id']], case_id=case['id'], explicit=True)['items']
    assert {r['id'] for r in items} == {mine['id']}
    other_project = services['domain'].save_project({'name': '后建项目'})
    manual(chat.memory, other_project, title='新项目不混入')
    assert {r['id'] for r in chat.memory.list(owner_hash(KEY), thread_id=result['thread_id'])['items']} == {mine['id']}
    with pytest.raises(DomainError, match='当前范围'):
        chat.memory.update(owner_hash(KEY), other['id'], {'revision': 1, 'action': 'delete'}, thread_id=result['thread_id'])


def test_proposed_memory_needs_confirmation_and_exact_source_quote(chat, project, case, monkeypatch):
    quote = '以后请先列出案件号，再给我三条进展。'
    result = send(chat, '请记住：' + quote, case_id=case['id'])
    requests = model(chat, monkeypatch, [response(('propose_memory', {'scope': 'personal', 'kind': 'preference', 'title': '进展表达顺序', 'quote': quote})),
        response(text='已加入待确认记忆，请在 AI 记忆里核对后启用。')])
    chat.run_one()
    record = chat.memory.list(owner_hash(KEY))['items'][0]
    assert record['state'] == 'candidate' and record['content'] == quote and record['source_quote'] == quote
    task = current_task(chat, result)
    assert not chat.memory.for_prompt(task, 'workspace', [project['id']])['items']
    approved = chat.memory.update(owner_hash(KEY), record['id'], {'revision': record['revision'], 'action': 'confirm'})
    assert approved['state'] == 'active'
    assert chat.memory.for_prompt(task, 'workspace', [project['id']])['items'][0]['content'] == quote
    with pytest.raises(DomainError, match='原话'):
        chat.memory.propose(task, 'workspace', [project['id']], {'scope': 'personal', 'kind': 'preference', 'title': '伪造', 'quote': '原文没有提出的内容'})
    assert not chat.db.all('SELECT * FROM platform_drafts')


def test_proposal_rejects_scope_confusion_and_unsupported_fact(chat, project, case):
    result = send(chat, '记住每周五检查本案材料', case_id=case['id'])
    task = current_task(chat, result)
    args = {'scope': 'project', 'kind': 'note', 'title': '检查材料', 'quote': '每周五检查本案材料'}
    with pytest.raises(DomainError, match='案件范围'):
        chat.memory.propose(task, 'workspace', [project['id']], args, project['id'], case['id'])
    with pytest.raises(DomainError, match='不能确认'):
        chat.memory.propose(task, 'workspace', [project['id']], {**args, 'kind': 'fact'}, project['id'])


def test_model_cannot_enable_proposed_memory_via_extra_arguments(chat, project, monkeypatch):
    result = send(chat, '记住以后给我简短回答')
    model(chat, monkeypatch, [response(('propose_memory', {'scope': 'personal', 'kind': 'preference', 'title': '简短', 'quote': '以后给我简短回答', 'state': 'active'})), response(text='需要先核对记忆。')])
    chat.run_one()
    assert not chat.memory.list(owner_hash(KEY))['items']
    assert not chat.get(KEY, result['thread_id'])['turns'][0]['steps'][0]['ok']


def test_snapshot_memory_invalidates_after_source_change_and_does_not_resurrect(chat, case):
    before = deepcopy(chat.domain.case(case['id']))
    chat.memory.observe_case(before)
    row = chat.memory.list(owner_hash(KEY))['items'][0]
    assert row['kind'] == 'fact' and row['state'] == 'active'
    with chat.db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    stale = chat.memory.list(owner_hash(KEY))['items'][0]
    assert stale['state'] == 'stale' and stale['stale_reason']
    chat.memory.observe_case(chat.domain.case(case['id']))
    refreshed = chat.memory.list(owner_hash(KEY))['items'][0]
    assert refreshed['state'] == 'active' and '查勘' in refreshed['content']
    chat.memory.update(owner_hash(KEY), refreshed['id'], {'revision': refreshed['revision'], 'action': 'delete'})
    chat.memory.observe_case(chat.domain.case(case['id']))
    assert not chat.memory.list(owner_hash(KEY))['items']
    tombstone = chat.db.one('SELECT * FROM agent_memories WHERE id=?', (row['id'],))
    assert tombstone['state'] == 'deleted' and tombstone['content'] == ''
    assert chat.db.one('SELECT COUNT(*) n FROM agent_memories')['n'] == 1


def test_correction_preserves_origin_and_cannot_change_case_record(chat, case):
    chat.memory.observe_case(chat.domain.case(case['id']))
    record = chat.memory.list(owner_hash(KEY))['items'][0]
    before = chat.db.one('SELECT * FROM cases WHERE id=?', (case['id'],))
    corrected = chat.memory.update(owner_hash(KEY), record['id'], {'action': 'edit', 'revision': record['revision'],
        'title': '现场复核备注', 'kind': 'note', 'content': '目前需核对阶段，不能认定已经验收。', 'expires_days': 7})
    assert corrected['source_kind'] == 'manual' and corrected['origin']['kind'] == 'case_snapshot'
    assert corrected['kind'] == 'note'
    chat.memory.observe_case(chat.domain.case(case['id']))
    assert chat.memory.list(owner_hash(KEY))['items'][0]['content'] == corrected['content']
    assert chat.db.one('SELECT * FROM cases WHERE id=?', (case['id'],)) == before


def test_expired_memory_and_changed_project_stop_recall(chat, project):
    record = manual(chat.memory, project)
    with chat.db.tx() as con:
        con.execute('UPDATE agent_memories SET expires_at=? WHERE id=?', ('2000-01-01T00:00:00+00:00', record['id']))
    assert chat.memory.list(owner_hash(KEY))['items'][0]['state'] == 'stale'
    current = chat.memory.list(owner_hash(KEY))['items'][0]
    chat.memory.update(owner_hash(KEY), record['id'], {'revision': current['revision'], 'action': 'confirm', 'expires_days': 30})
    with chat.db.tx() as con:
        con.execute('UPDATE projects SET name=? WHERE id=?', ('项目资料已更正', project['id']))
    assert chat.memory.list(owner_hash(KEY))['items'][0]['state'] == 'stale'


def test_memory_create_deduplication_revision_and_delete_guards(chat, project):
    request = uid()
    record = manual(chat.memory, project, request_id=request)
    assert manual(chat.memory, project, request_id=request)['id'] == record['id']
    with pytest.raises(DomainError, match='另一条记忆'):
        manual(chat.memory, project, request_id=request, content='不同内容')
    changed = chat.memory.update(owner_hash(KEY), record['id'], {'revision': record['revision'], 'action': 'edit', 'kind': 'note', 'title': '已改时间', 'content': '改为每周三核对。'})
    with pytest.raises(DomainError, match='其他操作'):
        chat.memory.update(owner_hash(KEY), record['id'], {'revision': record['revision'], 'action': 'delete'})
    assert chat.memory.list(owner_hash(KEY))['items'][0]['content'] == changed['content']


def test_mutated_memory_cancels_stale_inflight_answer(chat, project, monkeypatch):
    record = manual(chat.memory, project)
    result = send(chat, '概括本项目资料约定', project_id=project['id'])
    def reply(*args):
        chat.memory.update(owner_hash(KEY), record['id'], {'revision': record['revision'], 'action': 'delete'})
        return response(text='过期答复不得保存')
    monkeypatch.setattr(chat, 'request_model', reply)
    chat.run_one()
    turn = chat.get(KEY, result['thread_id'])['turns'][0]
    assert turn['state'] == 'failed' and not turn['answer'] and '记忆' in turn['error']


def test_feishu_preferences_stay_with_original_group_member(bot, project):
    message_id = capture(bot, '记住以后先写编号再写进展')
    bot.collect_messages()
    task = task_for(bot, message_id)
    candidate = bot.memory.propose(task, 'feishu', [project['id']], {'scope': 'personal', 'kind': 'preference', 'title': '进展格式', 'quote': '以后先写编号再写进展'})
    record = bot.memory.list(owner_hash(KEY))['items'][0]
    bot.memory.update(owner_hash(KEY), record['id'], {'revision': record['revision'], 'action': 'confirm'})
    assert bot.memory.for_prompt(task, 'feishu', [project['id']])['items'][0]['id'] == candidate['memory_id']
    assert not bot.memory.for_prompt({**task, 'sender_id': 'ou_somebodyelse'}, 'feishu', [project['id']])['items']
    assert not bot.memory.for_prompt({**task, 'connector_id': 'different_group'}, 'feishu', [project['id']])['items']
    bot.save_binding(bot.test_connector['id'], {'enabled': True, 'images_enabled': False})
    assert bot.memory.list(owner_hash(KEY))['items'][0]['state'] == 'stale'


def test_shared_project_notes_are_retrievable_in_authorized_feishu_group(bot, project):
    record = manual(bot.memory, project)
    mid = capture(bot, '本项目的资料约定是什么？')
    bot.collect_messages()
    task = task_for(bot, mid)
    assert bot.memory.for_prompt(task, 'feishu', [project['id']], '资料约定')['items'][0]['id'] == record['id']
    assert not bot.memory.for_prompt(task, 'feishu', [], '资料约定')['items']


def test_long_history_rolls_in_bounded_batches_and_keeps_early_case(chat, project, monkeypatch):
    pending, originals = seed_history(chat, project, 52)
    calls = []
    monkeypatch.setattr(chat, 'request_model', summarizer(calls))
    assert chat.run_one()
    summary = chat.memory.list(owner_hash(KEY), thread_id=pending['thread_id'])['summary']
    assert summary['source_count'] == 46
    assert 'ZLBH202600012345' in dump(summary['sections']) and '周五' in dump(summary['sections'])
    assert summary['source_turns'][0]['source'] == 'W:' + originals[0]
    assert len(calls) == 5  # four summary batches and the actual answer
    assert all(token_estimate(m) < SUMMARY_INPUT_BUDGET + 1000 for m in calls[:-1])
    assert 'ZLBH202600012345' in dump(calls[-1])
    assert chat.get(KEY, pending['thread_id'])['turns'][-1]['state'] == 'completed'


def test_token_budget_summarizes_large_recent_turns_instead_of_slicing(chat, project, monkeypatch):
    pending, _ = seed_history(chat, project, 8, long=True)
    calls = []
    monkeypatch.setattr(chat, 'request_model', summarizer(calls))
    chat.run_one()
    result = chat.get(KEY, pending['thread_id'])['turns'][-1]
    assert result['state'] == 'completed'
    assert result['usage']['context']['summarized_turns'] >= 6
    assert result['usage']['context']['omitted_turns'] == 0
    assert result['usage']['context']['estimated_history_tokens'] < HISTORY_BUDGET + 2800


@pytest.mark.parametrize('failure', ['length', 'json', 'source', 'empty'])
def test_invalid_summary_preserves_cursor_and_original_history(chat, project, monkeypatch, failure):
    pending, originals = seed_history(chat, project, 10)
    def invalid():
        result = response(('finish_context_summary', {'focus': [{'text': '伪造摘要', 'sources': ['W:another-owner']}], 'agreements': [], 'pending': []}))
        if failure == 'length': result['choices'][0]['finish_reason'] = 'length'
        elif failure == 'json': result['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{broken'
        elif failure == 'empty': result = response(('finish_context_summary', {'focus': [], 'agreements': [], 'pending': []}))
        return result
    calls = []
    monkeypatch.setattr(chat, 'request_model', summarizer(calls, invalid))
    chat.run_one()
    row = chat.db.one('SELECT * FROM agent_context_summaries WHERE thread_id=?', (pending['thread_id'],))
    assert row['cursor_seq'] == 0 and row['source_count'] == 0 and row['summary_json'] == '{}' and row['last_error']
    assert chat.db.one('SELECT question FROM workspace_chat_turns WHERE id=?', (originals[0],))['question'].startswith('记住案件')
    assert chat.get(KEY, pending['thread_id'])['turns'][-1]['state'] == 'completed'


def test_summary_never_includes_other_browser_or_thread(chat, project, monkeypatch):
    foreign, _ = seed_history(chat, project, 8, prefix='这个独立话题 SECRET_THREAD_ABC 不应泄漏。')
    with chat.db.tx() as con:
        con.execute("UPDATE workspace_chat_turns SET state='cancelled' WHERE id=?", (foreign['turn_id'],))
    pending, _ = seed_history(chat, project, 9)
    calls = []
    monkeypatch.setattr(chat, 'request_model', summarizer(calls))
    chat.run_one()
    assert 'SECRET_THREAD_ABC' not in dump(calls)
    with pytest.raises(DomainError, match='不属于'):
        chat.memory.list(owner_hash(OTHER), thread_id=pending['thread_id'])


def test_summary_correction_clear_and_revision_are_effective(chat, project, monkeypatch):
    pending, _ = seed_history(chat, project, 10)
    calls = []
    monkeypatch.setattr(chat, 'request_model', summarizer(calls))
    chat.run_one()
    summary = chat.memory.list(owner_hash(KEY), thread_id=pending['thread_id'])['summary']
    changed = chat.memory.edit_summary(owner_hash(KEY), pending['thread_id'], {'revision': summary['revision'], 'text': '已更正：原件核对日改为周三。'})
    with pytest.raises(DomainError, match='其他操作'):
        chat.memory.edit_summary(owner_hash(KEY), pending['thread_id'], {'revision': summary['revision'], 'text': '过期修改不应生效'})
    chat.memory.edit_summary(owner_hash(KEY), pending['thread_id'], {'revision': changed['revision'], 'clear': True})
    send(chat, '开始新的讨论', thread_id=pending['thread_id'])
    calls.clear()
    chat.run_one()
    assert len(calls) == 1 and 'ZLBH202600012345' not in dump(calls[0])
    assert chat.memory.list(owner_hash(KEY), thread_id=pending['thread_id'])['summary']['sections'] == {}


def test_summary_edit_stops_inflight_answer_using_old_summary(chat, project, monkeypatch):
    pending, _ = seed_history(chat, project, 3)
    def reply(*args):
        summary = chat.memory.list(owner_hash(KEY), thread_id=pending['thread_id'])['summary']
        chat.memory.edit_summary(owner_hash(KEY), pending['thread_id'], {'revision': summary['revision'], 'text': '由用户刚刚纠正的摘要。'})
        return response(text='旧摘要下的迟到答复')
    monkeypatch.setattr(chat, 'request_model', reply)
    chat.run_one()
    turn = chat.get(KEY, pending['thread_id'])['turns'][-1]
    assert turn['state'] == 'failed' and not turn['answer'] and '摘要' in turn['error']


def test_feishu_rolling_context_preserves_sender_binding_scope(bot, project, monkeypatch):
    first_ids = []
    for index in range(8):
        mid = capture(bot, '最初约定周五核对本案材料' if not index else f'跟进问题{index}')
        bot.collect_messages()
        item = task_for(bot, mid)
        with bot.db.tx() as con:
            con.execute("UPDATE feishu_agent_tasks SET state='completed',result_json=? WHERE id=?", (dump({'text': '请核对已有资料'}), item['id']))
        first_ids.append(item['id'])
    other = capture(bot, 'SECRET_OTHER_MEMBER 不应进入上下文', sender='ou_owner')
    bot.collect_messages()
    other_task = task_for(bot, other)
    with bot.db.tx() as con:
        con.execute("UPDATE feishu_agent_tasks SET state='completed',result_json=? WHERE id=?", (dump({'text': '另一成员的答复'}), other_task['id']))
    last = capture(bot, '最初核对日期是什么？')
    bot.collect_messages()
    task = task_for(bot, last)
    calls = []
    monkeypatch.setattr(bot, 'request_model', summarizer(calls))
    bot.run_one()
    assert 'SECRET_OTHER_MEMBER' not in dump(calls)
    summary = bot.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=?', (identity(task, 'feishu')[0],))
    assert summary['source_count'] == 4 and '周五' in summary['summary_json']
    assert identity({**task, 'binding_revision': task['binding_revision'] + 1}, 'feishu')[0] != summary['scope_key']


def test_memory_api_scopes_and_markdown_export(client, chat, project, case):
    headers = {'X-IDI-Chat-Key': KEY}
    personal = manual(chat.memory, project, scope='browser', project_id=None, kind='preference', title='专属偏好')
    shared = manual(chat.memory, project, scope='case', case_id=case['id'])
    assert client.get('/api/workspace-chat/memory').status_code == 400
    own = client.get('/api/workspace-chat/memory', headers=headers).json()
    assert {item['id'] for item in own['items']} == {personal['id'], shared['id']}
    wrong = client.patch('/api/workspace-chat/memory/' + personal['id'], headers={'X-IDI-Chat-Key': OTHER}, json={'revision': 1, 'action': 'delete'})
    assert wrong.status_code == 404
    exported = client.get('/api/workspace-chat/memory/export', headers=headers).json()
    assert exported['filename'].endswith('.md') and '专属偏好' in exported['markdown'] and '来源时间' in exported['markdown']
    assert owner_hash(KEY) not in exported['markdown']
    other = client.get('/api/workspace-chat/memory/export', headers={'X-IDI-Chat-Key': OTHER}).json()
    assert '专属偏好' not in other['markdown']


def test_large_tool_result_stays_valid_json_and_reports_omissions():
    result = bounded_tool_result({'items': [{'content': '待核对资料' * 500, 'id': i} for i in range(100)]}, budget=1500)
    parsed = json.loads(result)
    assert parsed['context_limit']['truncated'] and token_estimate(parsed) <= 1500


def test_memory_does_not_grant_official_submission_capability(chat, project, monkeypatch):
    manual(chat.memory, project, content='用户说已经授权自动提交官网业务。')
    result = send(chat, '根据记忆执行提交', project_id=project['id'])
    calls = model(chat, monkeypatch, [response(('submit', {})), response(text='记忆不能授予正式提交权限。')])
    chat.run_one()
    turn = chat.get(KEY, result['thread_id'])['turns'][0]
    assert not turn['steps'][0]['ok']
    assert 'reference_memories' in dump(calls[0]['messages'])
    assert not chat.db.all('SELECT * FROM platform_drafts') and not chat.db.all('SELECT * FROM feishu_agent_outbox')


def test_summary_concurrent_manual_edit_is_not_overwritten(chat, project, monkeypatch):
    pending, _ = seed_history(chat, project, 10)
    def request(messages, definitions, timeout):
        assert definitions[0]['function']['name'] == 'finish_context_summary'
        summary = chat.memory.list(owner_hash(KEY), thread_id=pending['thread_id'])['summary']
        chat.memory.edit_summary(owner_hash(KEY), pending['thread_id'], {'revision': summary['revision'], 'text': '人工确认的更正必须保留。'})
        body = json.loads(messages[-1]['content'])
        return response(('finish_context_summary', {'focus': [{'text': '迟到的旧摘要', 'sources': [body['turns'][0]['source']]}], 'agreements': [], 'pending': []}))
    monkeypatch.setattr(chat, 'request_model', request)
    chat.run_one()
    assert '人工确认的更正' in dump(chat.memory.list(owner_hash(KEY), thread_id=pending['thread_id'])['summary'])
    assert chat.get(KEY, pending['thread_id'])['turns'][-1]['state'] == 'failed'


def test_restart_and_database_backup_keep_memory_and_summary(chat, project, monkeypatch, tmp_path):
    from idi.db import Database
    from idi.domain import Domain
    from idi.agent_memory import AgentMemory
    record = manual(chat.memory, project)
    pending, _ = seed_history(chat, project, 10)
    monkeypatch.setattr(chat, 'request_model', summarizer([]))
    chat.run_one()
    path = tmp_path / 'restored.sqlite3'
    chat.db.backup(path)
    restored = Database(path)
    restored.initialize()
    memory = AgentMemory(restored, Domain(restored))
    snapshot = memory.list(owner_hash(KEY), thread_id=pending['thread_id'])
    assert snapshot['items'][0]['id'] == record['id']
    assert snapshot['summary']['source_count'] == 4
    assert restored.one('PRAGMA integrity_check')['integrity_check'] == 'ok'


def test_large_group_backlog_keeps_actual_latest_turns_and_reports_gap(bot, project, monkeypatch):
    binding = bot.binding(bot.test_connector['id'])
    with bot.db.tx() as con:
        for index in range(710):
            task_id = bot._insert_task(con, binding, 'chat', 'history:' + uid(),
                {'text': 'ACTUAL_LATEST_CONTEXT 请保留最近决定' if index == 709 else f'历史核对事项第{index}轮'}, sender='ou_member')
            con.execute("UPDATE feishu_agent_tasks SET state='completed',result_json=? WHERE id=?", (dump({'text': '已记录为历史对话'}), task_id))
    mid = capture(bot, '最新一轮说了什么？')
    bot.collect_messages()
    task = task_for(bot, mid)
    calls = []
    monkeypatch.setattr(bot, 'request_model', summarizer(calls))
    bot.run_one()
    result = bot.get(task['id'])
    assert result['state'] == 'partial'
    assert result['usage']['context']['omitted_turns'] > 0
    assert 'ACTUAL_LATEST_CONTEXT' in dump(calls[-1])
    assert '尚未带入' in result['result']['text']
    summary = bot.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=?', (identity(task, 'feishu')[0],))
    assert summary['source_count'] <= 96


def seed_group_summary(bot, monkeypatch):
    binding = bot.binding(bot.test_connector['id'])
    with bot.db.tx() as con:
        for index in range(8):
            task_id = bot._insert_task(con, binding, 'chat', 'summary-history:' + uid(),
                {'text': 'GROUP_ORIGINAL 周五核对现场原件' if index == 0 else f'补充资料第{index}轮'}, sender='ou_member')
            con.execute("UPDATE feishu_agent_tasks SET state='completed',result_json=? WHERE id=?", (dump({'text': '已记录为待核对事项'}), task_id))
        foreign = bot._insert_task(con, binding, 'chat', 'foreign-history:' + uid(), {'text': 'OTHER_MEMBER_SECRET'}, sender='ou_owner')
        con.execute("UPDATE feishu_agent_tasks SET state='completed',result_json=? WHERE id=?", (dump({'text': '另一成员独立内容'}), foreign))
    mid = capture(bot, '现场材料最初何时核对？')
    bot.collect_messages()
    task = task_for(bot, mid)
    monkeypatch.setattr(bot, 'request_model', summarizer([]))
    bot.run_one()
    return identity(task, 'feishu')[0], foreign


def test_group_summary_management_requires_full_scope_and_current_binding(bot, services, project, case, monkeypatch):
    key, foreign = seed_group_summary(bot, monkeypatch)
    other = services['domain'].save_project({'name': '另一个独立项目'})
    memory, owner = bot.memory, owner_hash(KEY)
    options = memory.list(owner, project_id=project['id'])['summary_options']
    assert options == [{'key': key, 'label': '项目群 · 现场材料最初何时核对？', 'channel': 'feishu'}]
    summary = memory.managed_summary(owner, key, project_id=project['id'])
    assert summary['channel'] == 'feishu' and summary['source_turns']
    assert 'GROUP_ORIGINAL' in dump(summary) and 'OTHER_MEMBER_SECRET' not in dump(summary)
    for scope in ({'project_id': other['id']}, {'case_id': case['id']}):
        assert not memory.list(owner, **scope)['summary_options']
        with pytest.raises(DomainError, match='不在当前范围'):
            memory.managed_summary(owner, key, **scope)
    # Even a corrupted source reference cannot expose a different member's text.
    row = bot.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=?', (key,))
    row['summary_json'] = dump({'focus': [{'text': '损坏的来源引用', 'sources': ['F:' + foreign]}]})
    assert memory.context.public(row)['source_turns'] == []
    bot.save_binding(bot.test_connector['id'], {'enabled': True, 'images_enabled': False})
    assert not memory.list(owner)['summary_options']
    with pytest.raises(DomainError, match='不在当前范围'):
        memory.managed_summary(owner, key)


def test_group_summary_api_edit_clear_and_export_keep_original_messages(bot, client, project, monkeypatch):
    key, _ = seed_group_summary(bot, monkeypatch)
    headers = {'X-IDI-Chat-Key': KEY}
    url = '/api/workspace-chat/memory/summaries/' + key + '?project_id=' + project['id']
    before = bot.db.all('SELECT * FROM feishu_agent_tasks ORDER BY rowid')
    summary = client.get(url, headers=headers).json()
    assert summary['source_count'] == 4
    changed = client.patch(url, headers=headers, json={'revision': summary['revision'], 'text': '群内已核对，更正为周三检查原件。'})
    assert changed.status_code == 200 and '周三' in dump(changed.json())
    exported = client.get('/api/workspace-chat/memory/export?project_id=' + project['id'], headers=headers).json()
    assert '项目群' in exported['markdown'] and '周三' in exported['markdown']
    assert client.patch(url, headers=headers, json={'revision': summary['revision'], 'clear': True}).status_code == 409
    cleared = client.patch(url, headers=headers, json={'revision': changed.json()['revision'], 'clear': True})
    assert cleared.status_code == 200 and cleared.json()['sections'] == {}
    assert bot.db.all('SELECT * FROM feishu_agent_tasks ORDER BY rowid') == before
    calls = []
    monkeypatch.setattr(bot, 'request_model', summarizer(calls))
    capture(bot, '开始新一段现场核对')
    bot.collect_messages()
    bot.run_one()
    assert len(calls) == 1 and 'GROUP_ORIGINAL' not in dump(calls[0]) and '周三' not in dump(calls[0])
    assert not bot.test_client.sent


def test_managed_summary_api_keeps_web_owner_boundary(client, chat, project, monkeypatch):
    pending, _ = seed_history(chat, project, 10)
    monkeypatch.setattr(chat, 'request_model', summarizer([]))
    chat.run_one()
    url = '/api/workspace-chat/memory/summaries/workspace:' + pending['thread_id'] + '?thread_id=' + pending['thread_id']
    own = client.get(url, headers={'X-IDI-Chat-Key': KEY})
    assert own.status_code == 200 and own.json()['source_turns']
    assert client.get(url, headers={'X-IDI-Chat-Key': OTHER}).status_code == 404
    assert client.patch(url, headers={'X-IDI-Chat-Key': OTHER}, json={'revision': own.json()['revision'], 'clear': True}).status_code == 404


def test_summary_batches_stop_at_the_task_token_limit(chat, project, monkeypatch):
    pending, _ = seed_history(chat, project, 52)
    calls = []
    base = summarizer(calls)
    def large_usage(*args):
        result = base(*args)
        result['usage'] = {'prompt_tokens': 50000, 'completion_tokens': 1}
        return result
    monkeypatch.setattr(chat, 'request_model', large_usage)
    chat.run_one()
    turn = chat.get(KEY, pending['thread_id'])['turns'][-1]
    assert len(calls) == 1 and turn['state'] == 'failed' and not turn['answer'] and '用量上限' in turn['error']
    summary = chat.memory.list(owner_hash(KEY), thread_id=pending['thread_id'])['summary']
    assert summary['source_count'] == 12 and '用量上限' in summary['last_error']
