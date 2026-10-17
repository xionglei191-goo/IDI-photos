import json

import pytest

from idi.case_agent import MAX_STEPS
from idi.db import dump, now, uid
from idi.domain import DomainError


@pytest.fixture
def case(case, services):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair',revision=revision+1 WHERE id=?", (case['id'],))
    return services['domain'].case(case['id'])


@pytest.fixture
def agent(services):
    services['db'].set_setting('recognition', {'vision_enabled': True, 'base_url': 'https://example.invalid/v1', 'model': 'test-vision', 'local_ocr': False})
    return services['case_agent']


def response(*calls, finish='tool_calls'):
    return {'choices': [{'finish_reason': finish, 'message': {'content': None, 'tool_calls': [
        {'id': 'call_' + str(i), 'type': 'function', 'function': {'name': name, 'arguments': dump(args)}} for i, (name, args) in enumerate(calls)]}}],
        'usage': {'prompt_tokens': 80, 'completion_tokens': 30, 'total_tokens': 110}}


def finding(photo_ids, **extra):
    return {'kind': 'visible_issue', 'certainty': 'clear', 'title': '维修主体被前景遮挡',
            'detail': '照片中的维修部位被前景物体遮挡，无法核对维修事项对应的细节。',
            'action': '请调整拍摄位置，补拍无遮挡的维修部位近照，保留周边定位信息。',
            'photo_ids': photo_ids, 'message_ids': [], **extra}


def finished(photo_ids, findings=None, **extra):
    return {'summary': '已结合案件记录核对照片，存在需要进一步核对的可见问题。',
            'limitations': ['照片不能替代现场验收。'], 'findings': findings if findings is not None else [finding(photo_ids)], **extra}


def prepare(agent, monkeypatch, photos, result=None, extra_responses=None):
    frames = [response(('read_case', {}), ('read_records', {}), ('read_requirements', {}))]
    frames += [response(('read_photos', {'photo_ids': photos[i:i + 4]})) for i in range(0, len(photos), 4)]
    frames += extra_responses or []
    frames.append(response(('finish_review', result or finished(photos))))
    calls = []
    def model(messages, config, timeout):
        calls.append(json.loads(json.dumps(messages)))
        return frames.pop(0) if frames else response(finish='length')
    monkeypatch.setattr(agent, '_request_model', model)
    return calls


def execute(agent, case):
    run = agent.request(case['id'])
    assert agent.run_one()
    return agent.get(run['id'])


def test_joint_review_uses_tools_images_and_persistent_history(agent, services, case, photo, monkeypatch):
    first, second = photo(), photo(color='red')
    calls = prepare(agent, monkeypatch, [first['id'], second['id']])
    before = services['domain'].case(case['id'])
    run = execute(agent, case)
    assert run['state'] == 'completed', run['error']
    assert run['current'] and len(run['result']['viewed_photo_ids']) == 2
    assert [s['tool'] for s in run['trace']] == ['read_case', 'read_records', 'read_requirements', 'read_photos', 'finish_review']
    assert sum(part.get('type') == 'image_url' for m in calls[-1] if isinstance(m.get('content'), list) for part in m['content']) == 2
    assert run['usage']['requests'] == 3
    assert services['domain'].case(case['id']) == before
    assert not services['db'].all('SELECT * FROM followup_issues')
    assert not services['db'].all('SELECT * FROM followup_feedback')
    assert agent.request(case['id'])['id'] == run['id']
    assert agent.list(case['id'])['history'][0]['id'] == run['id']


def test_draft_adoption_is_idempotent_and_basic_scan_cannot_resolve_it(agent, services, case, photo, monkeypatch):
    pic = photo()
    prepare(agent, monkeypatch, [pic['id']])
    run = execute(agent, case)
    adopted = agent.adopt(run['id'], [0])
    assert agent.adopt(run['id'], [0]) == adopted
    assert services['db'].one('SELECT COUNT(*) n FROM followup_issues')['n'] == 1
    issue = services['followup'].issues()[0]
    assert issue['feedback']['state'] == 'ready'
    assert issue['evidence']['trigger_at'] == ''
    assert not services['db'].all('SELECT * FROM followup_feedback')
    services['followup'].review()
    assert services['db'].one('SELECT state FROM followup_issues WHERE id=?', (adopted['issue_ids'][0],))['state'] == 'open'
    # The same suggestion from a deliberate rerun reuses its issue instead of notifying twice.
    prepare(agent, monkeypatch, [pic['id']])
    again = agent.request(case['id'], force=True)
    agent.run_one()
    assert agent.adopt(again['id'], [0])['issue_ids'] == adopted['issue_ids']


@pytest.mark.parametrize('change', ['case', 'new_photo', 'photo', 'template', 'model', 'original', 'policy'])
def test_changed_evidence_blocks_old_drafts(agent, services, case, photo, monkeypatch, change):
    pic = photo()
    prepare(agent, monkeypatch, [pic['id']])
    run = execute(agent, case)
    adopted = agent.adopt(run['id'], [0])
    if change == 'case':
        services['domain'].save_case({'title': '另一维修事项'}, case['id'])
    elif change == 'new_photo':
        photo(color='red')
    elif change == 'photo':
        services['domain'].update_photo(pic['id'], {'notes': '新核对信息', 'revision': pic['revision']})
    elif change == 'template':
        with services['db'].tx() as con:
            con.execute("UPDATE templates SET requirements='[]' WHERE id=?", (case['insurer_template_id'],))
    elif change == 'model':
        cfg = services['db'].setting('recognition')
        services['db'].set_setting('recognition', {**cfg, 'model': 'other-model'})
    elif change == 'policy':
        monkeypatch.setattr('idi.case_agent.POLICY_VERSION', 999)
    else:
        blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (pic['blob_hash'],))
        services['storage'].resolve(blob['stored_path']).write_bytes(b'changed-original')
    assert not agent.get(run['id'])['current']
    with pytest.raises(DomainError):
        agent.adopt(run['id'], [0])
    issue = next(i for i in services['followup'].issues() if i['id'] == adopted['issue_ids'][0])
    assert issue['feedback']['state'] == 'blocked'


def test_unseen_and_cross_case_citations_are_rejected(agent, case, photo, monkeypatch):
    pic = photo()
    prepare(agent, monkeypatch, [pic['id']], finished(['not-read-in-this-case']))
    run = execute(agent, case)
    assert run['state'] == 'failed' and run['result'] is None
    assert any('引用' in s['outcome'] for s in run['trace'])
    with pytest.raises(DomainError):
        agent.adopt(run['id'], [0])


def test_partial_review_is_labelled_and_cannot_be_adopted(agent, case, photo, monkeypatch):
    pic = photo()
    photo(color='red')
    prepare(agent, monkeypatch, [pic['id']])
    run = execute(agent, case)
    assert run['state'] == 'partial'
    assert len(run['result']['unseen_photo_ids']) == 1
    assert any('1/2' in line for line in run['result']['limitations'])
    with pytest.raises(DomainError):
        agent.adopt(run['id'], [0])


@pytest.mark.parametrize('entry', [
    {'kind': 'archive_note'}, {'kind': 'needs_check', 'certainty': 'uncertain'},
    {'action': '请补充资料'}, {'detail': '负责人未提交施工照片，需要补充材料。'},
    {'title': '照片分组与当前办理阶段不符', 'action': '请核实当前案件真实办理状态，再更新案件办理阶段。'},
    {'title': '地砖破裂缺少损坏状态照片', 'detail': '已查看照片仅显示打胶作业，未见原始损坏状态。', 'action': '请补拍地砖破裂的具体位置及损坏特写照片。'},
])
def test_internal_uncertain_or_generic_findings_never_become_feedback(agent, case, photo, monkeypatch, entry):
    pic = photo()
    prepare(agent, monkeypatch, [pic['id']], finished([pic['id']], [finding([pic['id']], **entry)]))
    run = execute(agent, case)
    assert run['state'] == 'completed'
    assert run['result']['findings'][0]['kind'] != 'visible_issue'
    with pytest.raises(DomainError):
        agent.adopt(run['id'], [0])


def test_truncated_and_invalid_responses_never_become_success(agent, case, photo, monkeypatch):
    photo()
    monkeypatch.setattr(agent, '_request_model', lambda *args: response(finish='length'))
    run = execute(agent, case)
    assert run['state'] == 'failed' and not run['result']
    assert '截断' in run['error']


def test_steps_are_bounded_even_when_model_ignores_tools(agent, case, photo, monkeypatch):
    photo()
    monkeypatch.setattr(agent, '_request_model', lambda *args: response(finish='stop'))
    run = execute(agent, case)
    assert run['state'] == 'failed' and run['usage']['requests'] == MAX_STEPS
    assert '步骤上限' in run['error']


def test_corrected_result_schema_can_complete_with_full_evidence(agent, case, photo, monkeypatch):
    pic = photo()
    bad = finished([pic['id']], limitations={'unexpected': 'not a list'})
    prepare(agent, monkeypatch, [pic['id']], extra_responses=[response(('finish_review', bad))])
    run = execute(agent, case)
    assert run['state'] == 'completed'
    assert any('limitations' in row['outcome'] for row in run['trace'])


def test_gateway_encoded_lists_are_normalized_without_losing_evidence(agent, case, photo, monkeypatch):
    pic = photo()
    result = finished([pic['id']], limitations='照片不能替代验收')
    result['findings'] = dump(result['findings'])
    prepare(agent, monkeypatch, [pic['id']], result)
    run = execute(agent, case)
    assert run['state'] == 'completed'
    assert set(run['result']['normalized_fields']) == {'limitations', 'findings'}
    assert run['result']['findings'][0]['photo_ids'] == [pic['id']]


def test_unavailable_original_keeps_review_incomplete(agent, services, case, photo, monkeypatch):
    pic = photo()
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (pic['blob_hash'],))
    services['storage'].resolve(blob['stored_path']).unlink()
    prepare(agent, monkeypatch, [pic['id']])
    run = execute(agent, case)
    assert run['state'] == 'failed'
    assert not run['result']
    assert any('原件缺失' in row['outcome'] for row in run['trace'])


def test_outside_tools_cannot_write_or_access_other_cases(agent, services, case, photo, monkeypatch):
    pic = photo()
    attempts = [response(('send_message', {'text': 'attempt'})), response(('read_photos', {'photo_ids': ['other-case-photo']}))]
    prepare(agent, monkeypatch, [pic['id']], extra_responses=attempts)
    run = execute(agent, case)
    assert run['state'] == 'completed'
    assert len(run['result']['viewed_photo_ids']) == 1
    assert not services['db'].all('SELECT * FROM followup_feedback')
    assert any('允许范围' in s['outcome'] for s in run['trace'])
    assert any('本案清单' in s['outcome'] for s in run['trace'])


def test_inflight_cancel_discards_model_reply(agent, case, photo, monkeypatch):
    photo()
    run = agent.request(case['id'])
    def cancel(*args):
        agent.cancel(run['id'])
        return response(('read_case', {}))
    monkeypatch.setattr(agent, '_request_model', cancel)
    agent.run_one()
    assert agent.get(run['id'])['state'] == 'cancelled'
    assert not agent.get(run['id'])['result']


def test_queue_dedup_cancel_and_scope(agent, services, case, photo):
    photo()
    run = agent.request(case['id'])
    assert agent.request(case['id'], force=True)['id'] == run['id']
    assert agent.cancel(run['id'])['state'] == 'cancelled'
    assert not agent.run_one()
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='pending_intake' WHERE id=?", (case['id'],))
    with pytest.raises(DomainError):
        agent.request(case['id'])


def test_change_during_model_request_stops_result(agent, services, case, photo, monkeypatch):
    photo()
    def changed(*args):
        services['domain'].save_case({'title': '变更后的维修事项'}, case['id'])
        return response(('read_case', {}))
    monkeypatch.setattr(agent, '_request_model', changed)
    run = execute(agent, case)
    assert run['state'] == 'failed' and '已变化' in run['error']
    assert not run['result']


def test_closed_case_can_be_reviewed_but_cannot_generate_demand(agent, services, case, photo, monkeypatch):
    pic = photo()
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='closed' WHERE id=?", (case['id'],))
    prepare(agent, monkeypatch, [pic['id']])
    run = execute(agent, case)
    assert run['state'] == 'completed'
    with pytest.raises(DomainError):
        agent.adopt(run['id'], [0])


def test_related_messages_require_explicit_identifier_and_bound_project(agent, services, case, photo):
    photo()
    db = services['db']
    connector_id = uid()
    with db.tx() as con:
        con.execute('INSERT INTO connectors(id,name,kind,config,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                    (connector_id, '测试资料入口', 'feed', dump({'project_id': case['project_id']}), now(), now()))
        for index, content in enumerate([case['short_code'] + ' 已补充近照', '另一案件已完成', 'x' + case['short_code'] + '99']):
            con.execute('INSERT INTO messages(id,connector_id,source_key,received_at,content) VALUES(?,?,?,?,?)', (uid(), connector_id, str(index), now(), content))
    snapshot = agent.snapshot(case['id'])
    assert len(snapshot['messages']) == 1
    assert snapshot['messages'][0]['content'].endswith('已补充近照')


def test_api_local_write_header_and_invalid_selection(client, agent, services, case, photo, monkeypatch):
    pic = photo()
    assert client.get(f"/api/agent/cases/{case['id']}").json()['image_count'] == 1
    prepare(agent, monkeypatch, [pic['id']])
    r = client.post(f"/api/agent/cases/{case['id']}/review", json={})
    assert r.status_code == 200
    agent.run_one()
    run_id = r.json()['id']
    assert client.get(f'/api/agent/runs/{run_id}').json()['state'] == 'completed'
    assert client.post(f'/api/agent/runs/{run_id}/adopt', json={'finding_indexes': [True]}).status_code == 400
    assert client.post(f'/api/agent/runs/{run_id}/adopt', json={'finding_indexes': [0, 0]}).status_code == 400
    assert client.post(f'/api/agent/runs/{run_id}/adopt', json={'finding_indexes': [0]}).json()['state'] == 'draft'


@pytest.mark.parametrize('changed', [False, True])
def test_agent_feedback_uses_its_own_current_evidence(agent, services, case, photo, monkeypatch, changed):
    pic = photo()
    prepare(agent, monkeypatch, [pic['id']])
    run = execute(agent, case)
    agent.adopt(run['id'], [0])
    connector_id = uid()
    with services['db'].tx() as con:
        con.execute('INSERT INTO connectors(id,name,kind,enabled,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (connector_id, '测试群', 'feed', 1, dump({'adapter': 'feishu', 'app_id': 'cli_test', 'chat_id': 'oc_test', 'chat_name': '测试群'}), now(), now()))
    class Feedback:
        sent = []
        def members(self, chat): return [{'open_id': 'ou_test', 'name': '测试负责人'}]
        def send_feedback(self, *args): self.sent.append(args); return 'om_test'
        def close(self): pass
    feedback = Feedback()
    service = services['followup']
    monkeypatch.setattr(service, 'client', lambda *args: feedback)
    service.save_contact(case['project_id'], {'connector_id': connector_id, 'open_id': 'ou_test', 'auto_feedback': True})
    assert service.preview_feedback(case['project_id'], automatic=True)['count'] == 0
    preview = service.preview_feedback(case['project_id'])
    assert preview['count'] == 1
    queued = service.queue_feedback(case['project_id'], preview_token=preview['preview_token'])
    if changed:
        photo(color='green')
    service.deliver(queued['id'])
    assert len(feedback.sent) == (0 if changed else 1)
    assert services['db'].one('SELECT state FROM followup_feedback WHERE id=?', (queued['id'],))['state'] == ('cancelled' if changed else 'sent')


def test_re_adoption_after_evidence_change_creates_new_issue_version(agent, services, case, photo, monkeypatch):
    first = photo()
    result = finished([first['id']])
    prepare(agent, monkeypatch, [first['id']], result)
    run = execute(agent, case)
    issue_id = agent.adopt(run['id'], [0])['issue_ids'][0]
    with services['db'].tx() as con:
        con.execute('UPDATE followup_issues SET feedback_generation=generation WHERE id=?', (issue_id,))
    second = photo(color='green')
    prepare(agent, monkeypatch, [first['id'], second['id']], result)
    run = execute(agent, case)
    assert agent.adopt(run['id'], [0])['issue_ids'] == [issue_id]
    issue = services['db'].one('SELECT * FROM followup_issues WHERE id=?', (issue_id,))
    assert issue['generation'] == 2 and issue['feedback_generation'] == 1


def test_short_photo_references_map_to_verified_originals(agent, case, photo, monkeypatch):
    first, second = photo(), photo(color='red')
    prepare(agent, monkeypatch, ['P01', 'P02'], finished(['P01', 'P02']))
    run = execute(agent, case)
    assert run['state'] == 'completed'
    assert set(run['result']['findings'][0]['photo_ids']) == {first['id'], second['id']}


def test_separate_finding_tools_keep_citations_and_finish_without_nested_arrays(agent, case, photo, monkeypatch):
    pic = photo()
    frames = [response(('read_case', {}), ('read_records', {}), ('read_requirements', {})),
              response(('read_photos', {'photo_ids': ['P01']})),
              response(('record_finding', finding(['P01']))),
              response(('finish_review', {'summary': '照片显示维修主体受前景遮挡，需核对。', 'limitations': []}))]
    monkeypatch.setattr(agent, '_request_model', lambda *args: frames.pop(0))
    run = execute(agent, case)
    assert run['state'] == 'completed'
    assert run['result']['findings'][0]['photo_ids'] == [pic['id']]
    assert run['trace'][-2]['tool'] == 'record_finding'
    assert agent.adopt(run['id'], [0])['count'] == 1


def test_recorded_finding_does_not_save_a_truncated_review(agent, case, photo, monkeypatch):
    photo()
    frames = [response(('read_case', {}), ('read_records', {}), ('read_requirements', {})),
              response(('read_photos', {'photo_ids': ['P01']})),
              response(('record_finding', finding(['P01']))), response(finish='length')]
    monkeypatch.setattr(agent, '_request_model', lambda *args: frames.pop(0))
    run = execute(agent, case)
    assert run['state'] == 'failed' and not run['result']


def test_up_to_six_findings_can_be_recorded_in_one_model_turn(agent, case, photo, monkeypatch):
    photo()
    frames = [response(('read_case', {}), ('read_records', {}), ('read_requirements', {})),
              response(('read_photos', {'photo_ids': ['P01']})),
              response(*(('record_finding', finding(['P01'], title=f'待核对事项 {i}', kind='needs_check')) for i in range(6))),
              response(('finish_review', {'summary': '本次记录了六项不同的核对事项。', 'limitations': []}))]
    monkeypatch.setattr(agent, '_request_model', lambda *args: frames.pop(0))
    run = execute(agent, case)
    assert run['state'] == 'completed' and len(run['result']['findings']) == 6


def test_finding_in_same_turn_as_image_request_needs_a_later_model_turn(agent, case, photo, monkeypatch):
    photo()
    frames = [response(('read_case', {}), ('read_records', {}), ('read_requirements', {})),
              response(('read_photos', {'photo_ids': ['P01']}), ('record_finding', finding(['P01']))),
              response(('record_finding', finding(['P01']))),
              response(('finish_review', {'summary': '照片显示维修主体被遮挡，需调整角度核对。', 'limitations': []}))]
    monkeypatch.setattr(agent, '_request_model', lambda *args: frames.pop(0))
    run = execute(agent, case)
    assert run['state'] == 'completed' and len(run['result']['findings']) == 1
    assert any('先接收并查看图片' in row['outcome'] for row in run['trace'])


def test_backup_restore_cancels_pending_agent_calls(agent, services, case, photo, tmp_path):
    from idi.cli import restore
    from idi.db import Database
    photo()
    run = agent.request(case['id'])
    backup = services['reporter'].backup({'backup_id': 'agent-restore-test'}, lambda *a: None)
    target = tmp_path / 'restored-agent'
    restore(services['settings'].data_dir / 'backups' / backup['filename'], target)
    restored = Database(target / 'archive.sqlite3')
    assert restored.one('SELECT state FROM case_agent_runs WHERE id=?', (run['id'],))['state'] == 'cancelled'
    assert services['db'].one('SELECT state FROM case_agent_runs WHERE id=?', (run['id'],))['state'] == 'queued'
