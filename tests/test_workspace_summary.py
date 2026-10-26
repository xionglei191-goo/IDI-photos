"""Regression coverage for complete project totals and refreshed chat scopes."""
import json

import pytest

from idi.context_memory import bounded_tool_result, token_estimate
from idi.db import dump, now, uid
from idi.domain import DomainError
from idi.workspace_chat import WorkspaceTools
from idi.workspace_scope import all_project_scope, current_thread_projects
from tests.test_feishu_agent import response
from tests.test_workspace_chat import chat, send, model, KEY


def seed_projects(chat, counts, *, long_names=False):
    projects, stamp, run_id = [], now(), uid()
    offset = chat.db.one('SELECT COUNT(*) n FROM projects')['n']
    with chat.db.tx() as con:
        con.execute('INSERT INTO platform_sync_runs(id,state,started_at,finished_at) VALUES(?,?,?,?)',
                    (run_id, 'completed', stamp, stamp))
        for index, (total, eligible) in enumerate(counts):
            project_id = uid()
            name = f'项目{offset + index + 1:03d}' + ('很长的中文项目名称' * 12 if long_names else '')
            con.execute('INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)', (project_id, name, stamp, stamp))
            projects.append({'id': project_id, 'name': name, 'total': total, 'eligible': eligible})
            for n in range(total):
                case_id = uid()
                con.execute('INSERT INTO cases(id,project_id,title,workflow_source,workflow_stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                            (case_id, project_id, '案件详细描述' * 30, 'platform', 'repair', stamp, stamp))
                if n < eligible:
                    con.execute('INSERT INTO platform_list_entries VALUES(?,?,?,?,?,?,?)',
                                ('repair', case_id, case_id, n, '{}', run_id, stamp))
                # Long event details previously caused the overview's entire
                # project array to be halved by the generic context limiter.
                if n < 8:
                    con.execute('INSERT INTO case_events(id,case_id,kind,from_stage,to_stage,source,event_at,created_at) VALUES(?,?,?,?,?,?,?,?)',
                                (uid(), case_id, 'workflow', 'survey', 'repair', 'platform', stamp, stamp))
    return projects


def running_context(chat):
    first = send(chat, '汇总所有项目的查勘维修情况')
    with chat.db.tx() as con:
        con.execute("UPDATE workspace_chat_turns SET state='running' WHERE id=?", (first['turn_id'],))
    task = chat.db.one('SELECT * FROM workspace_chat_turns WHERE id=?', (first['turn_id'],))
    thread = chat.db.one('SELECT * FROM workspace_chat_threads WHERE id=?', (first['thread_id'],))
    task.update(project_ids=thread['project_ids'], context_case_id=None, project_id=None,
                kind='chat', input={'text': task['question']}, source_message_id='', chat_id='')
    return WorkspaceTools(chat, task)


def test_old_all_project_thread_includes_new_projects_and_full_summary(chat, monkeypatch):
    # Same shape as the production incident: seven original projects, then two
    # additions; four original and two new projects contain repair-list cases.
    original = seed_projects(chat, [(1, 0), (1, 0), (2, 0), (54, 23), (45, 15), (78, 38), (295, 99)])
    first = send(chat, '怎样查询案件编号？')
    model(chat, monkeypatch, [response(text='旧的错误答复只统计两个项目。')])
    assert chat.run_one()
    added = seed_projects(chat, [(25, 25), (29, 29)])
    send(chat, '为什么不是全部项目？', thread_id=first['thread_id'])
    requests = model(chat, monkeypatch, [response(('read_followup_summary', {})),
                                         response(text='已重新核对全部项目的查勘维修记录。')])
    assert chat.run_one()
    initial = json.loads(next(item['content'] for item in requests[0]['messages']
                              if item['role'] == 'user' and '"scope_totals"' in item['content']))
    assert initial['scope_totals'] == {'project_count': 9, 'followup_project_count': 6, 'case_count': 229, 'all_case_count': 530}
    assert sorted(row['followup_case_count'] for row in initial['projects']) == [0, 0, 0, 15, 23, 25, 29, 38, 99]
    tool = json.loads(next(item['content'] for item in requests[1]['messages'] if item['role'] == 'tool'))
    assert 'context_limit' not in tool and token_estimate(tool) < 7000
    assert tool['timezone'] == 'Asia/Shanghai' and tool['queried_at'].endswith('+08:00')
    assert tool['totals_complete'] and not tool['has_more']
    assert tool['totals']['project_count'] == 9
    assert tool['totals']['followup_project_count'] == 6
    assert tool['totals']['case_count'] == 229 and tool['totals']['all_case_count'] == 530
    assert tool['totals']['excluded_case_count'] == 301
    assert tool['totals']['stages'] == {'施工中': 229}
    assert len(tool['projects']) == 9
    assert sorted(row['case_count'] for row in tool['projects']) == [0, 0, 0, 15, 23, 25, 29, 38, 99]
    assert 'recent_recorded_changes' not in dump(tool)
    stored = chat.db.one('SELECT * FROM workspace_chat_threads WHERE id=?', (first['thread_id'],))
    expected = {project['id'] for project in original + added}
    assert set(json.loads(stored['project_ids'])) == expected
    summary = chat.db.one('SELECT project_ids FROM agent_context_summaries WHERE thread_id=?', (first['thread_id'],))
    assert set(json.loads(summary['project_ids'])) == expected
    answer = chat.get(KEY, first['thread_id'])['turns'][-1]['answer']
    assert '共 9 个项目，其中 6 个项目、229 个案件' in answer
    assert len(chat.get(KEY, first['thread_id'])['turns']) == 2


def test_many_large_project_rows_are_paged_without_losing_totals(chat):
    seed_projects(chat, [(12, 10)] * 95, long_names=True)
    ctx = running_context(chat)
    args, seen, pages = {'page_size': 50}, set(), 0
    while True:
        result, images = ctx.execute('read_followup_summary', args)
        assert not images
        transported = json.loads(bounded_tool_result(result))
        assert 'context_limit' not in transported and token_estimate(transported) <= 6000
        assert result['totals']['project_count'] == 95 and result['totals']['case_count'] == 950
        assert result['totals']['all_case_count'] == 1140 and result['totals_complete']
        refs = {row['project_ref'] for row in result['projects']}
        assert not seen & refs
        seen.update(refs); pages += 1
        if not result['has_more']:
            assert result['next_page'] is None
            break
        args = {'page': result['next_page'], 'page_size': result['page_size']}
    assert len(seen) == 95 and pages > 1
    last, _ = ctx.execute('read_followup_summary', {'page': 10000, 'page_size': args['page_size']})
    assert not last['has_more'] and last['projects']


@pytest.mark.parametrize('args', [{'page': 0}, {'page': True}, {'page_size': 0}, {'page_size': 51}, {'project_id': 'outside'}, []])
def test_followup_summary_rejects_invalid_pagination(chat, project, args):
    ctx = running_context(chat)
    with pytest.raises(DomainError):
        ctx.execute('read_followup_summary', args)
    assert ctx.followup_totals is None


def test_no_followup_cases_are_still_visible_as_zero_count_projects(chat):
    seed_projects(chat, [(3, 0), (0, 0)])
    ctx = running_context(chat)
    value, _ = ctx.execute('read_followup_summary', {})
    assert value['total'] == 2 and len(value['projects']) == 2
    assert value['totals']['case_count'] == value['totals']['followup_project_count'] == 0
    assert value['totals']['all_case_count'] == 3
    assert all(row['case_count'] == 0 for row in value['projects'])


def test_missing_selected_project_never_becomes_all_projects(chat, project):
    thread = {'project_id': None, 'case_id': None, 'context_label': '原先明确选择的项目', 'project_ids': dump(['removed'])}
    assert not all_project_scope(thread)
    assert current_thread_projects(chat.db, thread) == ['removed']
