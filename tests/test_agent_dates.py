"""Date regressions use isolated archives and fake model / group clients."""
import json
from datetime import datetime

import pytest

from idi.agent_dates import AgentDates, local_date
from idi.db import dump
from idi.domain import DomainError
from tests.test_agent_capabilities import context, call
from tests.test_feishu_agent import bot, capture, task_for, response, arrange_model
from tests.test_workspace_chat import chat, send, model, KEY


STAMP = '2026-10-19T07:14:03.980+00:00'


def dates(text='今天新增了哪些报案？', at=STAMP, **data):
    return AgentDates({'kind': 'chat', 'created_at': at, 'input': {'text': text, **data}})


def dated_context(chat, case, text='今天新增了哪些报案？'):
    ctx = context(chat, case, text, case_context=False)
    ctx.dates = dates(text)
    return ctx


@pytest.mark.parametrize('text,period,expected', [
    ('今天新增了哪些报案', 'today', ('2026-01-01', '2026-01-01')),
    ('昨天收到哪些照片', 'yesterday', ('2025-12-31', '2025-12-31')),
    ('前天新增了哪些报案', 'day_before_yesterday', ('2025-12-30', '2025-12-30')),
    ('本周新增了哪些报案', 'this_week', ('2025-12-29', '2026-01-04')),
    ('上周新增了哪些报案', 'last_week', ('2025-12-22', '2025-12-28')),
    ('本月新增了哪些报案', 'this_month', ('2026-01-01', '2026-01-31')),
    ('上个月新增了哪些报案', 'last_month', ('2025-12-01', '2025-12-31')),
])
def test_relative_periods_use_beijing_calendar_across_new_year(text, period, expected):
    ctx = dates(text, '2025-12-31T16:01:00+00:00')
    tool = 'search_photos' if '照片' in text else 'search_cases'
    result = ctx.resolve(tool, {'date_period': period})
    assert (result['date_from'], result['date_to']) == expected
    assert ctx.public()['message_date'] == '2026-01-01'


def test_delayed_task_uses_sent_date_and_falls_back_on_invalid_timestamp(monkeypatch):
    monkeypatch.setattr('idi.agent_dates.now', lambda: '2026-10-20T02:00:00+00:00')
    timestamp = str(int(datetime.fromisoformat('2026-10-18T15:59:59+00:00').timestamp() * 1000))
    ctx = dates(source_time=timestamp)
    assert ctx.public()['server_now'].startswith('2026-10-20')
    assert ctx.public()['message_date'] == '2026-10-18'
    assert ctx.public()['anchor_source'] == 'message_sent_at'
    assert ctx.resolve('search_cases', {})['date_to'] == '2026-10-18'
    assert dates(source_time='not-a-time').public()['message_date'] == '2026-10-19'


def test_wrong_year_and_wrong_date_basis_fail_before_a_successful_read(chat, case):
    ctx = dated_context(chat, case)
    with pytest.raises(DomainError, match='2026-10-19'):
        call(ctx, 'search_cases', query='', date_from='2025-01-22', date_to='2025-01-22')
    with pytest.raises(DomainError, match='报案日期'):
        call(ctx, 'search_cases', query='', date_period='today', date_basis='updated_at')
    with pytest.raises(DomainError, match='先成功调用'):
        call(ctx, 'finish_reply', text='今天没有新增报案。')
    assert ctx.dates.reads == []


def test_reports_use_official_report_date_not_sync_date_and_report_missing_dates(chat, services, project, case):
    official = services['domain'].save_case({'project_id': project['id'], 'title': '官网今日报案'})
    old = services['domain'].save_case({'project_id': project['id'], 'title': '今天同步的旧报案', 'report_at': '2026-10-18'})
    services['domain'].save_case({'project_id': project['id'], 'title': '本地今日报案', 'report_at': '2026-10-18T16:00:00+00:00'})
    other = services['domain'].save_project({'name': '其他授权范围'})
    services['domain'].save_case({'project_id': other['id'], 'title': '范围外不可见', 'report_at': '2026-10-19'})
    chat.platform.save_link(official['id'], {'report_receiving_id': 'official-date-test', 'revision': 0}, snapshot={'row': {'reportDate': '2026-10-19 08:30:00'}})
    with chat.db.tx() as con:
        con.execute('UPDATE platform_links SET snapshot=?,verified_at=? WHERE case_id=?',
                    (dump({'row': {'reportDate': '2026-10-19 08:30:00'}}), STAMP, official['id']))
        con.execute('UPDATE cases SET created_at=?,updated_at=?', (STAMP, STAMP))
    ctx = dated_context(chat, case)
    result = call(ctx, 'search_cases', query='')
    assert result['total'] == 2 and result['unknown_date_count'] == 1
    assert {v['title'] for v in result['items']} == {'官网今日报案', '本地今日报案'}
    assert result['date_filter']['date_basis'] == 'report_at'
    assert result['date_filter']['date_from'] == result['date_filter']['date_to'] == '2026-10-19'
    by_title = {v['title']: v for v in result['items']}
    assert by_title['官网今日报案']['date_evidence']['source'] == 'official_snapshot'
    assert by_title['本地今日报案']['date_evidence']['source'] == 'local_ledger'
    assert result['freshness'] == {'live_official_query': False, 'snapshot_verified_at_min': STAMP, 'snapshot_verified_at_max': STAMP}
    reply = call(ctx, 'finish_reply', text='本次查询找到2件报案。')['text']
    assert '2026-10-19' in reply and '1 件日期缺失' in reply and '未实时刷新官网' in reply
    with pytest.raises(DomainError, match='答复中的'):
        call(ctx, 'finish_reply', text='今天（2025年1月22日）没有新增报案。')
    assert old['id'] not in [ctx.case(v['case_ref'])['id'] for v in result['items']]


@pytest.mark.parametrize('text,basis', [('今天新增入库哪些案件', 'created_at'), ('今天台账更新了哪些案件', 'updated_at')])
def test_creation_and_update_dates_are_distinct(chat, services, case, text, basis):
    with chat.db.tx() as con:
        con.execute("UPDATE cases SET created_at='2026-10-18T16:00:00+00:00',updated_at='2026-10-19T16:00:00+00:00',report_at='2026-09-01' WHERE id=?", (case['id'],))
    ctx = dated_context(chat, case, text)
    result = call(ctx, 'search_cases', query='')
    assert result['date_filter']['date_basis'] == basis
    assert result['total'] == (1 if basis == 'created_at' else 0)


def test_today_photos_respect_beijing_midnight_and_wrong_year_is_rejected(chat, case, photo):
    pictures = [photo(color=(i, 20, 10)) for i in range(4)]
    times = ['2026-10-18T15:59:59.999+00:00', '2026-10-18T16:00:00.000+00:00',
             '2026-10-19T15:59:59.999+00:00', '2026-10-19T16:00:00.000+00:00']
    with chat.db.tx() as con:
        con.executemany('UPDATE photos SET created_at=? WHERE id=?', [(t, p['id']) for t, p in zip(times, pictures)])
    ctx = dated_context(chat, case, '今天有新照片么？')
    with pytest.raises(DomainError, match='2026-10-19'):
        call(ctx, 'search_photos', date_from='2025-01-22', date_to='2025-01-22')
    result = call(ctx, 'search_photos', date_period='today')
    assert result['total'] == 2
    assert {ctx.photo(v['photo_ref'])['id'] for v in result['items']} == {p['id'] for p in pictures[1:3]}
    assert '照片归档日期' in call(ctx, 'finish_reply', text='查到2张。')['text']


def test_explicit_historical_query_and_quoted_today_do_not_force_current_date(chat, case):
    ctx = dated_context(chat, case, '今天请帮我查询2025年1月22日的报案')
    result = call(ctx, 'search_cases', query='', date_from='2025-01-22', date_to='2025-01-22')
    assert result['date_filter']['date_from'] == '2025-01-22'
    assert not ctx.dates.required
    assert not dates('他说“今天新增了哪些报案”，这句话是什么意思？').required
    assert not dates('今天请刷新这个案件的官网资料').required


def test_two_periods_need_separate_reads_before_concluding():
    ctx = dates('今天和昨天各新增了哪些报案？')
    with pytest.raises(DomainError, match='多个日期范围'):
        ctx.resolve('search_cases', {})
    for period in ['today', 'yesterday']:
        args = ctx.resolve('search_cases', {'date_period': period})
        ctx.record('search_cases', args, {'total': 0})
        if period == 'today':
            with pytest.raises(DomainError, match='yesterday'):
                ctx.finish('两天均未找到。')
    assert '2026-10-18' in ctx.finish('两天均未找到。')


def test_workspace_retries_direct_unsupported_answer_and_wrong_tool_date(chat, case, monkeypatch):
    first = send(chat, '今天新增了哪些报案？', project_id=case['project_id'])
    with chat.db.tx() as con:
        con.execute('UPDATE workspace_chat_turns SET created_at=? WHERE id=?', (STAMP, first['turn_id']))
    requests = model(chat, monkeypatch, [response(text='今天（2025年1月22日）没有新增报案。'),
        response(('search_cases', {'query': '', 'date_from': '2025-01-22', 'date_to': '2025-01-22'})),
        response(('search_cases', {'query': '', 'date_period': 'today'})), response(text='已按报案日期查询当前范围。')])
    assert chat.run_one()
    result = chat.get(KEY, first['thread_id'])['turns'][0]
    assert result['state'] == 'completed' and '2026-10-19' in result['answer'] and '2025' not in result['answer']
    assert [v['ok'] for v in result['steps']] == [False, True]
    assert '服务器日期校验未通过' in dump(requests[1])
    assert 'time_context' in dump(requests[0])
    assert not chat.db.all('SELECT * FROM feishu_agent_outbox')


def test_feishu_bad_historical_today_is_dated_and_cannot_override_message_date(bot, case, monkeypatch):
    first = capture(bot, '今天有新照片么？')
    bot.collect_messages()
    previous = task_for(bot, first)
    with bot.db.tx() as con:
        con.execute("UPDATE feishu_agent_tasks SET state='completed',result_json=?,created_at=? WHERE id=?",
                    (dump({'text': '今天（2025年1月22日）没有收到新照片。'}), '2026-10-18T15:41:52+00:00', previous['id']))
    mid = capture(bot, '今天新增了哪些报案？')
    bot.collect_messages()
    task = task_for(bot, mid)
    task['input']['source_time'] = str(int(datetime.fromisoformat(STAMP).timestamp() * 1000))
    with bot.db.tx() as con:
        con.execute('UPDATE feishu_agent_tasks SET input_json=? WHERE id=?', (dump(task['input']), task['id']))
    requests = arrange_model(bot, monkeypatch, [
        response(('search_cases', {'query': '', 'date_from': '2025-01-22', 'date_to': '2025-01-22'})),
        response(('finish_reply', {'text': '今天没有新增报案。'})),
        response(('search_cases', {'query': '', 'date_period': 'today'})),
        response(('finish_reply', {'text': '已按本次消息日期查询，缺失日期的案件仍需核对。'}))])
    assert bot.run_one()
    result = task_for(bot, mid)
    assert result['state'] == 'completed' and not result['error']
    assert '2026-10-19' in result['result']['text'] and '2025' not in result['result']['text']
    assert '2026-10-18T23:41:52+08:00' in dump(requests[0])
    assert 'error' in json.loads(result['steps'][0]['outcome'])
    assert 'error' in json.loads(result['steps'][1]['outcome'])
    assert json.loads(result['steps'][2]['outcome'])['date_filter']['date_from'] == '2026-10-19'
    assert bot.test_client.sent == []


@pytest.mark.parametrize('value,expected', [('2026-10-19', '2026-10-19'), ('2026-10-19 00:00:00', '2026-10-19'),
    ('2026-10-18T16:00:00Z', '2026-10-19'), ('2026-02-30', None), (None, None)])
def test_source_date_formats_and_invalid_values(value, expected):
    assert local_date(value) == expected
