import json

import pytest
from fastapi.testclient import TestClient

from idi.case_assignments import CaseAssignments
from idi.domain import DomainError
from idi.identity import acting_as
from tests.test_project_access import make_person, login_person, png
from tests.test_official_camera import official, upload
from tests.test_platform_preparation import platform
from tests.test_automatic_uploads import setup_flow, tick


def assign(client, case, workers, mode='replace', revision=None):
    if revision is None:
        revision = client.get(f"/api/cases/{case['id']}/workers").json()['revision']
    return client.post('/api/access/case-assignments', json={
        'case_ids': [case['id']], 'user_ids': [w['id'] for w in workers], 'mode': mode,
        'expected_revisions': {case['id']: revision}})


@pytest.fixture
def worker_phone(official, services, project):
    worker = make_person(services, project['id'], 'worker')
    camera = services['mobile_camera']
    pair = camera.pair(camera.create_pairing({'name': '现场工人手机', 'user_id': worker['id'],
                                             'project_ids': [project['id']]})['code'])
    official['worker'] = worker
    official['device'] = pair
    official['headers'] = {'Authorization': 'Bearer ' + pair['token']}
    return official


def test_worker_has_empty_list_until_explicitly_assigned_and_cannot_browse_other_cases(app, client, services, project, case, worker_phone):
    extra = services['domain'].save_case({'project_id': project['id'], 'title': '不可见案件', 'building': '2', 'room': '901'})
    phone = TestClient(app, headers=worker_phone['headers'])
    web = login_person(app)
    assert phone.get('/api/camera/cases').json()['items'] == []
    token = services['automatic_uploads']._signal.token()
    assert phone.get('/api/camera/cases').json()['items'] == []
    assert services['automatic_uploads']._signal.token() == token  # last-seen heartbeat is not a permission change
    assert web.get('/api/cases').json() == []
    assert web.get('/api/projects').json()[0]['case_count'] == 0
    assert upload(client, worker_phone).status_code == 403
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    assert [c['id'] for c in phone.get('/api/camera/cases').json()['items']] == [case['id']]
    assert [c['id'] for c in web.get('/api/cases').json()] == [case['id']]
    assert web.get('/api/cases/' + case['id']).status_code == 200
    assert web.get('/api/cases/' + extra['id']).status_code == 403
    assert web.get('/api/projects').json()[0]['case_count'] == 1
    assert web.get('/api/workflow/board?board=work&source=local').json()['items'] == []
    for path in (f"/api/workflow/cases/{case['id']}", f"/api/cases/{case['id']}/workers",
                 f"/api/access/projects/{project['id']}/workers", '/api/access/users'):
        assert web.get(path).status_code == 403, path
    assert web.patch('/api/cases/' + case['id'], json={'title': '越权修改'}).status_code == 403
    assert web.post('/api/import/upload', data={'case_id': case['id']}, files={'files': ('a.png', png(), 'image/png')}).status_code == 403
    assert assign(web, case, [], revision=1).status_code == 403


def test_worker_receipt_upload_delete_and_retries_require_current_assignment(client, services, case, worker_phone):
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    worker_phone['service'].settings.save({'auto_verify': False})
    first = upload(client, worker_phone)
    assert first.status_code == 200, first.text
    photo_id = first.json()['photo_id']
    assert services['domain'].photo(photo_id)['uploaded_by'] == worker_phone['worker']['id']
    path = '/api/camera/official-captures/' + worker_phone['meta']['capture_id']
    assert client.get(path, headers=worker_phone['headers']).status_code == 200
    assert assign(client, case, []).status_code == 200
    for response in (upload(client, worker_phone), client.get(path, headers=worker_phone['headers']),
                     client.post(path + '/retry', headers=worker_phone['headers']),
                     client.request('DELETE', path, headers=worker_phone['headers'], json={'case_id': case['id']})):
        assert response.status_code == 403, response.text
    assert services['db'].one('SELECT id FROM photos WHERE id=?', (photo_id,))
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    assert upload(client, worker_phone).json()['duplicate']
    deleted = client.request('DELETE', path, headers=worker_phone['headers'], json={'case_id': case['id']})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()['originals_retained']


def test_worker_cannot_delete_other_workers_capture(client, services, project, case, worker_phone):
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    worker_phone['service'].settings.save({'auto_verify': False})
    assert upload(client, worker_phone).status_code == 200
    second = make_person(services, project['id'], 'worker', 'worker-two')
    assert assign(client, case, [second], mode='add').status_code == 200
    camera = services['mobile_camera']
    device = camera.pair(camera.create_pairing({'name': '另一手机', 'user_id': second['id'], 'project_ids': [project['id']]})['code'])
    response = client.request('DELETE', '/api/camera/official-captures/' + worker_phone['meta']['capture_id'],
        headers={'Authorization': 'Bearer ' + device['token']}, json={'case_id': case['id']})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize('phase', ['queued', 'processing'])
def test_revocation_stops_queued_and_inflight_verification(client, services, case, worker_phone, phase):
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    assert upload(client, worker_phone).status_code == 200
    if phase == 'processing':
        assert worker_phone['service'].process_one()
    before = len(worker_phone['calls'])
    assert assign(client, case, []).status_code == 200
    assert worker_phone['service'].process_one()
    assert len(worker_phone['calls']) == before
    assert not services['db'].all('SELECT id FROM photos')
    row = services['db'].one('SELECT * FROM camera_official_captures')
    assert row['state'] == 'error' and '分配' in row['error']
    assert services['storage'].resolve(services['db'].one('SELECT stored_path FROM blobs')['stored_path']).is_file()


def test_assignment_removed_while_file_is_staged_is_rechecked_at_commit(client, services, case, worker_phone, monkeypatch):
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    worker_phone['service'].settings.save({'auto_verify': False})
    original = services['storage'].put
    def revoke_after_store(*args, **kwargs):
        result = original(*args, **kwargs)
        with services['db'].tx() as con:
            con.execute('DELETE FROM case_worker_assignments WHERE case_id=?', (case['id'],))
        return result
    monkeypatch.setattr(services['storage'], 'put', revoke_after_store)
    response = upload(client, worker_phone)
    assert response.status_code == 403, response.text
    assert not services['db'].all('SELECT id FROM photos')
    assert not services['db'].all('SELECT * FROM camera_official_captures')


def test_assignment_permissions_batch_atomicity_conflicts_and_audit(app, client, services, project, case):
    manager = make_person(services, project['id'], 'manager', 'manager')
    worker = make_person(services, project['id'], 'worker')
    second = make_person(services, project['id'], 'worker', 'worker-two')
    member = make_person(services, project['id'], 'member', 'member')
    other_project = services['domain'].save_project({'name': '其他项目'})
    other = services['domain'].save_case({'project_id': other_project['id'], 'title': '其他项目案件'})
    managing = login_person(app, 'manager')
    assert assign(managing, case, [worker, second]).status_code == 200
    assert assign(managing, case, [member]).status_code == 403
    assert assign(managing, case, [], revision=0).status_code == 409
    body = {'case_ids': [case['id'], other['id']], 'user_ids': [worker['id']], 'mode': 'replace',
            'expected_revisions': {case['id']: 1, other['id']: 0}}
    assert managing.post('/api/access/case-assignments', json=body).status_code == 403
    detail = client.get(f"/api/cases/{case['id']}/workers").json()
    assert len(detail['workers']) == 2
    assert detail['history'][0]['actor_name'] == manager['display_name']
    assert assign(managing, case, [worker], mode='remove').status_code == 200
    assert [w['user_id'] for w in client.get(f"/api/cases/{case['id']}/workers").json()['workers']] == [second['id']]
    assert assign(managing, other, [worker], revision=0).status_code == 403


def test_membership_removal_does_not_resurrect_assignments_and_mixed_roles_are_scoped(app, client, services, project, case):
    worker = make_person(services, project['id'], 'worker')
    assert assign(client, case, [worker]).status_code == 200
    second_project = services['domain'].save_project({'name': '我管理的项目'})
    services['access'].save_members(second_project['id'], {'members': [{'user_id': worker['id'], 'role': 'manager'}]})
    second = services['domain'].save_case({'project_id': second_project['id'], 'title': '管理案件'})
    services['importer'].add_file(png(), '管理项目待归案照片.png', project_id=second_project['id'])
    hidden = services['domain'].save_case({'project_id': project['id'], 'title': '工人项目其他案件'})
    web = login_person(app)
    assert next(p for p in web.get('/api/projects').json() if p['id'] == second_project['id'])['photo_count'] == 1
    assert {c['id'] for c in web.get('/api/cases').json()} == {case['id'], second['id']}
    assert web.get('/api/cases/' + hidden['id']).status_code == 403
    assert web.get('/api/workflow/cases/' + second['id']).status_code == 200
    services['access'].save_members(project['id'], {'members': []})
    services['access'].save_members(project['id'], {'members': [{'user_id': worker['id'], 'role': 'worker'}]})
    assert [c['id'] for c in web.get('/api/cases').json()] == [second['id']]
    assert services['db'].one("SELECT action FROM audit WHERE entity_type='case_workers' ORDER BY rowid DESC")['action'] == 'membership_removed'


def test_assignment_filter_and_project_move(client, services, project, case):
    worker = make_person(services, project['id'], 'worker')
    extra = services['domain'].save_case({'project_id': project['id'], 'title': '待分配新案'})
    assert assign(client, case, [worker]).status_code == 200
    base = '/api/workflow/board?source=local&project_id=' + project['id']
    assert [c['id'] for c in client.get(base + '&worker=unassigned').json()['items']] == [extra['id']]
    assert [c['id'] for c in client.get(base + '&worker=assigned').json()['items']] == [case['id']]
    other = services['domain'].save_project({'name': '移交项目'})
    services['domain'].save_case({'project_id': other['id'], 'revision': case['revision']}, case['id'])
    assert not services['db'].all('SELECT * FROM case_worker_assignments')


def test_worker_schema_upgrade_preserves_existing_data(services, project, case, admin_user):
    from tests.conftest import rewind_additive_test_schema
    make_person(services, project['id'])
    db = services['db']
    tables = ('idi_users', 'project_members', 'camera_devices', 'photos', 'audit')
    before = {t: db.all('SELECT * FROM ' + t) for t in tables}
    with db.tx() as con:
        rewind_additive_test_schema(con, 43)
    db.initialize()
    assert before == {t: db.all('SELECT * FROM ' + t) for t in tables}
    assert db.one('PRAGMA integrity_check')['integrity_check'] == 'ok'
    assert db.all('PRAGMA foreign_key_check') == []
    assert db.all('SELECT * FROM case_worker_assignments') == []


def test_revoked_worker_photo_cannot_continue_rules_or_automatic_upload(client, services, case, worker_phone, platform, photo, monkeypatch):
    from idi.mobile_camera import case_number
    auto, _, uploads, writes, _ = setup_flow(services, platform, case, photo, monkeypatch)
    worker_phone['meta'].update(case_number=case_number(services['domain'].case(case['id'])),
                               photo_group='survey_problem', role='problem')
    worker_phone['service'].settings.save({'auto_verify': False})
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    receipt = upload(client, worker_phone)
    assert receipt.status_code == 200, receipt.text
    photo_id = receipt.json()['photo_id']
    rules = services['photo_workflow']
    rules.process(photo_id)
    assert services['db'].one('SELECT identity FROM photo_pipeline WHERE photo_id=?', (photo_id,))['identity'] == 'app_assigned_case'
    before = auto.signature(case['id'])
    assert assign(client, case, []).status_code == 200
    assert auto.signature(case['id']) != before
    blocked = rules.process(photo_id)
    assert blocked['state'] == 'needs_check' and '案件权限' in blocked['reason']
    tick(auto, case['id'])
    assert not uploads and not writes
    assert services['domain'].photo(photo_id)['case_id'] == case['id']
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    tick(auto, case['id'])
    assert len(uploads) == 1 and all(payload['state'] == 0 for _, payload in writes)


def test_worker_legacy_upload_and_original_use_same_scope(client, services, case, worker_phone):
    from tests.test_mobile_camera import jpeg
    metadata = {k: v for k, v in worker_phone['meta'].items() if k != 'source'}
    def legacy():
        return client.post('/api/camera/captures', headers=worker_phone['headers'], data={'metadata': json.dumps(metadata)},
                           files={'original': ('a.jpg', jpeg('blue'), 'image/jpeg'), 'stamped': ('b.jpg', jpeg('red'), 'image/jpeg')})
    assert legacy().status_code == 403
    assert assign(client, case, [worker_phone['worker']]).status_code == 200
    result = legacy()
    assert result.status_code == 200, result.text
    original = '/api/camera/captures/' + metadata['capture_id'] + '/original'
    assert client.get(original, headers=worker_phone['headers']).status_code == 200
    assert assign(client, case, []).status_code == 200
    assert legacy().status_code == 403
    assert client.get(original, headers=worker_phone['headers']).status_code == 403
