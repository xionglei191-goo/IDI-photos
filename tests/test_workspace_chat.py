import json
from copy import deepcopy

import pytest

from idi.db import Database, dump, uid
from idi.domain import DomainError
from tests.test_feishu_agent import response

KEY = 'a' * 48
OTHER = 'b' * 48


@pytest.fixture
def chat(services):
    services['db'].set_setting('recognition', {'vision_enabled': True, 'base_url': 'https://model.invalid/v1', 'model': 'chat-test'})
    return services['workspace_chat']


def send(chat, text='查询案件进展', key=KEY, **kwargs):
    return chat.send(key, {'question': text, 'request_id': uid(), **kwargs})


def model(chat, monkeypatch, replies):
    values, requests = iter(replies), []
    def request(messages, definitions, timeout):
        requests.append({'messages': deepcopy(messages), 'tools': deepcopy(definitions)})
        return next(values)
    monkeypatch.setattr(chat, 'request_model', request)
    return requests


def test_native_tools_project_scope_history_and_case_links(chat, services, project, case, monkeypatch):
    other = services['domain'].save_project({'name': '另一个项目'})
    services['domain'].save_case({'project_id': other['id'], 'title': '不应出现在该对话的案件', 'report_number': 'PRIVATE-987'})
    first = send(chat, case['short_code'] + ' 目前进展如何？', case_id=case['id'])
    requests = model(chat, monkeypatch, [response(('search_cases', {'query': case['short_code']})), response(('read_case', {'case_ref': 'C001'})),
        response(('finish_reply', {'text': 'C001 台账显示待维保受理。'})), response(('read_case_records', {'case_ref': 'C001'})),
        response(('finish_reply', {'text': '暂未查询到进场记录，请核对现存资料。'}))])
    assert chat.run_one()
    thread = chat.get(KEY, first['thread_id'])
    assert thread['turns'][0]['state'] == 'completed'
    assert thread['turns'][0]['sources'][0]['case_id'] == case['id']
    assert case['short_code'] in thread['turns'][0]['answer'] and 'C001' not in thread['turns'][0]['answer']
    assert 'PRIVATE-987' not in dump(requests)
    send(chat, '那它有进场记录吗？', thread_id=first['thread_id'])
    assert chat.run_one()
    assert len(chat.get(KEY, first['thread_id'])['turns']) == 2
    assert any('目前进展如何' in m.get('content', '') for m in requests[3]['messages'] if isinstance(m.get('content'), str))
    assert not chat.db.all('SELECT * FROM feishu_agent_tasks')
    assert not chat.db.all('SELECT * FROM platform_drafts')


def test_browser_conversations_are_isolated_and_scope_is_frozen(chat, project, case, services, monkeypatch):
    first = send(chat, '仅属于甲浏览器的对话内容')
    model(chat, monkeypatch, [response(text='你好，请提供案件编号。')])
    chat.run_one()
    assert len(chat.status(KEY)['threads']) == 1
    assert chat.status(OTHER)['threads'] == []
    with pytest.raises(DomainError, match='不属于'):
        chat.get(OTHER, first['thread_id'])
    with pytest.raises(DomainError, match='不属于'):
        send(chat, key=OTHER, thread_id=first['thread_id'])
    with pytest.raises(DomainError, match='不属于'):
        chat.cancel(OTHER, first['turn_id'])
    services['domain'].save_project({'name': '对话建立后新增的项目'})
    send(chat, '列出项目', thread_id=first['thread_id'])
    requests = model(chat, monkeypatch, [response(('list_projects', {})), response(text='当前对话范围中有一个项目。')])
    chat.run_one()
    assert '对话建立后新增的项目' not in dump(requests)
    second = send(chat, '新的独立话题', key=OTHER, project_id=project['id'])
    requests = model(chat, monkeypatch, [response(text='你好。')])
    chat.run_one()
    assert '仅属于甲浏览器' not in dump(requests)
    assert len(chat.get(OTHER, second['thread_id'])['turns']) == 1


@pytest.mark.parametrize('name,args', [
    ('save_official_draft', {'case_ref': 'C001'}), ('prepare_official_draft', {'case_ref': 'C001'}),
    ('organize_image', {'photo_ref': 'P001'}), ('submit', {}), ('send_feedback', {}),
    ('read_case', {'case_ref': 'C001', 'path': 'C:/secret'}), ('read_case', {'case_ref': 'C999'}),
])
def test_unavailable_tools_and_extra_arguments_never_write(chat, case, monkeypatch, name, args):
    first = send(chat, '请帮我直接修改并提交官网', case_id=case['id'])
    requests = model(chat, monkeypatch, [response((name, args)), response(text='网页对话没有这项操作权限，请打开案件页面核对。')])
    monkeypatch.setattr(chat.platform.client, 'upload', lambda *args: pytest.fail('Chat uploaded a file'))
    monkeypatch.setattr(chat.reviews, 'save_for_agent', lambda *args: pytest.fail('Chat saved a draft'))
    chat.run_one()
    result = chat.get(KEY, first['thread_id'])['turns'][0]
    assert result['state'] == 'completed' and not result['steps'][0]['ok']
    names = {v['function']['name'] for v in requests[0]['tools']}
    assert not names & {'save_official_draft', 'organize_image', 'prepare_official_draft', 'submit'}
    assert not chat.db.all('SELECT * FROM platform_drafts')
    assert not chat.db.all('SELECT * FROM feishu_agent_outbox')


def test_read_original_images_and_keep_business_data_unchanged(chat, case, photo, monkeypatch):
    original = photo()
    before = chat.db.one('SELECT * FROM photos WHERE id=?', (original['id'],))
    first = send(chat, '请查看本案照片，说明能看到的内容', case_id=case['id'])
    requests = model(chat, monkeypatch, [response(('read_case', {'case_ref': 'C001'})), response(('read_images', {'photo_refs': ['P001']})),
        response(text='仅查看了本案1张照片，图中为纯色区域，无法判断工程问题。')])
    chat.run_one()
    result = chat.get(KEY, first['thread_id'])['turns'][0]
    assert result['state'] == 'completed' and all(v['ok'] for v in result['steps'])
    assert '实际查看 1 张已归档原图' in result['answer'] and '不能代替完工或验收认定' in result['answer']
    assert 'data:image/jpeg;base64,' in dump(requests[2])
    assert chat.db.one('SELECT * FROM photos WHERE id=?', (original['id'],)) == before
    assert not chat.db.all('SELECT * FROM feishu_agent_images')


def test_original_hash_failure_is_not_passed_to_model_as_an_image(chat, case, photo, monkeypatch):
    original = photo()
    blob = chat.db.one('SELECT * FROM blobs WHERE hash=?', (original['blob_hash'],))
    chat.storage.resolve(blob['stored_path']).write_bytes(b'corrupt test copy')
    first = send(chat, case_id=case['id'])
    requests = model(chat, monkeypatch, [response(('read_case', {'case_ref': 'C001'})), response(('read_images', {'photo_refs': ['P001']})), response(text='原图校验失败，无法查看。')])
    chat.run_one()
    result = chat.get(KEY, first['thread_id'])['turns'][0]
    assert not result['steps'][1]['ok']
    assert 'base64,' not in dump(requests)


def test_followup_summary_excludes_intake_only_cases(chat, services, project, case, monkeypatch):
    services['domain'].save_case({'project_id': project['id'], 'title': '仍在受理的另一案'})
    with chat.db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='survey' WHERE id=?", (case['id'],))
    first = send(chat, '汇总查勘维修进展', project_id=project['id'])
    requests = model(chat, monkeypatch, [response(('read_followup_summary', {})), response(text='跟进范围内有1宗查勘案件。')])
    chat.run_one()
    output = json.loads(next(v['content'] for v in requests[1]['messages'] if v['role'] == 'tool'))
    assert output['projects'][0]['case_count'] == 1 and output['projects'][0]['all_case_count'] == 2
    assert chat.get(KEY, first['thread_id'])['turns'][0]['state'] == 'completed'


def test_repeated_request_is_deduplicated_and_pending_turn_blocks_double_send(chat, project):
    payload = {'question': '你好', 'request_id': uid(), 'project_id': project['id']}
    first = chat.send(KEY, payload)
    assert chat.send(KEY, payload) == first
    with pytest.raises(DomainError, match='另一个问题'):
        chat.send(KEY, {**payload, 'question': '换一个问题'})
    with pytest.raises(DomainError, match='另一个对话范围'):
        chat.send(KEY, {**payload, 'project_id': None})
    with pytest.raises(DomainError, match='仍在处理'):
        send(chat, '新的问题')
    assert len(chat.db.all('SELECT * FROM workspace_chat_turns')) == 1
    assert len(chat.db.all('SELECT * FROM workspace_chat_threads')) == 1


def test_cancel_during_model_request_discards_late_answer(chat, case, monkeypatch):
    first = send(chat, case_id=case['id'])
    def request(*args):
        chat.cancel(KEY, first['turn_id'])
        return response(text='这个答复不应保存')
    monkeypatch.setattr(chat, 'request_model', request)
    chat.run_one()
    result = chat.get(KEY, first['thread_id'])['turns'][0]
    assert result['state'] == 'cancelled' and result['answer'] == ''
    send(chat, '可以继续问了吗', thread_id=first['thread_id'])


@pytest.mark.parametrize('failure', ['length', 'content_filter', 'invalid', 'budget'])
def test_incomplete_or_invalid_responses_stay_failed(chat, project, monkeypatch, failure):
    first = send(chat, project_id=project['id'])
    reply = response(text='不能当作完整答案的文字')
    if failure in ('length', 'content_filter'):
        reply['choices'][0]['finish_reason'] = failure
    elif failure == 'budget':
        reply['usage']['prompt_tokens'] = 999999
    else:
        reply = {'unrecognized': True}
    model(chat, monkeypatch, [reply])
    chat.run_one()
    result = chat.get(KEY, first['thread_id'])['turns'][0]
    assert result['state'] == 'failed' and result['error'] and not result['answer']


def test_disabling_model_mid_request_stops_answer(chat, project, monkeypatch):
    first = send(chat, project_id=project['id'])
    def request(*args):
        chat.db.set_setting('recognition', {'vision_enabled': False})
        return response(text='不能发布的答复')
    monkeypatch.setattr(chat, 'request_model', request)
    chat.run_one()
    assert chat.get(KEY, first['thread_id'])['turns'][0]['state'] == 'failed'
    assert not chat.status(KEY)['ready']


def test_recovery_and_backup_restore_do_not_replay_running_questions(chat, services, project, tmp_path):
    from idi.cli import restore
    first = send(chat, project_id=project['id'])
    with chat.db.tx() as con:
        con.execute("UPDATE workspace_chat_turns SET state='running'")
    chat.recover()
    assert chat.get(KEY, first['thread_id'])['turns'][0]['state'] == 'failed'
    second = send(chat, '排队中的问题', thread_id=first['thread_id'])
    backup = services['reporter'].backup({}, lambda *args: None)
    target = tmp_path / 'restored-chat'
    restore(services['settings'].data_dir / 'backups' / backup['filename'], target)
    db = Database(target / 'archive.sqlite3')
    assert db.one('SELECT state FROM workspace_chat_turns WHERE id=?', (second['turn_id'],))['state'] == 'cancelled'


def test_api_owner_header_scope_validation_and_model_readiness(client, chat, project, case):
    assert client.get('/api/workspace-chat').status_code == 400
    headers = {'X-IDI-Chat-Key': KEY}
    status = client.get('/api/workspace-chat', headers=headers)
    assert status.status_code == 200 and status.json()['ready']
    invalid = client.post('/api/workspace-chat/turns', headers=headers, json={'question': '你好', 'request_id': uid(), 'submit': True})
    assert invalid.status_code == 400
    invalid = client.post('/api/workspace-chat/turns', headers=headers, json={'question': '你好', 'request_id': uid(), 'project_id': ['bad']})
    assert invalid.status_code == 400
    created = client.post('/api/workspace-chat/turns', headers=headers, json={'question': '你好', 'request_id': uid(), 'case_id': case['id']})
    assert created.status_code == 200
    assert client.get('/api/workspace-chat/threads/' + created.json()['thread_id'], headers={'X-IDI-Chat-Key': OTHER}).status_code == 404
    assert client.post('/api/workspace-chat/turns/' + created.json()['turn_id'] + '/cancel', headers=headers).status_code == 200
    chat.db.set_setting('recognition', {'vision_enabled': False})
    disabled = client.post('/api/workspace-chat/turns', headers=headers, json={'question': '你好', 'request_id': uid()})
    assert disabled.status_code == 409
