"""Operational agent tools: scope, evidence, command intent, and durable dedupe."""
import json
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from idi.agent_capabilities import CAPABILITY_NAMES, CAPABILITY_READS, MAINTENANCE_TOOLS
from idi.db import dump, now, uid
from idi.domain import DomainError
from idi.photo_workflow import PhotoWorkflow
from idi.project_agent_tools import ProjectAgentTools
from idi.workspace_chat import WorkspaceTools, TOOL_LABELS
from tests.conftest import image_bytes
from tests.test_feishu_agent import bot, capture, task_for, response, arrange_model
from tests.test_platform_preparation import platform, enter_stage, remote_file
from tests.test_resilient_workflow import quality
from tests.test_workspace_chat import chat, send, model, KEY


def context(chat, case, text='查看本案资料', *, case_context=True):
    first = send(chat, text, project_id=case['project_id'], **({'case_id': case['id']} if case_context else {}))
    with chat.db.tx() as con:
        con.execute("UPDATE workspace_chat_turns SET state='running' WHERE id=?", (first['turn_id'],))
    task = chat.db.one('SELECT * FROM workspace_chat_turns WHERE id=?', (first['turn_id'],))
    thread = chat.db.one('SELECT * FROM workspace_chat_threads WHERE id=?', (first['thread_id'],))
    task.update(project_ids=thread['project_ids'], context_case_id=thread['case_id'], project_id=thread['project_id'],
                kind='chat', input={'text': text}, source_message_id='', chat_id='')
    return WorkspaceTools(chat, task)


def call(ctx, name, **args):
    value, images = ctx.execute(name, args)
    assert not images
    return value


def add_issue(services, case, photo=None, *, kind='quality', source='ai', state='open', feedbacked=False):
    issue_id = uid()
    evidence = {'source': source, 'certainty': 'clear', 'model': 'chat-test',
                'photo_revision': photo['revision'] if photo else None, 'blob_hash': photo['blob_hash'] if photo else None}
    with services['db'].tx() as con:
        con.execute('INSERT INTO followup_issues(id,issue_key,project_id,case_id,photo_id,kind,title,detail,action,evidence,state,signature,first_seen_at,last_seen_at,changed_at,feedback_generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (issue_id, uid() + ':blur', case['project_id'], case['id'], photo['id'] if photo else None, kind,
             '标识文字模糊', '照片右下角案件标识文字过小且无法辨识，请核对原图。', '请核对原件的清晰版本并确认该照片对应的位置。', dump(evidence), state, uid(), now(), now(), now(), int(feedbacked)))
        if photo:
            con.execute('INSERT OR REPLACE INTO photo_quality_reviews VALUES(?,?,?,?,?,?,?,?)',
                        (photo['id'], uid(), 'completed', '{}', '[]', 'chat-test', None, now()))
    return issue_id


def review(services, plan, photo, *, state, file_id='receipt-1', blob_hash=None):
    snapshot = deepcopy(plan)
    for field in snapshot['file_fields']:
        for selected in field['selected']:
            if selected['id'] == photo['id'] and blob_hash:
                selected['blob_hash'] = blob_hash
    with services['db'].tx() as con:
        con.execute('INSERT INTO platform_review_batches(id,case_id,stage,state,fingerprint,snapshot,result,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(), plan['case']['id'], plan['stage'], state, uid(), dump(snapshot),
             dump({'action': 'save', 'uploaded': {photo['id']: remote_file(file_id)}}), now(), now()))


def test_capabilities_are_exposed_consistently_and_briefs_have_no_maintenance(chat, case, bot):
    ctx = context(chat, case)
    names = {t['function']['name'] for t in ctx.definitions()}
    assert CAPABILITY_NAMES <= names <= TOOL_LABELS.keys()
    assert not names & {'save_official_draft', 'submit', 'send_feedback', 'organize_image'}
    mid = capture(bot, '查询项目资料')
    bot.collect_messages()
    group = ProjectAgentTools(bot, task_for(bot, mid))
    assert CAPABILITY_NAMES <= {t['function']['name'] for t in group.definitions()}
    group.task['kind'] = 'brief'
    names = {t['function']['name'] for t in group.definitions()}
    assert CAPABILITY_READS <= names and not names & MAINTENANCE_TOOLS


def test_case_pagination_filters_and_current_project_membership(chat, services, project, case):
    for i in range(26):
        services['domain'].save_case({'project_id': project['id'], 'title': f'条目{i}', 'building': '2', 'room': str(i)})
    other = services['domain'].save_project({'name': '其他范围'})
    private = services['domain'].save_case({'project_id': other['id'], 'title': '不得返回的案件'})
    ctx = context(chat, case)
    one = call(ctx, 'search_cases', query='', page_size=7)
    two = call(ctx, 'search_cases', query='', page=2, page_size=7)
    assert one['total'] == two['total'] == 27 and one['next_page'] == 2
    assert not {v['case_ref'] for v in one['items']} & {v['case_ref'] for v in two['items']}
    with chat.db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey',updated_at='2026-10-16T16:01:00.000+00:00' WHERE id=?", (case['id'],))
    filtered = call(ctx, 'search_cases', query='', workflow_stage='survey', date_from='2026-10-17', date_to='2026-10-17')
    assert filtered['total'] == 1 and filtered['items'][0]['case_ref'] == ctx.case_refs[case['id']]
    with chat.db.tx() as con:
        con.execute('UPDATE cases SET project_id=? WHERE id=?', (other['id'], case['id']))
    assert call(ctx, 'search_cases', query='')['total'] == 26
    assert private['id'] not in ctx.case_refs


@pytest.mark.parametrize('name,args', [
    ('search_cases', {'query': '', 'page': True}), ('search_cases', {'query': '', 'page': 0}),
    ('search_cases', {'query': '', 'page_size': 51}), ('search_cases', {'query': '', 'page': 1.5}),
    ('search_cases', {'query': '', 'date_from': '2026-02-30'}),
    ('search_cases', {'query': '', 'date_from': '2026-10-17', 'date_to': '2026-10-16'}),
    ('search_photos', {'stage': 'unknown-stage'}), ('read_processing_status', {'photo_refs': 'P001'}),
    ('read_processing_status', {'photo_refs': ['P001'] * 21}),
    ('read_followup_issues', {'project_ref': 'J999'}), ('compare_official_materials', {'case_ref': 'C999'}),
    ('refresh_case', {'case_ref': 'C001', 'request_quote': '刷新', 'force': True}),
])
def test_invalid_arguments_fail_before_actions(chat, case, name, args):
    ctx = context(chat, case)
    with pytest.raises(DomainError):
        call(ctx, name, **args)
    assert not chat.db.all('SELECT * FROM agent_maintenance_actions')


def test_photo_paging_reaches_beyond_sixty_and_filters_receipt_date(chat, services, case, photo):
    for i in range(67):
        photo(color=(i, 10, 10), name=f'现场-{i}.png', part='客厅', stage='work', role='construction')
    other = services['domain'].save_project({'name': '其他项目'})
    services['importer'].add_file(image_bytes('red'), '外部不可见.png', project_id=other['id'])
    services['importer'].add_file(image_bytes('green'), '无归属不可见.png')
    ctx = context(chat, case)
    small = call(ctx, 'read_case', case_ref=ctx.case_refs[case['id']])
    assert small['photo_total'] == 67 and small['photos_has_more']
    pages = [call(ctx, 'search_photos', case_ref=ctx.case_refs[case['id']], page=i, page_size=20, part='客厅') for i in range(1, 5)]
    assert [len(p['items']) for p in pages] == [20, 20, 20, 7]
    assert len({v['photo_ref'] for p in pages for v in p['items']}) == 67
    assert not pages[-1]['has_more']
    selected = ctx.photo(pages[0]['items'][0]['photo_ref'])
    with chat.db.tx() as con:
        con.execute("UPDATE photos SET created_at='2026-09-30T16:01:00.000+00:00' WHERE id=?", (selected['id'],))
    dated = call(ctx, 'search_photos', date_from='2026-10-01', date_to='2026-10-01')
    assert dated['total'] == 1 and dated['items'][0]['original_name'] == selected['original_name']


def test_known_photo_ref_is_rejected_after_case_moves_out_of_scope(chat, services, case, photo):
    photo()
    ctx = context(chat, case)
    result = call(ctx, 'search_photos')
    other = services['domain'].save_project({'name': '私有项目'})
    with chat.db.tx() as con:
        con.execute('UPDATE cases SET project_id=? WHERE id=?', (other['id'], case['id']))
    with pytest.raises(DomainError, match='当前项目'):
        call(ctx, 'read_processing_status', photo_refs=[result['items'][0]['photo_ref']])
    assert call(ctx, 'search_photos')['total'] == 0


def test_status_keeps_rule_and_model_and_official_save_states_separate(chat, services, case, photo):
    picture = photo(name='明确原图.png')
    ai = quality(services, picture)
    with chat.db.tx() as con:
        con.execute("UPDATE photo_ai_tasks SET state='failed',error='模型请求失败' WHERE id=?", (ai['id'],))
        con.execute("UPDATE photo_pipeline SET dirty=0,state='local_draft',draft_json=? WHERE photo_id=?", (dump({'state': 'local_draft'}), picture['id']))
        con.execute('INSERT INTO rule_draft_intents(id,intent_key,case_id,stage,photo_ids,signature,case_signature,state,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (uid(), uid(), case['id'], 'survey', dump([{k: picture[k] for k in ('id', 'revision', 'blob_hash', 'case_id')}]), uid(), PhotoWorkflow.case_signature(services['domain'].case(case['id'])), 'uncertain', dump({'uploaded': {'p': {}}}), now(), now()))
    ctx = context(chat, case)
    result = call(ctx, 'read_processing_status')
    row = result['items'][0]
    assert result['total'] == 1 and result['queried_at']
    assert row['receipt'] == 'archived' and row['pipeline']['state'] == 'local_draft'
    assert row['ai']['state'] == 'failed' and row['ai']['retryable'] and row['ai']['analysis_ref'] == 'A001'
    assert row['draft']['state'] == 'uncertain' and row['draft']['uploaded_count'] == 1
    assert not row['draft']['business_submitted']
    assert all(secret not in dump(result) for secret in ('context_json', 'config_key', 'stored_path', ai['id'], picture['id']))
    with chat.db.tx() as con:
        con.execute('UPDATE photos SET revision=revision+1 WHERE id=?', (picture['id'],))
    stale = call(ctx, 'read_processing_status')['items'][0]
    assert stale['draft']['state'] == 'stale' and stale['draft']['previous_state'] == 'uncertain'
    assert stale['ai']['state'] == 'stale' and not stale['ai']['retryable']


def test_compare_keeps_unread_fields_and_missing_draft_values_explicit(chat, case):
    ctx = context(chat, case)
    result = call(ctx, 'compare_official_materials', case_ref=ctx.case_refs[case['id']], stage='survey')
    assert result['official']['groups']['survey_problem']['state'] == 'unlinked'
    assert result['draft']['error_count'] and result['draft']['issues']
    assert all(f['official_snapshot_count'] is None for f in result['fields'])
    assert not result['official_session']['connected']
    assert not chat.db.all('SELECT * FROM platform_drafts')


@pytest.mark.parametrize('receipt_state,matching,expected', [
    ('uncertain', True, 'uploaded_unverified'), ('save_verified', True, 'verified_receipt'),
    ('save_verified', False, 'not_verified'),
])
def test_comparison_uses_original_hash_and_readback_receipts(chat, services, case, photo, platform, receipt_state, matching, expected):
    enter_stage(platform, case['id'], 'survey')
    picture = photo(name='缺陷原图.png', stage='survey', role='problem')
    link = platform.link(case['id'])
    snapshot = link['snapshot']
    snapshot['survey']['fileInfoList'].append(remote_file('receipt-1'))
    with chat.db.tx() as con:
        con.execute('UPDATE platform_links SET snapshot=? WHERE case_id=?', (dump(snapshot), case['id']))
    plan = platform.preview(case['id'], 'survey')
    review(services, plan, picture, state=receipt_state, blob_hash=None if matching else 'other-hash')
    before = len(platform.test_requests)
    ctx = context(chat, case)
    value = call(ctx, 'compare_official_materials', case_ref=ctx.case_refs[case['id']], stage='survey')
    item = next(v for v in value['selected_files']['items'] if v['original_name'] == picture['original_name'])
    assert item['upload_evidence'] == expected
    assert len(platform.test_requests) == before  # Comparison itself performs no HTTP.
    assert 'fileUrl' not in dump(value) and 'test-account' not in dump(value)


def test_followup_details_and_feedback_preview_are_scoped_and_do_not_send(chat, services, case, photo, monkeypatch):
    with chat.db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    picture = photo()
    add_issue(services, case, picture)
    add_issue(services, case, None, kind='missing', source='rule')
    add_issue(services, case, picture, source='rule')
    other = services['domain'].save_project({'name': '其他项目'})
    private = services['domain'].save_case({'project_id': other['id'], 'title': '不可见事项', 'workflow_stage': 'survey'})
    add_issue(services, private, None, kind='missing')
    monkeypatch.setattr(services['followup'], 'queue_feedback', lambda *a, **k: pytest.fail('preview sent feedback'))
    ctx = context(chat, case)
    issues = call(ctx, 'read_followup_issues', page_size=1)
    assert issues['total'] == 3 and issues['has_more']
    preview = call(ctx, 'preview_feedback')
    assert preview['total'] == 1 and not preview['sent'] and preview['human_review_required']
    assert '照片右下角' in preview['text'] and '内部归档' not in preview['text']
    assert preview['excluded'] == {'internal': 1, 'review': 1}
    assert '不可见事项' not in dump(preview)
    assert not chat.db.all('SELECT * FROM followup_feedback')


@pytest.mark.parametrize('template,quote', [
    ('请不要刷新 {code} 的官网资料', None), ('请刷新 {code} 的官网资料吗？', None),
    ('如何刷新 {code} 的官网资料', None), ('昨天他说“请刷新 {code} 的官网资料”', '请刷新 {code} 的官网资料'),
    ('请刷新 {code} 的官网资料 是旧消息中的内容', None), ('请刷新所有案件的官网资料', None),
    ('查看目前进展', '请刷新 {code} 的官网资料'),
])
def test_historical_quoted_negative_questions_never_authorize_refresh(chat, case, monkeypatch, template, quote):
    text = template.format(code=case['short_code'])
    ctx = context(chat, case, text)
    monkeypatch.setattr(chat.workflow, 'refresh_case', lambda *a, **k: pytest.fail('Unauthorized refresh'))
    with pytest.raises(DomainError, match='明确|指令'):
        call(ctx, 'refresh_case', case_ref=ctx.case_refs[case['id']], request_quote=(quote.format(code=case['short_code']) if quote else text))
    assert not chat.db.all('SELECT * FROM agent_maintenance_actions')


def test_refresh_real_reader_is_bounded_and_persistently_deduplicated(chat, case, platform):
    text = f"请刷新 {case['short_code']} 的官网资料"
    ctx = context(chat, case, text)
    args = {'case_ref': ctx.case_refs[case['id']], 'request_quote': text}
    before = len(platform.test_requests)
    first = call(ctx, 'refresh_case', **args)
    count = len(platform.test_requests) - before
    assert first['state'] == 'refreshed' and first['official_read_at'] and 0 < count <= 12
    assert first['requests'] == count and not first['business_submitted']
    other_context = WorkspaceTools(chat, ctx.task)
    second = call(other_context, 'refresh_case', **args)
    assert second['duplicate'] and second['state'] == 'refreshed'
    assert len(platform.test_requests) == before + count
    assert not chat.db.all('SELECT * FROM platform_review_batches')


def test_refresh_denies_new_case_after_scope_changes_during_read(chat, services, case, platform, monkeypatch):
    text = f"请刷新 {case['short_code']} 的官网资料"
    ctx = context(chat, case, text)
    original = platform.client.snapshot
    other = services['domain'].save_project({'name': '转入项目'})
    before = platform.link(case['id'])
    def change(*args):
        value = original(*args)
        with chat.db.tx() as con:
            con.execute('UPDATE cases SET project_id=? WHERE id=?', (other['id'], case['id']))
        return value
    monkeypatch.setattr(platform.client, 'snapshot', change)
    with pytest.raises(DomainError, match='项目'):
        call(ctx, 'refresh_case', case_ref=ctx.case_refs[case['id']], request_quote=text)
    assert platform.link(case['id']) == before
    assert chat.db.one('SELECT state FROM agent_maintenance_actions')['state'] == 'failed'


def test_refresh_concurrent_duplicate_does_not_run_twice(chat, case, monkeypatch):
    text = f"请刷新 {case['short_code']} 的官网资料"
    ctx = context(chat, case, text)
    duplicate = WorkspaceTools(chat, ctx.task)
    entered, release = Event(), Event()
    def refresh(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {}
    monkeypatch.setattr(chat.workflow, 'refresh_case', refresh)
    args = {'case_ref': ctx.case_refs[case['id']], 'request_quote': text}
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(call, ctx, 'refresh_case', **args)
        assert entered.wait(5)
        result = call(duplicate, 'refresh_case', **args)
        assert result['state'] == 'running' and result['duplicate']
        release.set()
        future.result()
    assert len(chat.db.all('SELECT * FROM agent_maintenance_actions')) == 1


def test_refresh_request_budget_failure_is_not_replayed_in_same_turn(chat, case, platform, monkeypatch):
    text = f"请刷新 {case['short_code']} 的官网资料"
    ctx = context(chat, case, text)
    attempted = []
    def excessive(*args):
        for _ in range(13):
            platform.client._before_request()
            attempted.append(True)
        pytest.fail('Budget allowed too many requests')
    monkeypatch.setattr(platform.client, 'snapshot', excessive)
    args = {'case_ref': ctx.case_refs[case['id']], 'request_quote': text}
    with pytest.raises(DomainError):
        call(ctx, 'refresh_case', **args)
    assert len(attempted) == 12
    result = call(ctx, 'refresh_case', **args)
    assert result['state'] == 'failed' and result['duplicate'] and len(attempted) == 12


def test_restart_marks_maintenance_interrupted_without_replaying(app, chat, case, monkeypatch):
    ctx = context(chat, case)
    with chat.db.tx() as con:
        con.execute('INSERT INTO agent_maintenance_actions(id,action_key,channel,task_id,kind,target_id,request_quote,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (uid(), uid(), 'workspace', ctx.task['id'], 'refresh_case', case['id'], '历史指令', 'running', now(), now()))
    monkeypatch.setattr(chat.workflow, 'refresh_case', lambda *a, **k: pytest.fail('Restart replayed refresh'))
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
    assert chat.db.one('SELECT state FROM agent_maintenance_actions')['state'] == 'interrupted'


def test_retry_failed_photo_only_once_preserves_human_fields_and_never_saves(chat, services, case, photo, monkeypatch):
    picture = photo(name='唯一测试照片.png', stage='survey', role='problem')
    ai = quality(services, picture)
    with chat.db.tx() as con:
        con.execute("UPDATE photo_ai_tasks SET state='failed' WHERE id=?", (ai['id'],))
    before = chat.db.one('SELECT * FROM photos WHERE id=?', (picture['id'],))
    text = '请重新分析照片 唯一测试照片.png'
    ctx = context(chat, case, text)
    result = call(ctx, 'read_processing_status')
    ref = result['items'][0]['ai']['analysis_ref']
    monkeypatch.setattr(chat.reviews, 'save_for_agent', lambda *a, **k: pytest.fail('Retry saved to website'))
    first = call(ctx, 'retry_photo_analysis', analysis_ref=ref, request_quote=text)
    second = call(ctx, 'retry_photo_analysis', analysis_ref=ref, request_quote=text)
    assert first['state'] == 'queued' and first['analysis_state'] == 'queued'
    assert second['duplicate'] and len(chat.db.all('SELECT * FROM photo_ai_tasks')) == 2
    assert chat.db.one('SELECT * FROM photos WHERE id=?', (picture['id'],)) == before
    assert not chat.db.all('SELECT * FROM platform_review_batches') and not chat.db.all('SELECT * FROM followup_feedback')


@pytest.mark.parametrize('change', ['renamed', 'completed', 'duplicate_name', 'query_only', 'old_quote'])
def test_retry_refuses_stale_successful_ambiguous_or_unrequested_analysis(chat, services, case, photo, change):
    picture = photo(name='待分析.png')
    ai = quality(services, picture)
    with chat.db.tx() as con:
        con.execute("UPDATE photo_ai_tasks SET state=? WHERE id=?", ('completed' if change == 'completed' else 'failed', ai['id']))
    request = '请重新分析照片 待分析.png'
    text = '这张照片分析到了哪一步？' if change in ('query_only', 'old_quote') else request
    ctx = context(chat, case, text)
    value = call(ctx, 'read_processing_status')
    ref = value['items'][0]['ai']['analysis_ref']
    if change == 'renamed':
        with chat.db.tx() as con:
            con.execute('UPDATE photos SET revision=revision+1 WHERE id=?', (picture['id'],))
    if change == 'duplicate_name':
        photo(color='red', name='待分析.png')
    with pytest.raises(DomainError):
        call(ctx, 'retry_photo_analysis', analysis_ref=ref, request_quote=text if change == 'query_only' else request)
    assert len(chat.db.all('SELECT * FROM photo_ai_tasks')) == 1


def test_retry_replacement_rolls_back_with_cancel_and_concurrent_requests_dedupe(chat, services, photo, monkeypatch):
    picture = photo()
    old = quality(services, picture)
    with chat.db.tx() as con:
        con.execute("UPDATE photo_ai_tasks SET state='failed' WHERE id=?", (old['id'],))
    original = services['photo_ai'].enqueue
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('insertion interrupted')
    monkeypatch.setattr(services['photo_ai'], 'enqueue', fail)
    with pytest.raises(RuntimeError):
        services['photo_ai'].retry(old['id'])
    assert chat.db.one('SELECT state FROM photo_ai_tasks WHERE id=?', (old['id'],))['state'] == 'failed'
    assert len(chat.db.all('SELECT * FROM photo_ai_tasks')) == 1
    monkeypatch.setattr(services['photo_ai'], 'enqueue', original)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = list(pool.map(services['photo_ai'].retry, [old['id'], old['id']]))
    assert one['id'] == two['id'] and len(chat.db.all('SELECT * FROM photo_ai_tasks')) == 2


def test_ocr_queue_receipt_is_distinct_from_unprocessed_and_never_claims_completion(chat, services, case, photo):
    picture = photo()
    ctx = context(chat, case)
    before = call(ctx, 'read_processing_status')
    assert before['items'][0]['ocr']['state'] == 'not_recorded'
    services['jobs'].enqueue('recognize', {'photo_ids': [picture['id']]})
    after = call(ctx, 'read_processing_status')
    assert after['items'][0]['ocr']['state'] == 'queued'
    assert after['items'][0]['ai']['state'] == 'not_scheduled'
    assert not after['worker_enabled']


def test_feishu_maintenance_needs_current_mention_and_binding_permission(bot, services, case, platform):
    text = f"请刷新 {case['short_code']} 的官网资料"
    mid = capture(bot, text)
    bot.collect_messages()
    task = task_for(bot, mid)
    ctx = ProjectAgentTools(bot, task)
    task['input']['mentioned'] = False
    before = len(platform.test_requests)
    with pytest.raises(DomainError, match='本次直接'):
        call(ctx, 'refresh_case', case_ref=ctx.case_refs[case['id']], request_quote=text)
    task['input']['mentioned'] = True
    with services['db'].tx() as con:
        con.execute('UPDATE feishu_agent_bindings SET ai_enabled=0')
    with pytest.raises(DomainError, match='关闭 AI'):
        call(ctx, 'refresh_case', case_ref=ctx.case_refs[case['id']], request_quote=text)
    assert len(platform.test_requests) == before


def test_browser_model_can_select_new_read_tools_and_finish_without_mutation(chat, services, case, photo, monkeypatch):
    photo()
    first = send(chat, '查收图处理状态，再看看官网资料差异和具体待办', case_id=case['id'])
    requests = model(chat, monkeypatch, [response(('search_cases', {'query': case['short_code']})),
        response(('read_processing_status', {'case_ref': 'C001'}), ('compare_official_materials', {'case_ref': 'C001'})),
        response(('read_followup_issues', {'case_ref': 'C001'})),
        response(('finish_reply', {'text': '已收到一张归档照片，官网尚未关联；暂无可反馈的具体问题。'}))])
    assert chat.run_one()
    turn = chat.get(KEY, first['thread_id'])['turns'][0]
    assert turn['state'] == 'completed' and all(step['ok'] for step in turn['steps'])
    assert CAPABILITY_NAMES <= {t['function']['name'] for t in requests[0]['tools']}
    assert not chat.db.all('SELECT * FROM agent_maintenance_actions')
    assert turn['sources'][0]['case_id'] == case['id']


def test_group_model_can_use_shared_read_capabilities_without_writes(bot, case, photo, monkeypatch):
    photo()
    mid = capture(bot, f"{case['short_code']} 照片收到后处理到哪一步了，官网还缺什么？")
    bot.collect_messages()
    arrange_model(bot, monkeypatch, [response(('search_cases', {'query': case['short_code']})),
        response(('read_processing_status', {'case_ref': 'C001'}), ('search_photos', {'case_ref': 'C001'}),
                 ('compare_official_materials', {'case_ref': 'C001'})),
        response(('preview_feedback', {})), response(('finish_reply', {'text': '已归档一张照片，官网尚未关联，本次仅预览反馈。'}))])
    assert bot.run_one()
    result = task_for(bot, mid)
    assert result['state'] == 'completed'
    assert all(not step['outcome'].startswith('{"error":') for step in result['steps'])
    assert not bot.db.all('SELECT * FROM platform_drafts')
    assert not bot.db.all('SELECT * FROM agent_maintenance_actions')
    assert not bot.test_client.sent
