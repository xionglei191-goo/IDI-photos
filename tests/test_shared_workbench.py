"""Real HTTP role boundaries for the shared management workbench."""
import json

import pytest

from idi.db import dump, now, uid
from idi.identity import acting_as
from tests.test_project_access import make_person, login_person, png
from tests.test_feishu_agent import bot, capture, response

KEY = 'shared-workbench-browser-key-000000000000'
HEADERS = {'X-IDI-Chat-Key': KEY}


@pytest.fixture
def scoped(app, services, project, case, client):
    private_project = services['domain'].save_project({'name': '绝不显示的乙项目'})
    private_case = services['domain'].save_case({'project_id': private_project['id'], 'title': '乙项目私密维修事项',
                                               'building': '9', 'room': '909', 'platform_report_code': 'ZLBH202610230000000002'})
    services['domain'].save_case({'platform_report_code': 'ZLBH202610230000000001'}, case['id'])
    user = make_person(services, project['id'], 'manager', 'scoped-manager')
    return login_person(app, 'scoped-manager'), user, private_project, private_case


@pytest.fixture
def chat(services):
    services['db'].set_setting('recognition', {'vision_enabled': True, 'base_url': 'https://model.invalid/v1', 'model': 'isolated-test'})
    return services['workspace_chat']


def send(client, **data):
    return client.post('/api/workspace-chat/turns', headers=HEADERS,
                       json={'question': '有哪些项目和案件？', 'request_id': uid(), **data})


def test_manager_read_pages_keep_counts_and_records_within_projects(scoped, services, project, case, photo):
    manager, _, private_project, private_case = scoped
    photo()
    services['importer'].add_file(png(), '乙项目私密照片.png', case_id=private_case['id'])
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair',workflow_source='manual'")
        con.execute('INSERT INTO followup_runs(id,state,result,error,created_at) VALUES(?,?,?,?,?)',
                    (uid(), 'failed', dump({'failed': []}), private_case['title'], now()))
    services['db'].set_setting('platform_lists', {'counts': {'repair': 9999}, 'coverage': 'full', 'private': private_case['title']})
    for path in ('/api/projects', '/api/cases', '/api/summary', '/api/system', '/api/jobs', '/api/operations',
                 '/api/followup', '/api/followup/issues', '/api/followup/feedback', '/api/packages',
                 '/api/workflow/sync', '/api/workflow/board?source=local&board=work', '/api/workflow/uploads'):
        result = manager.get(path)
        assert result.status_code == 200, (path, result.text)
        assert private_project['name'] not in result.text and private_case['title'] not in result.text, path
        assert '9999' not in result.text, path
    assert manager.get('/api/summary').json()['total'] == 1
    overview = manager.get('/api/followup').json()
    assert overview['case_count'] == 1 and [p['id'] for p in overview['projects']] == [project['id']]
    assert overview['latest'] is None
    assert manager.get('/api/operations').json()['ocr']['pending'] == 1
    assert manager.get('/api/access/project-accounts/' + project['id']).status_code == 200
    assert manager.get('/api/access/project-accounts/' + private_project['id']).status_code == 403
    for path, method in (('/api/access/users', 'get'), ('/api/backups', 'get'), ('/api/operations/model/retry', 'post'),
                         ('/api/workflow/sync', 'post'), ('/api/followup/review', 'post')):
        assert getattr(manager, method)(path).status_code == 403, path


def test_manager_packages_upload_jobs_and_receipts_are_scoped(scoped, services, case, photo):
    manager, _, _, private_case = scoped
    mine = photo(stage='survey')
    foreign = services['importer'].add_file(png(), '其他项目照片.png', case_id=private_case['id'], metadata={'stage': 'survey'})['photo_id']
    own_package = services['reporter'].build(uid(), case['id'], [mine['id']])
    other_package = services['reporter'].build(uid(), private_case['id'], [foreign])
    packages = manager.get('/api/packages').json()
    assert [p['id'] for p in packages] == [own_package['id']]
    assert manager.get('/api/packages/' + own_package['id'] + '/download').status_code == 200
    for suffix in ('', '/download', '/document'):
        assert manager.get('/api/packages/' + other_package['id'] + suffix).status_code == 403
    assert manager.post('/api/packages/preview', json={'photo_ids': [mine['id']]}).status_code == 200
    assert manager.post('/api/packages/preview', json={'photo_ids': [mine['id'], foreign]}).status_code == 403
    assert manager.post('/api/packages', json={'photo_ids': [mine['id']], 'parent_id': other_package['id']}).status_code == 403
    uploaded = manager.post('/api/import/upload', data={'case_id': case['id'], 'auto_ocr': 'false'},
                            files={'files': ('自己上传.png', png(), 'image/png')})
    assert uploaded.status_code == 200, uploaded.text
    assert services['jobs'].run_one()
    assert manager.get('/api/imports/' + uploaded.json()['batch_id']).status_code == 200
    assert manager.get('/api/jobs/' + uploaded.json()['job']['id']).status_code == 200


def test_chat_binds_person_and_model_only_receives_authorized_projects(app, scoped, services, chat, monkeypatch, case, project, client):
    manager, user, private_project, private_case = scoped
    first = send(manager)
    assert first.status_code == 200, first.text
    thread_id = first.json()['thread_id']
    stored = services['db'].one('SELECT * FROM workspace_chat_threads WHERE id=?', (thread_id,))
    assert stored['user_id'] == user['id'] and json.loads(stored['project_ids']) == [project['id']]
    calls = []
    replies = iter([response(('list_projects', {})), response(text='可查看当前获授权的项目。')])
    monkeypatch.setattr(chat, 'request_model', lambda messages, *_: calls.append(dump(messages)) or next(replies))
    assert chat.run_one()
    result = manager.get('/api/workspace-chat/threads/' + thread_id, headers=HEADERS)
    assert result.status_code == 200 and result.json()['turns'][0]['state'] == 'completed'
    assert calls and all(private_project['name'] not in c and private_case['title'] not in c for c in calls)
    assert send(manager, project_id=private_project['id']).status_code == 403
    assert send(manager, case_id=private_case['id']).status_code == 403
    assert manager.get('/api/workspace-chat/memory', headers=HEADERS, params={'project_id': private_project['id']}).status_code == 403
    make_person(services, project['id'], 'manager', 'second-manager')
    second = login_person(app, 'second-manager')
    assert second.get('/api/workspace-chat', headers=HEADERS).json()['threads'] == []
    assert second.get('/api/workspace-chat/threads/' + thread_id, headers=HEADERS).status_code == 404
    assert client.get('/api/workspace-chat/threads/' + thread_id, headers=HEADERS).status_code == 404


@pytest.mark.parametrize('when', ['queued', 'during_model'])
def test_chat_rechecks_revoked_scope_before_and_after_model(scoped, services, project, chat, monkeypatch, when):
    manager, user, private_project, _ = scoped
    # Retain a second management assignment so access to the shared UI stays on.
    services['access'].save_members(private_project['id'], {'members': [{'user_id': user['id'], 'role': 'manager'}]})
    first = send(manager, project_id=project['id']).json()
    calls = []
    def revoke():
        services['access'].save_members(project['id'], {'members': []})
    def model(*_):
        calls.append(True)
        # The model transport is outside the request identity; simulate admin revocation.
        with acting_as(None):
            revoke()
        return response(text='这条内容必须被权限复核拦下。')
    monkeypatch.setattr(chat, 'request_model', model)
    if when == 'queued':
        revoke()
    assert chat.run_one()
    row = services['db'].one('SELECT state,answer,error FROM workspace_chat_turns WHERE id=?', (first['turn_id'],))
    assert row['state'] == 'failed' and row['answer'] == '' and '权限' in row['error']
    assert len(calls) == (0 if when == 'queued' else 1)
    assert manager.get('/api/workspace-chat', headers=HEADERS).json()['threads'] == []
    assert manager.get('/api/workspace-chat/threads/' + first['thread_id'], headers=HEADERS).status_code == 403
    assert manager.get('/api/workspace-chat/memory', headers=HEADERS, params={'thread_id': first['thread_id']}).status_code == 403


def test_manager_cannot_recover_a_previous_admin_browser_thread(scoped, chat, client, services, monkeypatch):
    manager, _, private_project, _ = scoped
    first = send(client, project_id=private_project['id']).json()
    monkeypatch.setattr(chat, 'request_model', lambda *_: response(text='管理员的其他项目答复'))
    chat.run_one()
    assert manager.get('/api/workspace-chat/threads/' + first['thread_id'], headers=HEADERS).status_code == 404
    assert manager.get('/api/workspace-chat', headers=HEADERS).json()['threads'] == []


def test_feishu_records_require_the_entire_frozen_project_scope(scoped, services, project, bot):
    manager, _, private_project, _ = scoped
    own_mid = capture(bot, '本项目可见问答')
    bot.collect_messages()
    own = services['db'].one('SELECT id FROM feishu_agent_tasks WHERE source_message_id=?', (own_mid,))['id']
    mixed_mid = capture(bot, '多项目会话私密内容')
    bot.collect_messages()
    mixed = services['db'].one('SELECT id,input_json FROM feishu_agent_tasks WHERE source_message_id=?', (mixed_mid,))
    with services['db'].tx() as con:
        data = json.loads(mixed['input_json'])
        data['allowed_project_ids'] = [project['id'], private_project['id']]
        con.execute('UPDATE feishu_agent_tasks SET input_json=? WHERE id=?', (dump(data), mixed['id']))
    status = manager.get('/api/feishu-agent')
    assert status.status_code == 200, status.text
    assert [t['id'] for t in status.json()['tasks']] == [own]
    assert '多项目会话私密内容' not in status.text
    assert manager.get('/api/feishu-agent/tasks/' + mixed['id']).status_code == 404
    assert manager.get('/api/feishu-agent/tasks/' + own).status_code == 200
    sessions = manager.get('/api/feishu-agent/sessions').json()
    assert sessions['total'] == 1 and sessions['sessions'][0]['turn_count'] == 1
    transcript = manager.get('/api/feishu-agent/sessions/' + sessions['sessions'][0]['id'])
    assert transcript.status_code == 200 and '多项目会话私密内容' not in transcript.text


def test_case_review_queue_stops_after_manager_assignment_removed(scoped, services, project, case, photo, chat, monkeypatch):
    manager, _, _, private_case = scoped
    photo()
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair',workflow_source='manual' WHERE id=?", (case['id'],))
    agent = services['case_agent']
    queued = manager.post('/api/agent/cases/' + case['id'] + '/review', json={})
    assert queued.status_code == 200, queued.text
    assert manager.get('/api/agent/cases/' + private_case['id']).status_code == 403
    services['access'].save_members(project['id'], {'members': []})
    monkeypatch.setattr(agent, '_request_model', lambda *_: pytest.fail('Revoked task sent a model request'))
    assert agent.run_one()
    row = services['db'].one('SELECT state,error FROM case_agent_runs WHERE id=?', (queued.json()['id'],))
    assert row['state'] == 'failed' and '权限' in row['error']


def test_membership_changes_hide_old_jobs_and_member_only_project_stays_read_only(scoped, services, project, case):
    manager, user, private_project, private_case = scoped
    services['access'].save_members(private_project['id'], {'members': [{'user_id': user['id'], 'role': 'member'}]})
    plan = manager.get('/api/platform/cases/' + private_case['id'] + '/stages/intake')
    assert plan.status_code == 200, plan.text
    assert not plan.json()['access']['editable']
    uploaded = manager.post('/api/import/upload', data={'case_id': case['id'], 'auto_ocr': 'false'}, files={'files': ('原项目.png', png(), 'image/png')}).json()
    services['access'].save_members(project['id'], {'members': []})
    assert manager.get('/api/jobs').json() == []
    assert manager.get('/api/jobs/' + uploaded['job']['id']).status_code == 403
    assert manager.get('/api/imports/' + uploaded['batch_id']).status_code == 403
    assert manager.patch('/api/cases/' + private_case['id'], json={'title': '越权修改'}).status_code == 403
