"""Saved-photo status must identify the project writer, not the company reader."""
import json

from idi.db import now
from tests.test_project_access import make_person
from tests.test_project_accounts import configured


def active_save(services, case, stage='repair'):
    with services['db'].tx() as con:
        con.execute('UPDATE cases SET workflow_stage=? WHERE id=?', (stage, case['id']))
        con.execute('INSERT INTO automatic_upload_cases(case_id,state,created_at,updated_at) VALUES(?,?,?,?)',
                    (case['id'], 'awaiting_login', now(), now()))


def test_company_connection_does_not_mask_missing_project_credentials(services, project, case, client):
    active_save(services, case)
    company = services['project_accounts'].company.client
    company._token, company._account = 'company-token', 'company-reader'
    result = client.get('/api/workflow/uploads')
    assert result.status_code == 200
    value = result.json()
    assert value['account_scope'] == 'project' and value['company_connected'] is True
    assert value['connected'] is False and value['can_configure_project_accounts'] is True
    assert value['project_connections'] == [{'project_id': project['id'], 'project_name': project['name'],
                                              'state': 'unconfigured', 'connected': False}]
    assert value['items'][0]['project_id'] == project['id']
    assert 'company-token' not in result.text and 'company-reader' not in result.text


def test_connected_project_does_not_hide_another_project_and_pause_is_distinct(services, project, case, client):
    active_save(services, case)
    other = services['domain'].save_project({'name': '仍待连接的项目'})
    other_case = services['domain'].save_case({'project_id': other['id'], 'title': '另一项目照片'})
    active_save(services, other_case)
    calls = []
    configured(services, project['id'], 'writer-one', calls)
    value = client.get('/api/workflow/uploads').json()
    assert value['connected'] is True
    states = {item['project_id']: item['state'] for item in value['project_connections']}
    assert states == {project['id']: 'connected', other['id']: 'unconfigured'}
    with services['db'].tx() as con:
        con.execute('UPDATE project_website_accounts SET enabled=0 WHERE project_id=?', (project['id'],))
    states = {item['project_id']: item['state'] for item in client.get('/api/workflow/uploads').json()['project_connections']}
    assert states[project['id']] == 'write_paused'
    assert calls == []


def test_member_only_sees_own_account_need_without_credentials_or_edit_action(services, project, case, client):
    active_save(services, case)
    other = services['domain'].save_project({'name': '不可见项目'})
    other_case = services['domain'].save_case({'project_id': other['id'], 'title': '不可见保存'})
    active_save(services, other_case)
    configured(services, other['id'], 'private-writer', [])
    make_person(services, project['id'], 'member', 'save-viewer')
    assert client.post('/api/auth/login', json={'username': 'save-viewer', 'password': 'person-test-password'}).status_code == 200
    value = client.get('/api/workflow/uploads').json()
    assert value['can_configure_project_accounts'] is False
    assert [item['project_id'] for item in value['project_connections']] == [project['id']]
    assert other['id'] not in json.dumps(value) and 'private-writer' not in json.dumps(value)


def test_completed_stage_does_not_request_unused_project_connection(services, case, client):
    active_save(services, case, stage='acceptance')
    assert client.get('/api/workflow/uploads').json()['project_connections'] == []


def test_disconnected_project_and_server_write_switch_have_separate_states(services, project, case, client):
    active_save(services, case)
    account = configured(services, project['id'], 'writer-one', [])
    account.client._token = ''
    assert client.get('/api/workflow/uploads').json()['project_connections'][0]['state'] == 'disabled'
    services['project_accounts'].write_enabled = False
    assert client.get('/api/workflow/uploads').json()['project_connections'][0]['state'] == 'server_read_only'
