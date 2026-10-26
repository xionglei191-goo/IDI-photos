import json

import pytest

from idi.context_memory import bounded_tool_result, token_estimate
from idi.db import now, uid
from idi.domain import DomainError
from tests.conftest import image_bytes
from tests.test_agent_capabilities import context, call
from tests.test_workspace_chat import chat, send, model, KEY
from tests.test_feishu_agent import response


def test_joins_real_case_instead_of_filename_and_reads_current_stage(chat, services, case, photo):
    picture = photo(name='其他项目1栋3004玻璃爆裂验收.jpg', stage='acceptance', role='finished')
    ctx = context(chat, case)
    with chat.db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='acceptance',workflow_source='official',workflow_verified_at=? WHERE id=?", (now(), case['id']))
    before = chat.db.one('SELECT * FROM photos WHERE id=?', (picture['id'],))
    output = call(ctx, 'read_photo_case_summary', stage='acceptance')
    assert output['totals']['photo_count'] == output['totals']['case_count'] == 1
    row = output['items'][0]
    assert row['title'] == case['title'] and row['short_code'] == case['short_code']
    assert row['workflow_stage'] == 'acceptance' and row['stage_label'] == '待验收'
    assert row['workflow_verified_at'] and not row['live_official_query']
    assert '3004' not in json.dumps(output, ensure_ascii=False)
    assert chat.db.one('SELECT * FROM photos WHERE id=?', (picture['id'],)) == before


def test_totals_separate_photographs_cases_stages_and_unassigned(chat, services, case, photo):
    photo(color='red', stage='acceptance', role='finished')
    photo(color='blue', stage='acceptance', role='finished')
    photo(color='green', stage='survey', role='problem')
    second = services['domain'].save_case({'project_id': case['project_id'], 'title': '仍在施工'})
    services['importer'].add_file(image_bytes('yellow'), '施工案验收.png', case_id=second['id'], metadata={'stage': 'acceptance', 'role': 'finished'})
    services['importer'].add_file(image_bytes('purple'), '未归案.png', project_id=case['project_id'], metadata={'stage': 'acceptance', 'role': 'finished'})
    with chat.db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='acceptance' WHERE id=?", (case['id'],))
        con.execute("UPDATE cases SET workflow_stage='repair' WHERE id=?", (second['id'],))
    ctx = context(chat, case)
    output = call(ctx, 'read_photo_case_summary', photo_group='completion', page_size=1)
    assert output['totals'] == {'photo_count': 4, 'case_count': 2, 'unassigned_photo_count': 1, 'stages': [
        {'workflow_stage': 'repair', 'stage_label': '施工中', 'case_count': 1, 'photo_count': 1},
        {'workflow_stage': 'acceptance', 'stage_label': '待验收', 'case_count': 1, 'photo_count': 2}]}
    second_page = call(ctx, 'read_photo_case_summary', photo_group='completion', page=2, page_size=1)
    assert output['has_more'] and not second_page['has_more']
    assert output['totals'] == second_page['totals'] and output['items'][0]['case_ref'] != second_page['items'][0]['case_ref']


def test_scope_current_membership_and_date_filters(chat, services, case, photo):
    picture = photo(stage='acceptance', role='finished')
    other = services['domain'].save_project({'name': '不能读取的项目'})
    services['importer'].add_file(image_bytes('red'), '外部照片.png', project_id=other['id'], metadata={'stage': 'acceptance'})
    services['importer'].add_file(image_bytes('blue'), '无项目照片.png', metadata={'stage': 'acceptance'})
    ctx = context(chat, case)
    with chat.db.tx() as con:
        con.execute("UPDATE photos SET created_at='2026-09-30T16:01:00.000+00:00' WHERE id=?", (picture['id'],))
    assert call(ctx, 'read_photo_case_summary', date_from='2026-10-01', date_to='2026-10-01')['totals']['photo_count'] == 1
    assert call(ctx, 'read_photo_case_summary', date_from='2026-10-02')['totals']['photo_count'] == 0
    with chat.db.tx() as con:
        con.execute('UPDATE cases SET project_id=? WHERE id=?', (other['id'], case['id']))
    assert call(ctx, 'read_photo_case_summary')['totals']['photo_count'] == 0


def test_counts_every_photo_beyond_domain_page_limit(chat, services, case, photo):
    picture = photo(stage='acceptance', role='finished')
    blob = chat.db.one('SELECT * FROM blobs WHERE hash=?', (picture['blob_hash'],))
    with chat.db.tx() as con:
        for _ in range(504):
            copied = {**blob, 'hash': uid() + uid()}
            con.execute('INSERT INTO blobs(' + ','.join(copied) + ') VALUES(' + ','.join('?' for _ in copied) + ')', tuple(copied.values()))
            con.execute('INSERT INTO photos(id,blob_hash,project_id,case_id,stage,role,photo_group,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        (uid(), copied['hash'], case['project_id'], case['id'], 'acceptance', 'finished', 'completion', now(), now()))
    output = call(context(chat, case), 'read_photo_case_summary', stage='acceptance')
    assert output['totals']['photo_count'] == output['items'][0]['photo_count'] == 505
    assert output['totals']['case_count'] == 1 and output['totals_complete']


def test_case_pages_preserve_complete_totals_and_fit_transport_budget(chat, services, case, photo):
    picture = photo(stage='acceptance', role='finished')
    for i in range(34):
        added = services['domain'].save_case({'project_id': case['project_id'], 'title': ('很长的案件说明' * 70) + str(i), 'building': '第二栋', 'room': str(i)})
        with chat.db.tx() as con:
            con.execute('INSERT INTO photos(id,blob_hash,project_id,case_id,stage,role,photo_group,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        (uid(), picture['blob_hash'], case['project_id'], added['id'], 'acceptance', 'finished', 'completion', now(), now()))
    ctx, args, seen = context(chat, case), {'stage': 'acceptance', 'page_size': 50}, []
    while True:
        output = call(ctx, 'read_photo_case_summary', **args)
        assert output['totals']['case_count'] == output['totals']['photo_count'] == 35
        assert token_estimate(output) < 7000 and 'context_limit' not in json.loads(bounded_tool_result(output))
        seen.extend(row['case_ref'] for row in output['items'])
        if not output['has_more']:
            break
        args.update(page=output['next_page'], page_size=output['page_size'])
    assert len(seen) == len(set(seen)) == 35


@pytest.mark.parametrize('args', [{'stage': 'wrong'}, {'page': True}, {'page_size': 51}, {'case_ref': 'C999'}, {'project_ref': 'J999'}])
def test_photo_case_query_validates_arguments(chat, case, args):
    with pytest.raises(DomainError):
        call(context(chat, case), 'read_photo_case_summary', **args)


@pytest.mark.parametrize('finish_tool', [False, True])
def test_cross_stage_answer_requires_current_join_even_after_photo_search(chat, case, photo, monkeypatch, finish_tool):
    photo(name='误导的1栋3004查勘照片.png', stage='acceptance', role='finished')
    first = send(chat, '完工验收是指有验收阶段的照片，但是还没有进入待验收阶段', project_id=case['project_id'])
    unsupported = response(('finish_reply', {'text': '1栋3004处于施工中。'})) if finish_tool else response(text='1栋3004处于施工中。')
    requests = model(chat, monkeypatch, [response(('search_photos', {'stage': 'acceptance'})), unsupported,
        response(('read_photo_case_summary', {'stage': 'acceptance'})), response(text='旧名单里的3004、1906都在施工中，其他案件也都是历史待核实。')])
    chat.run_one()
    turn = chat.get(KEY, first['thread_id'])['turns'][0]
    assert turn['state'] == 'completed'
    assert '1栋3004处于施工中' not in turn['answer']
    assert '3004' not in turn['answer'] and '1906' not in turn['answer'] and '历史待核实' not in turn['answer']
    assert '待维保受理 1 案/1 张' in turn['answer']
    assert 'read_photo_case_summary' in json.dumps(requests[2], ensure_ascii=False)
    assert turn['sources'][0]['case_id'] == case['id']


def test_cross_stage_summary_requires_acceptance_photo_filter(chat, case, photo, monkeypatch):
    photo(stage='survey', role='problem')
    first = send(chat, '哪些案件已有验收照片但还没有进入待验收？', project_id=case['project_id'])
    requests = model(chat, monkeypatch, [response(('read_photo_case_summary', {})), response(text='一案符合。'),
        response(('read_photo_case_summary', {'stage': 'acceptance'})), response(text='没有符合案件。')])
    chat.run_one()
    turn = chat.get(KEY, first['thread_id'])['turns'][0]
    assert turn['state'] == 'completed' and '共 0 案' in turn['answer']
    assert 'stage=acceptance' in json.dumps(requests[2], ensure_ascii=False)


def test_repeated_unverified_answers_never_publish_false_case_stage(chat, case, monkeypatch):
    first = send(chat, '哪些案件有验收照片但未进入待验收？', project_id=case['project_id'])
    model(chat, monkeypatch, [response(text='虚构房号仍在查勘。')] * 12)
    chat.run_one()
    turn = chat.get(KEY, first['thread_id'])['turns'][0]
    assert turn['state'] == 'failed' and not turn['answer']
