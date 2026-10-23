"""Progress needs dated evidence; synchronized ledgers are not business activity."""
from datetime import datetime

import pytest

from idi.agent_dates import AgentDates
from idi.db import dump, uid
from idi.domain import DomainError
from tests.conftest import image_bytes
from tests.test_agent_capabilities import context, call
from tests.test_feishu_agent import bot, capture, task_for, response, arrange_model
from tests.test_workspace_chat import chat, send, model, KEY


STAMP = '2026-10-23T07:24:11+00:00'
OLD = '2026-10-22T07:24:11+00:00'
QUESTION = '今天有哪些案件有进展？'


@pytest.fixture(autouse=True)
def server_clock(monkeypatch):
    monkeypatch.setattr('idi.agent_dates.now', lambda: STAMP)


def dated(text=QUESTION):
    return AgentDates({'kind': 'chat', 'created_at': STAMP, 'input': {'text': text}})


def progress_context(chat, case, text=QUESTION):
    ctx = context(chat, case, text, case_context=False)
    ctx.dates = dated(text)
    return ctx


def event(db, case_id, *, source='platform', before='survey', after='repair', at=STAMP, recorded=STAMP):
    with db.tx() as con:
        con.execute('INSERT INTO case_events VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(), case_id, 'test_event', before, after, source, '{}', at, recorded))


def extra_cases(db, project_id, count):
    ids = [uid() for _ in range(count)]
    with db.tx() as con:
        con.executemany('INSERT INTO cases(id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?)',
            [(case_id, project_id, f'历史案件{i}', OLD, STAMP) for i, case_id in enumerate(ids)])
    return ids


def receipt(db, case_id, *, state='verified', action='submit', confirmed=STAMP, readback=STAMP, save_only=False, mismatches=None):
    with db.tx() as con:
        con.execute('INSERT INTO platform_review_batches(id,case_id,stage,state,fingerprint,snapshot,result,created_at,confirmed_at,finished_at,save_only) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (uid(), case_id, 'survey', state, uid(), '{}',
             dump({'action': action, 'readback_at': readback, 'mismatches': mismatches or []}),
             OLD, confirmed, STAMP, int(save_only)))


def test_445_updated_ledgers_only_three_cases_have_progress_evidence(chat, services, project, case):
    db = services['db']
    ids = [case['id'], *extra_cases(db, project['id'], 444)]
    with db.tx() as con:
        con.execute('UPDATE cases SET updated_at=?', (STAMP,))
    for case_id in ids[3:25]:
        event(db, case_id, before=None)  # First synchronization is a baseline.
    event(db, ids[25], before='repair')  # An unchanged readback is not progress.
    event(db, ids[2], source='platform_list', at=OLD)
    for index in range(8):
        photo = services['importer'].add_file(image_bytes((index, 2, 3)), f'{index}.png', case_id=ids[int(index >= 5)])
        with db.tx() as con:
            con.execute('UPDATE photos SET created_at=? WHERE id=?', (STAMP, photo['photo_id']))
    ledger = progress_context(chat, case, '今天台账更新了哪些案件？')
    assert call(ledger, 'search_cases', query='')['total'] == 445
    with db.tx() as con:
        con.execute("UPDATE workspace_chat_turns SET state='completed' WHERE id=?", (ledger.task['id'],))
    ctx = progress_context(chat, case)
    before = {table: db.all(f'SELECT * FROM {table}') for table in ('cases', 'photos', 'case_events', 'platform_review_batches')}
    result = call(ctx, 'read_case_progress', date_period='today')
    assert result['total'] == result['summary']['cases_with_evidence'] == 3
    categories = result['summary']['categories']
    assert categories['photos_archived'] == {'count': 8, 'case_count': 2}
    assert categories['workflow_change_observed'] == {'count': 1, 'case_count': 1}
    changed = next(e for item in result['items'] for e in item['evidence'] if e['kind'] == 'workflow_change_observed')
    assert changed['occurred_at'] is None and changed['time_basis'] == 'observed_at'
    assert changed['evidence_at'] == '2026-10-23T15:24:11+08:00'
    assert result['date_filter']['date_basis'] == 'progress_evidence'
    assert before == {table: db.all(f'SELECT * FROM {table}') for table in before}
    with pytest.raises(DomainError, match='数量超过'):
        call(ctx, 'finish_reply', text='今天有445个案件的台账发生了更新。以下是部分案件进展。')
    answer = call(ctx, 'finish_reply', text='3个案件有记录：2案新增归档8张照片；另1案同步发现查勘转修复，实际办理时间未知。')['text']
    assert '进展事件证据' in answer and '纯台账刷新' in answer


def test_date_boundaries_future_records_and_photo_copies_are_excluded(chat, services, case, photo):
    pictures = [photo(color=(i, 4, 5)) for i in range(6)]
    times = ['2026-10-22T15:59:59.999Z', '2026-10-22T16:00:00.000Z', STAMP,
             '2026-10-23T07:24:12+00:00', '2026-10-23T16:00:00+00:00', STAMP]
    with chat.db.tx() as con:
        con.executemany('UPDATE photos SET created_at=? WHERE id=?', [(at, p['id']) for at, p in zip(times, pictures)])
        con.execute('UPDATE photos SET reuse_source_id=? WHERE id=?', (pictures[1]['id'], pictures[-1]['id']))
    event(chat.db, case['id'], at=STAMP, recorded='2026-10-23T07:24:12+00:00')
    ctx = progress_context(chat, case)
    result = call(ctx, 'read_case_progress')
    counts = result['summary']['categories']
    assert counts['photos_archived']['count'] == 2
    assert counts['workflow_change_observed']['count'] == 0
    assert result['items'][0]['evidence'][0]['first_archived_at'] == '2026-10-23T00:00:00+08:00'


def test_manual_business_dates_and_submission_receipts_do_not_use_refresh_time(chat, case):
    event(chat.db, case['id'], source='manual_record', at=OLD)
    event(chat.db, case['id'], source='manual_record', at='2026-10-23 09:30:00')
    event(chat.db, case['id'], source='manual_record', at='not-a-date')
    with chat.db.tx() as con:
        for at, basis in [('2026-10-22', 'manual'), ('2026-10-23', 'manual'), ('2026-10-23', 'watermark')]:
            con.execute('INSERT INTO visits(id,case_id,stage,visit_date,visit_key,basis,created_at) VALUES(?,?,?,?,?,?,?)',
                (uid(), case['id'], 'work', at, uid(), basis, STAMP))
    receipt(chat.db, case['id'])
    receipt(chat.db, case['id'], confirmed=OLD)  # Old submit rechecked today.
    receipt(chat.db, case['id'], state='save_verified', action='save', save_only=True)
    receipt(chat.db, case['id'], state='verified', action='save')
    receipt(chat.db, case['id'], save_only=True)
    receipt(chat.db, case['id'], state='reviewed')
    receipt(chat.db, case['id'], state='uncertain')
    receipt(chat.db, case['id'], state='submitted', mismatches=['workflow_stage'])
    receipt(chat.db, case['id'], readback=None)
    receipt(chat.db, case['id'], readback='2026-10-23T07:24:12+00:00')
    result = call(progress_context(chat, case), 'read_case_progress')
    assert result['total'] == 1  # All categories concern the same case.
    categories = result['summary']['categories']
    assert categories['handling_record'] == {'count': 1, 'case_count': 1}
    assert categories['manual_visit'] == {'count': 1, 'case_count': 1}
    assert categories['formal_submission_verified'] == {'count': 1, 'case_count': 1}
    assert result['pending_submission_count'] == 4
    assert result['unknown_date_count'] == 1
    assert result['summary']['categories_may_overlap']


def test_progress_is_scoped_paginated_and_rechecks_moved_cases(chat, services, project, case):
    ids = [case['id'], *extra_cases(chat.db, project['id'], 3)]
    other = services['domain'].save_project({'name': '其他项目'})
    private = extra_cases(chat.db, other['id'], 1)[0]
    for case_id in [*ids, private]:
        event(chat.db, case_id)
    ctx = progress_context(chat, case)
    one = call(ctx, 'read_case_progress', page_size=2)
    two = call(ctx, 'read_case_progress', page_size=2, page=2)
    assert one['total'] == two['total'] == 4 and one['has_more'] and not two['has_more']
    assert not {i['case_ref'] for i in one['items']} & {i['case_ref'] for i in two['items']}
    assert private not in dump(one) + dump(two)
    ref = ctx.case_refs[ids[1]]
    assert call(ctx, 'read_case_progress', case_ref=ref)['total'] == 1
    with chat.db.tx() as con:
        con.execute('UPDATE cases SET project_id=? WHERE id=?', (other['id'], ids[1]))
    assert call(ctx, 'read_case_progress')['total'] == 3
    with pytest.raises(DomainError, match='项目已变化'):
        call(ctx, 'read_case_progress', case_ref=ref)
    with pytest.raises(DomainError, match='授权范围'):
        call(ctx, 'read_case_progress', project_ref='J999')
    assert '路径' not in dump(one)


@pytest.mark.parametrize('question', [QUESTION, '请汇总今日案件推进情况', '本周的办理结果有哪些？', '2026年10月23日有哪些案件进展？'])
def test_progress_cannot_finish_from_history_or_ledger_search(question):
    ctx = dated(question)
    assert ctx.public()['progress_evidence_required']
    with pytest.raises(DomainError, match='先成功调用'):
        ctx.finish('今天有445个案件有进展。')
    with pytest.raises(DomainError, match='read_case_progress'):
        ctx.resolve('search_cases', {'query': ''})
    assert ctx.resolve('search_cases', {'query': 'KNOWN-001'})['date_basis'] == 'updated_at'


@pytest.mark.parametrize('question', ['今天照片上传进度怎样？', '今天同步进展如何？', '目前这个案件进展如何？', '他说“今天案件有进展”，这是什么意思？'])
def test_processing_or_undated_questions_are_not_forced_into_daily_progress(question):
    assert not dated(question).progress_requested


def test_progress_explicit_dates_multiple_periods_and_zero_evidence(chat, case):
    ctx = progress_context(chat, case, '查询2026年10月21日至2026年10月22日的案件进展')
    result = call(ctx, 'read_case_progress')
    assert result['date_filter']['date_from'] == '2026-10-21'
    assert result['date_filter']['date_to'] == '2026-10-22'
    assert '未找到记录不代表现场没有实际进展' in call(ctx, 'finish_reply', text='当前记录未查到相应证据。')['text']
    with pytest.raises(DomainError, match='明确日期'):
        call(ctx, 'read_case_progress', date_period='today')
    ctx = dated('今天和昨天各有哪些案件进展？')
    with pytest.raises(DomainError, match='多个日期范围'):
        ctx.resolve('read_case_progress', {})
    for period in ('today', 'yesterday'):
        args = ctx.resolve('read_case_progress', {'date_period': period})
        ctx.record('read_case_progress', args, {'total': 0})
        if period == 'today':
            with pytest.raises(DomainError, match='yesterday'):
                ctx.finish('这两天未查到进展记录。')
    assert '2026-10-22' in ctx.finish('这两天未查到进展记录。')
    ledger = dated('今天哪些案件台账更新了，又有哪些案件有进展？')
    assert ledger.required == {'search_cases', 'read_case_progress'}
    assert ledger.resolve('search_cases', {'query': ''})['date_basis'] == 'updated_at'
    assert dated('今天哪些报案有进展？').required == {'read_case_progress'}


def test_workspace_corrects_stale_445_reply_and_wrong_tool(chat, case, monkeypatch):
    event(chat.db, case['id'])
    first = send(chat, QUESTION, project_id=case['project_id'])
    requests = model(chat, monkeypatch, [response(text='今天有445个案件有进展。'),
        response(('search_cases', {'query': '', 'date_period': 'today'})),
        response(('read_case_progress', {'date_period': 'today'})),
        response(text='今天有445个案件的台账发生了更新。以下是部分案件进展。'),
        response(text='1个案件有进展记录：本次官网同步发现查勘转修复，实际办理时间未知。')])
    assert chat.run_one()
    result = chat.get(KEY, first['thread_id'])['turns'][0]
    assert result['state'] == 'completed' and '445' not in result['answer']
    assert '同步发现' in result['answer'] and '进展事件证据' in result['answer']
    assert [(step['tool'], step['ok']) for step in result['steps']] == [('search_cases', False), ('read_case_progress', True)]
    assert 'read_case_progress' in dump(requests[1]) and '数量超过' in dump(requests[4])
    assert not chat.db.all('SELECT * FROM feishu_agent_outbox')


def test_feishu_progress_uses_same_evidence_guard_without_sending(bot, case, monkeypatch):
    event(bot.db, case['id'])
    mid = capture(bot, QUESTION)
    bot.collect_messages()
    task = task_for(bot, mid)
    task['input']['source_time'] = str(int(datetime.fromisoformat(STAMP).timestamp() * 1000))
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_agent_tasks SET input_json=? WHERE id=?', (dump(task['input']), task['id']))
    arrange_model(bot, monkeypatch, [response(('finish_reply', {'text': '今天有445个案件有进展。'})),
        response(('read_case_progress', {'date_period': 'today'})),
        response(('finish_reply', {'text': '1个案件有记录：今天同步发现环节变化，实际办理时间未知。'}))])
    assert bot.run_one()
    result = task_for(bot, mid)
    assert result['state'] == 'completed' and '445' not in result['result']['text']
    assert '进展事件证据' in result['result']['text'] and bot.test_client.sent == []


def test_project_summary_excludes_initial_imports_and_dates_manual_results_correctly(chat, case):
    event(chat.db, case['id'], before=None)
    event(chat.db, case['id'], before='repair')
    event(chat.db, case['id'], source='manual_record', at=OLD)
    event(chat.db, case['id'], source='platform_list', at=OLD)
    result = chat.project_service.project_snapshot(case['project_id'], instant=datetime.fromisoformat(STAMP))
    assert len(result['recent_recorded_changes']) == 1
    item = result['recent_recorded_changes'][0]
    assert item['time_basis'] == 'observed_at' and '实际办理时间未知' in item['time_note']
