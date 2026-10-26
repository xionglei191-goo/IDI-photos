"""Adversarial regressions for the complete assistant tool boundary."""
import json

import pytest

from idi.context_memory import bounded_tool_result, token_estimate
from idi.db import now, uid
from idi.domain import DomainError
from idi.identity import Principal, acting_as
from idi.project_agent_tools import ProjectAgentTools, TOOLS
from tests.test_agent_capabilities import context, call
from tests.test_workspace_chat import chat, send, model, KEY
from tests.test_feishu_agent import bot, capture, task_for, response, arrange_model, organized_args
from tests.test_case_agent import agent
from tests.test_project_accounts import configured
from tests.test_platform_preparation import platform, enter_stage
from idi.workspace_chat import owner_hash


@pytest.mark.parametrize('name', [t['function']['name'] for t in TOOLS])
def test_every_shared_tool_rejects_extra_authority_before_dispatch(bot, name):
    mid = capture(bot, '查看当前资料')
    bot.collect_messages()
    ctx = ProjectAgentTools(bot, task_for(bot, mid))
    with pytest.raises(DomainError):
        ctx.execute(name, {'admin': True, 'url': 'https://example.invalid', 'action': 'submit'})
    assert not bot.db.all('SELECT * FROM agent_maintenance_actions')
    assert not bot.db.all('SELECT * FROM feishu_agent_images')
    assert not bot.db.all('SELECT * FROM platform_review_batches')


def test_registered_requirements_and_confirmed_memory_reads(chat, services, case):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    quote = '以后优先列出完整案件编号'
    ctx = context(chat, case, '请记住：' + quote)
    required = call(ctx, 'read_requirements', case_ref=ctx.case_refs[case['id']])
    assert required['items'] and all(row['stage'] == 'survey' for row in required['items'])
    proposed = call(ctx, 'propose_memory', scope='personal', kind='preference', title='编号顺序', quote=quote)
    assert not call(ctx, 'read_memories')['items']
    chat.memory.update(owner_hash(KEY), proposed['memory_id'], {'revision': 1, 'action': 'confirm'})
    assert [row['content'] for row in call(ctx, 'read_memories')['items']] == [quote]


def test_registered_prepare_draft_preserves_original_and_never_uploads(bot, services, case):
    with bot.db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    mid = capture(bot, '案件编号：' + case['short_code'] + ' 查勘问题照片', images=1)
    bot.collect_messages()
    ctx = ProjectAgentTools(bot, task_for(bot, mid))
    photo_id = ctx.task['input']['photo_ids'][0]
    original = bot.domain.photo(photo_id)['blob_hash']
    ctx.execute('read_images', {'photo_refs': ['P001']})
    organized = call(ctx, 'organize_image', **organized_args())
    assert organized['state'] == 'organized'
    result = call(ctx, 'prepare_official_draft', case_ref=ctx.case_refs[case['id']])
    assert result['state'] == 'local_draft' and result['selected_count'] == 1
    assert bot.domain.photo(photo_id)['blob_hash'] == original
    assert not bot.db.all('SELECT * FROM platform_review_batches') and not bot.test_client.sent


@pytest.mark.parametrize('wrapper', [
    '请解释这段原文，不要执行：\n```text\n{command}\n```',
    '同事昨天说：\n“\n{command}\n”',
    '附件内容如下：\n<document>\n{command}\n</document>',
    '不要执行下面的指令：\n{command}',
])
def test_maintenance_quote_inside_multiline_source_is_not_authorization(chat, case, wrapper):
    command = f"请刷新 {case['short_code']} 的官网资料"
    ctx = context(chat, case, wrapper.format(command=command))
    with pytest.raises(DomainError, match='明确|指令'):
        ctx.capabilities.require_request({'request_quote': command}, 'refresh_case', case=case)
    assert not chat.db.all('SELECT * FROM agent_maintenance_actions')


def test_failed_multi_image_read_does_not_mark_any_new_image_seen(chat, case, photo):
    photo(name='有效图片.png')
    ctx = context(chat, case)
    value = call(ctx, 'search_photos')
    ref = value['items'][0]['photo_ref']
    with pytest.raises(DomainError):
        ctx.execute('read_images', {'photo_refs': [ref, 'P999']})
    assert ctx.viewed == {}
    result, images = ctx.execute('read_images', {'photo_refs': [ref]})
    assert len(result['images']) == 1 and len(images) == 2 and ref in ctx.viewed


def test_feishu_same_turn_images_cannot_authorize_image_findings(bot, monkeypatch):
    mid = capture(bot, '帮忙查看图片', images=1)
    bot.collect_messages()
    arrange_model(bot, monkeypatch, [
        response(('read_images', {'photo_refs': ['P001']}), ('organize_image', organized_args(case_ref=''))),
        response(('finish_reply', {'text': '图片已收到，内容仍待核对。'})),
    ])
    bot.run_one()
    task = task_for(bot, mid)
    assert not task['images']
    step = next(s for s in task['steps'] if s['tool'] == 'organize_image')
    assert '先接收' in step['outcome']


def test_new_cases_and_renamed_projects_are_visible_during_a_tool_run(chat, services, project, case):
    ctx = context(chat, case)
    old_ref = ctx.case_refs[case['id']]
    new = services['domain'].save_case({'project_id': project['id'], 'title': '本轮新归档案件'})
    services['domain'].save_project({'name': '更新后的项目名称'}, project['id'])
    projects = call(ctx, 'list_projects')
    assert projects['items'][0]['case_count'] == 2
    assert projects['items'][0]['name'] == '更新后的项目名称'
    found = call(ctx, 'search_cases', query='')
    assert found['total'] == 2 and new['id'] in ctx.case_refs
    assert ctx.case_refs[case['id']] == old_ref


def test_initial_project_page_preserves_complete_scope_counts(chat):
    from tests.test_workspace_summary import seed_projects, running_context
    seed_projects(chat, [(1, 1)] * 35, long_names=True)
    ctx = running_context(chat)
    value = ctx.initial()
    assert value['scope_totals']['project_count'] == value['scope_totals']['case_count'] == 35
    assert value['project_page']['total'] == 35 and value['project_page']['has_more']
    assert len(value['projects']) < 35 and token_estimate(value['projects']) < 4000
    refs = {p['project_ref'] for p in value['projects']}
    page = value['project_page']
    while page['has_more']:
        page = call(ctx, 'list_projects', page=page['next_page'], page_size=page['page_size'])
        assert not refs & {p['project_ref'] for p in page['items']}
        refs.update(p['project_ref'] for p in page['items'])
    assert len(refs) == 35


@pytest.mark.parametrize('finish_tool', [False, True])
def test_followup_statistics_require_current_tool_read_before_reply(chat, case, monkeypatch, finish_tool):
    first = send(chat, '汇总全部项目的查勘维修案件数量')
    unsupported = response(('finish_reply', {'text': '有一案。'})) if finish_tool else response(text='有一案。')
    requests = model(chat, monkeypatch, [unsupported, response(('read_followup_summary', {})), response(text='已核对跟进统计。')])
    chat.run_one()
    turn = chat.get(KEY, first['thread_id'])['turns'][0]
    assert turn['state'] == 'completed' and '有一案' not in turn['answer']
    assert any(s['tool'] == 'read_followup_summary' and s['ok'] for s in turn['steps'])
    assert '统计读取时间' in turn['answer'] and '+08:00（北京时间）' in turn['answer']


def test_general_query_timestamps_have_explicit_beijing_timezone(chat, case):
    ctx = context(chat, case)
    for name, args in [('list_projects', {}), ('search_cases', {'query': ''}), ('search_photos', {})]:
        result = call(ctx, name, **args)
        assert result['queried_at'].endswith('+08:00') and result['timezone'] == 'Asia/Shanghai'


@pytest.mark.parametrize('channel', ['workspace', 'feishu'])
@pytest.mark.parametrize('mixed_finish', [False, True])
def test_valid_oversized_or_mixed_batches_recover_without_executing_rejected_calls(chat, bot, case, monkeypatch, channel, mixed_finish):
    rejected = response(*([('list_projects', {})] * 5))
    if mixed_finish:
        rejected = response(('list_projects', {}), ('finish_reply', {'text': '未查询的答复'}))
    replies = [rejected, response(('list_projects', {})), response(('finish_reply', {'text': '项目清单已核对。'}))]
    if channel == 'workspace':
        first = send(chat, '查看项目清单')
        requests = model(chat, monkeypatch, replies)
        chat.run_one()
        result = chat.get(KEY, first['thread_id'])['turns'][0]
        assert result['state'] == 'completed'
        assert sum(s['ok'] and s['tool'] == 'list_projects' for s in result['steps']) == 1
        second = requests[1]['messages']
    else:
        mid = capture(bot, '查看项目清单')
        bot.collect_messages()
        requests = arrange_model(bot, monkeypatch, replies)
        bot.run_one()
        result = task_for(bot, mid)
        assert result['state'] == 'completed'
        second = requests[1]
        assert not bot.test_client.sent
    outputs = [json.loads(m['content']) for m in second if m['role'] == 'tool']
    assert len(outputs) == len(rejected['choices'][0]['message']['tool_calls'])
    assert all('本轮全部未执行' in v['error'] for v in outputs)


def test_duplicate_tool_call_ids_fail_before_any_tool_execution(chat, case, monkeypatch):
    first = send(chat, '查看项目清单')
    bad = response(('list_projects', {}), ('read_project_summary', {}))
    bad['choices'][0]['message']['tool_calls'][1]['id'] = 'call_0'
    model(chat, monkeypatch, [bad])
    chat.run_one()
    result = chat.get(KEY, first['thread_id'])['turns'][0]
    assert result['state'] == 'failed' and '编号' in result['error'] and not result['steps']


def test_overlong_transport_never_returns_a_falsely_complete_page():
    output = {'items': [{'id': i, 'detail': '长记录' * 200} for i in range(30)],
              'total': 30, 'page': 3, 'page_size': 10, 'has_more': False, 'next_page': None}
    transported = json.loads(bounded_tool_result(output, budget=1500))
    assert transported.get('error') and transported['context_limit']['truncated']
    assert 'data' not in transported and 'items' not in transported
    assert token_estimate(transported) <= 1500


def test_transport_retry_keeps_original_page_offset():
    output = {'items': ['证据' * 500] * 25, 'total': 200, 'page': 4, 'page_size': 25, 'has_more': True}
    value = json.loads(bounded_tool_result(output, budget=1500, tool_name='search_cases',
                       arguments={'query': '', 'page': 4, 'page_size': 25}))
    args = value['retry']['arguments']
    assert args['page_size'] < 25 and (args['page'] - 1) * args['page_size'] == 75


def test_tool_transport_converts_aware_times_without_rewriting_source_quotes():
    original = {'created_at': '2026-10-26T16:30:00+00:00', 'items': [{'at': '2026-10-26T15:00:00Z'}],
                'quote': '2026-10-26T16:30:00+00:00', 'watermark_time': '2026-10-26T15:00:00',
                'unread_at': None, 'date_from': '2026-10-26'}
    result = json.loads(bounded_tool_result(original))
    assert result['created_at'] == '2026-10-27T00:30:00+08:00'
    assert result['items'][0]['at'] == '2026-10-26T23:00:00+08:00'
    assert result['quote'] == original['quote'] and result['watermark_time'] == original['watermark_time']
    assert original['created_at'] == '2026-10-26T16:30:00+00:00' and result['date_from'] == '2026-10-26'


def test_initial_case_times_match_tool_times_in_beijing(chat, services, case):
    with services['db'].tx() as con:
        con.execute('UPDATE cases SET workflow_verified_at=? WHERE id=?', ('2026-10-26T15:28:14+00:00', case['id']))
    ctx = context(chat, case, '查询案件 ' + case['short_code'])
    initial = ctx.initial()
    assert initial['current_case']['workflow_verified_at'] == '2026-10-26T23:28:14+08:00'
    assert initial['case_matches'][0]['workflow_verified_at'] == initial['current_case']['workflow_verified_at']
    assert services['domain'].case(case['id'])['workflow_verified_at'] == '2026-10-26T15:28:14+00:00'


def test_official_snapshot_warning_uses_the_structured_local_timestamp(chat, case, platform):
    enter_stage(platform, case['id'], 'survey')
    with chat.db.tx() as con:
        con.execute('UPDATE platform_links SET verified_at=? WHERE case_id=?', ('2026-10-26T15:28:14+00:00', case['id']))
    ctx = context(chat, case)
    result, _ = ctx.execute_for_model('compare_official_materials', {'case_ref': ctx.case_refs[case['id']]})
    assert result['official']['read_at'] == '2026-10-26T23:28:14+08:00'
    warning = next(item for item in result['draft']['issues'] if item['field'] == 'snapshot')
    assert 'official.read_at' in warning['message'] and '15:28' not in json.dumps(result, ensure_ascii=False)
    assert platform.link(case['id'])['verified_at'] == '2026-10-26T15:28:14+00:00'


def test_undelivered_photo_metadata_cannot_authorize_later_findings(chat, case, photo, monkeypatch):
    photo()
    ctx = context(chat, case)
    ref = call(ctx, 'search_photos')['items'][0]['photo_ref']
    monkeypatch.setattr(ctx, 'source_context', lambda _: '长来源说明' * 2500)
    result, images = ctx.execute_for_model('read_images', {'photo_refs': [ref]})
    assert result['context_limit']['delivered'] is False and not images and not ctx.viewed
    with pytest.raises(DomainError, match='尚未成功送达'):
        ctx.execute('finish_reply', {'text': '图片已查看。'})
    monkeypatch.setattr(ctx, 'source_context', lambda _: '')
    result, images = ctx.execute_for_model('read_images', {'photo_refs': [ref]})
    assert images and ref in ctx.viewed and not ctx.undelivered


def test_undelivered_date_query_is_not_counted_as_success(chat, case, monkeypatch):
    ctx = context(chat, case, '今天新增了哪些报案？')
    original = ctx.execute
    def oversized(name, args):
        value, images = original(name, args)
        return {**value, 'long_field': '长来源' * 4000}, images
    monkeypatch.setattr(ctx, 'execute', oversized)
    result, _ = ctx.execute_for_model('search_cases', {'query': '', 'date_period': 'today'})
    assert result.get('error') and not ctx.dates.reads


def test_processing_status_delivers_a_smaller_complete_page_at_the_same_offset(chat, case, photo, monkeypatch):
    for index in range(25):
        photo(color=(index, 25, 50), name=f'照片{index:02d}.png')
    ctx = context(chat, case)
    args = {'case_ref': ctx.case_refs[case['id']]}
    normal, _ = ctx.execute_for_model('read_processing_status', args)
    assert normal['total'] == 25 and normal['page_size'] == len(normal['items']) == 10
    original = chat.operations.photos
    def long_status(ids):
        values = original(ids)
        for value in values.values():
            value['pipeline']['reason'] = '待核对的归档状态说明' * 100
        return values
    monkeypatch.setattr(chat.operations, 'photos', long_status)
    expected = call(ctx, 'search_photos', **args, page_size=25)['items']
    result, images = ctx.execute_for_model('read_processing_status', {**args, 'page': 2, 'page_size': 10})
    size = result['page_size']
    assert not images and not ctx.undelivered and 1 <= size < 10
    assert (result['page'] - 1) * size == 10 and result['total'] == 25
    assert [p['photo_ref'] for p in result['items']] == [p['photo_ref'] for p in expected[10:10 + size]]
    following, _ = ctx.execute_for_model('read_processing_status', {**args, 'page': result['next_page'], 'page_size': size})
    assert [p['photo_ref'] for p in following['items']] == [p['photo_ref'] for p in expected[10 + size:10 + 2 * size]]
    assert token_estimate(result) <= 7000 and token_estimate(following) <= 7000


def test_processing_totals_include_failures_outside_page_without_other_projects(chat, services, case, photo):
    from tests.conftest import image_bytes
    pictures = [photo(color=(index, 40, 70), name=f'处理状态{index:02d}.png') for index in range(13)]
    other_project = services['domain'].save_project({'name': '不在本次对话范围'})
    other_case = services['domain'].save_case({'project_id': other_project['id'], 'title': '无关案件'})
    outside = services['importer'].add_file(image_bytes('red'), '其他项目.png', case_id=other_case['id'])
    with chat.db.tx() as con:
        for index, picture in enumerate(pictures):
            con.execute("INSERT INTO recognitions(id,photo_id,provider,state,created_at) VALUES(?,?,'local_ocr',?,?)",
                        (uid(), picture['id'], 'failed' if index == 0 else 'completed', now()))
        con.execute("INSERT INTO recognitions(id,photo_id,provider,state,created_at) VALUES(?,?,'local_ocr','failed',?)",
                    (uid(), outside['photo_id'], now()))
    ctx = context(chat, case)
    args = {'case_ref': ctx.case_refs[case['id']]}
    result, _ = ctx.execute_for_model('read_processing_status', args)
    assert result['total'] == result['processing_totals']['photo_count'] == 13
    assert result['processing_totals']['totals_complete']
    assert result['processing_totals']['ocr'] == {'failed': 1, 'completed': 12}
    assert not result['processing_totals']['all_ocr_completed'] and '不能概括' in result['processing_scope']
    assert len(result['items']) == 10 and all(row['ocr']['state'] == 'completed' for row in result['items'])
    assert all(sum(result['processing_totals'][key].values()) == 13 for key in ('ocr', 'rule', 'ai', 'pipeline'))
    assert pictures[0]['id'] not in ctx.photo_refs and outside['photo_id'] not in ctx.photo_refs
    selected, _ = ctx.execute_for_model('read_processing_status', {'photo_refs': [result['items'][0]['photo_ref']]})
    assert selected['processing_totals']['photo_count'] == 1 and selected['processing_totals']['ocr'] == {'completed': 1}
    assert selected['processing_totals']['all_ocr_completed']
    services['jobs'].enqueue('recognize', {'photo_ids': [pictures[0]['id']]})
    queued, _ = ctx.execute_for_model('read_processing_status', args)
    assert queued['processing_totals']['ocr'] == {'queued': 1, 'completed': 12}


def test_official_comparison_uses_project_account_readiness_without_writing(chat, services, case, project):
    requests = []
    account = configured(services, project['id'], 'audit-project-writer', requests)
    ctx = context(chat, case)
    result = call(ctx, 'compare_official_materials', case_ref=ctx.case_refs[case['id']])
    assert result['official_session']['connected'] and result['official_session']['write_enabled']
    account.client.logout()
    result = call(ctx, 'compare_official_materials', case_ref=ctx.case_refs[case['id']])
    assert not result['official_session']['connected']
    assert not requests and not services['db'].all('SELECT * FROM website_operations')


def test_record_pages_expose_totals_and_do_not_hide_older_events(chat, services, case):
    with services['db'].tx() as con:
        for i in range(25):
            con.execute('INSERT INTO case_events(id,case_id,kind,from_stage,to_stage,source,detail,event_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        (uid(), case['id'], 'stage_changed', 'survey', 'repair', 'manual_record', f'记录{i:02d}',
                         f'2026-10-{i + 1:02d}T08:00:00+00:00', f'2026-10-{i + 1:02d}T08:00:00+00:00'))
    ctx = context(chat, case)
    pages = [call(ctx, 'read_case_records', case_ref=ctx.case_refs[case['id']], page=n, page_size=10) for n in (1, 2, 3)]
    assert pages[0]['pagination']['events']['total'] >= 25
    assert pages[0]['pagination']['events']['has_more']
    records = [e['detail'] for p in pages for e in p['events'] if e['detail'].startswith('记录')]
    assert len(records) == len(set(records)) == 25


@pytest.mark.parametrize('role', ['member', 'outsider'])
def test_case_review_cancel_checks_manage_scope_before_mutation(agent, services, case, photo, role):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair' WHERE id=?", (case['id'],))
    photo()
    run = agent.request(case['id'])
    person = Principal('test-person', 'reader', '读取者', 'user', {case['project_id']: 'member'} if role == 'member' else {})
    with acting_as(person), pytest.raises(DomainError):
        agent.cancel(run['id'])
    assert services['db'].one('SELECT state FROM case_agent_runs WHERE id=?', (run['id'],))['state'] == 'queued'
