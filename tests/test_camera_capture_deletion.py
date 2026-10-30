"""Paired devices can withdraw only their own captures without resurrection."""
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from idi.db import dump, now
from idi.domain import DomainError
from idi.xhey_camera import XheyError
from test_official_camera import official, upload, upload_video, video_container
from test_project_access import make_person
from test_watermark_dedup import CODE, image_pair


@pytest.fixture
def phone(client, official):
    official['admin_cookies'] = list(client.cookies.jar)
    client.cookies.clear()  # Real APP requests never carry the workbench cookie.
    official['service'].settings.save({'auto_verify': False})
    return official


def remove(client, phone, *, capture_id=None, case_id=None, data=None, headers=None):
    return client.request('DELETE', '/api/camera/official-captures/' + (capture_id or phone['meta']['capture_id']),
                          headers=phone['headers'] if headers is None else headers,
                          json={'case_id': case_id or phone['meta']['case_id']} if data is None else data)


def pair_member(services, project, phone, username='other-camera-person'):
    user = make_person(services, project['id'], name=username)
    mobile = services['mobile_camera']
    pair = mobile.pair(mobile.create_pairing({'name': username, 'project_ids': [project['id']],
                                            'user_id': user['id']})['code'])
    return {**phone, 'headers': {'Authorization': 'Bearer ' + pair['token']}, 'device': pair, 'user': user}


@pytest.mark.parametrize('media', ['image', 'video'])
def test_delete_archived_capture_is_idempotent_preserves_original_and_blocks_replay(client, phone, services, media):
    response = upload_video(client, phone) if media == 'video' else upload(client, phone)
    assert response.status_code == 200, response.text
    receipt = response.json()
    db = services['db']
    blob = db.one('SELECT * FROM blobs WHERE hash=?', (receipt['original_hash'],))
    original = services['storage'].resolve(blob['stored_path']).read_bytes()
    deleted = remove(client, phone)
    assert deleted.status_code == 200, deleted.text
    result = deleted.json()
    assert result['state'] == 'deleted' and result['deleted'] and result['archive_removed']
    assert result['originals_retained'] and not result['shared_archive_retained'] and not result['already_deleted']
    assert not db.one('SELECT * FROM photos WHERE id=?', (receipt['photo_id'],))
    row = db.one('SELECT * FROM camera_official_captures')
    assert row['state'] == 'deleted' and row['photo_id'] is None and row['blob_hash'] == receipt['original_hash']
    assert client.get('/api/camera/official-captures/' + phone['meta']['capture_id'], headers=phone['headers']).status_code == 410
    replay = upload_video(client, phone) if media == 'video' else upload(client, phone)
    assert replay.status_code == 410
    assert remove(client, phone).json() == {**result, 'already_deleted': True}
    assert len(db.all('SELECT * FROM camera_capture_deletions')) == 1
    assert len(db.all("SELECT * FROM audit WHERE entity_type='camera_official' AND action='delete'")) == 1
    photo_audit = db.one("SELECT * FROM audit WHERE entity_type='photo' AND entity_id=? AND action='delete'", (receipt['photo_id'],))
    assert json.loads(photo_audit['before_json'])['sources']
    assert photo_audit['actor'] == 'user:' + phone['device']['user_id']
    assert services['storage'].resolve(blob['stored_path']).read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == receipt['original_hash']
    assert not services['official_camera'].process_one() and not services['jobs'].run_one()
    assert not phone['calls'] and not db.all('PRAGMA foreign_key_check')
    for cookie in phone['admin_cookies']:
        client.cookies.jar.set_cookie(cookie)
    assert client.get('/api/camera/admin/official').json()['captures'] == []


def test_delete_unknown_capture_prevents_late_upload_to_both_ingestion_paths(client, phone, services):
    deleted = remove(client, phone)
    assert deleted.status_code == 200 and deleted.json()['archive_removed'] is False
    assert not services['db'].all('SELECT * FROM camera_official_captures')
    assert client.get('/api/camera/official-captures/' + phone['meta']['capture_id'], headers=phone['headers']).status_code == 410
    assert upload_video(client, phone).status_code == 410
    meta = {key: value for key, value in phone['meta'].items() if key != 'source'}
    legacy = client.post('/api/camera/captures', headers=phone['headers'], data={'metadata': json.dumps(meta)},
                         files={'original': ('raw.jpg', phone['image'], 'image/jpeg'),
                                'stamped': ('stamped.jpg', phone['image'], 'image/jpeg')})
    assert legacy.status_code == 410, legacy.text
    assert not services['db'].all('SELECT * FROM photos') and not services['db'].all('SELECT * FROM blobs')
    assert remove(client, phone).json()['already_deleted'] is True


def test_delete_pending_verification_stops_further_verifier_work(client, phone, services):
    phone['service'].settings.save({'auto_verify': True})
    response = upload(client, phone)
    assert response.status_code == 200 and response.json()['state'] == 'received'
    assert response.json()['photo_id'] is None
    assert remove(client, phone).status_code == 200
    assert not phone['service'].process_one() and not phone['calls']
    assert not services['db'].all('SELECT * FROM photos')
    assert upload(client, phone).status_code == 410


def test_wrong_case_body_cannot_redirect_deletion(client, phone, services, project):
    receipt = upload(client, phone).json()
    other = services['domain'].save_case({'project_id': project['id'], 'title': '同项目其他案件'})
    result = remove(client, phone, case_id=other['id'])
    assert result.status_code == 409
    assert services['db'].one('SELECT id FROM photos WHERE id=?', (receipt['photo_id'],))
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_forged_allowed_case_does_not_bypass_real_capture_project_permission(client, phone, services):
    receipt = upload(client, phone).json()
    other_project = services['domain'].save_project({'name': '另一已授权项目'})
    other_case = services['domain'].save_case({'project_id': other_project['id'], 'title': '其他案件'})
    with services['db'].tx() as con:
        con.execute('UPDATE camera_devices SET project_ids=? WHERE id=?',
                    (dump([other_project['id']]), phone['device']['device_id']))
    assert remove(client, phone, case_id=other_case['id']).status_code == 403
    assert services['db'].one('SELECT id FROM photos WHERE id=?', (receipt['photo_id'],))
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_moved_archive_is_not_deleted_using_the_old_capture_snapshot(client, phone, services, project):
    receipt = upload(client, phone).json()
    other = services['domain'].save_case({'project_id': project['id'], 'title': '调整后案件'})
    with services['db'].tx() as con:
        con.execute('UPDATE photos SET case_id=? WHERE id=?', (other['id'], receipt['photo_id']))
    assert remove(client, phone).status_code == 409
    assert services['db'].one('SELECT case_id FROM photos')['case_id'] == other['id']
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_capture_already_deleted_on_workbench_can_be_acknowledged_idempotently(client, phone, services):
    receipt = upload(client, phone).json()
    for cookie in phone['admin_cookies']:
        client.cookies.jar.set_cookie(cookie)
    assert client.delete('/api/photos/' + receipt['photo_id']).status_code == 200
    client.cookies.clear()
    assert upload(client, phone).status_code == 410
    deleted = remove(client, phone)
    assert deleted.status_code == 200 and deleted.json()['deleted']
    assert not deleted.json()['archive_removed']
    assert remove(client, phone).json()['already_deleted']
    assert upload(client, phone).status_code == 410


def test_other_person_device_cannot_delete_capture_even_with_same_project(client, phone, services, project):
    receipt = upload(client, phone).json()
    other = pair_member(services, project, phone)
    response = remove(client, other)
    assert response.status_code == 403, response.text
    assert services['db'].one('SELECT id FROM photos WHERE id=?', (receipt['photo_id'],))
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_member_can_delete_own_capture_without_manager_or_browser_login(client, phone, services, project):
    member = pair_member(services, project, phone, 'capture-owner')
    response = upload(client, member)
    assert response.status_code == 200, response.text
    assert remove(client, member).status_code == 200
    assert not services['db'].all('SELECT * FROM photos')
    assert services['db'].one('SELECT requested_by FROM camera_capture_deletions')['requested_by'] == member['user']['id']


@pytest.mark.parametrize('permission,expected', [('device_project', 403), ('membership', 403),
                                               ('revoked', 401), ('disabled_person', 401)])
def test_deletion_rechecks_current_device_and_person_scope(client, phone, services, project, permission, expected):
    owner = pair_member(services, project, phone, 'scope-owner')
    receipt = upload(client, owner).json()
    with services['db'].tx() as con:
        if permission == 'device_project':
            con.execute("UPDATE camera_devices SET project_ids='[]' WHERE id=?", (owner['device']['device_id'],))
        elif permission == 'membership':
            con.execute('DELETE FROM project_members WHERE user_id=?', (owner['user']['id'],))
        elif permission == 'revoked':
            con.execute('UPDATE camera_devices SET revoked_at=? WHERE id=?', (now(), owner['device']['device_id']))
        else:
            con.execute('UPDATE idi_users SET enabled=0 WHERE id=?', (owner['user']['id'],))
    assert remove(client, owner).status_code == expected
    assert services['db'].one('SELECT id FROM photos WHERE id=?', (receipt['photo_id'],))
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_unknown_capture_cannot_create_tombstone_in_unpermitted_project(client, phone, services):
    project = services['domain'].save_project({'name': '未授权项目'})
    case = services['domain'].save_case({'project_id': project['id'], 'title': '未授权案件'})
    assert remove(client, phone, case_id=case['id']).status_code == 403
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_closed_case_can_withdraw_own_capture_with_current_project_access(client, phone, services):
    upload(client, phone)
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET workflow_stage='closed' WHERE id=?", (phone['meta']['case_id'],))
    assert remove(client, phone).status_code == 200


@pytest.mark.parametrize('data', [{}, {'case_id': ''}, {'case_id': 1}, {'case_id': 'bad', 'photo_id': 'forged'}])
def test_delete_requires_valid_case_envelope(client, phone, services, data):
    assert remove(client, phone, data=data).status_code == 400
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_delete_requires_device_token_even_when_browser_admin_is_logged_in(client, phone, services):
    for cookie in phone['admin_cookies']:
        client.cookies.jar.set_cookie(cookie)
    assert remove(client, phone, headers={}).status_code == 401
    client.cookies.clear()
    assert remove(client, phone, headers={'Authorization': 'Bearer invalid'}).status_code == 401
    assert remove(client, phone, capture_id='bad-id').status_code == 400
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_deleting_deduplicated_capture_retains_other_person_record_and_source(client, phone, services, project):
    phone['image'] = image_pair()[0]
    phone['meta']['sdk_anti_fake_code'] = CODE
    first = upload(client, phone).json()
    other = pair_member(services, project, phone, 'dedup-owner')
    other['meta'] = {**phone['meta'], 'capture_id': '23456789-1234-1234-1234-123456789abc'}
    second = upload(client, other)
    assert second.status_code == 200, second.text
    assert second.json()['photo_id'] == first['photo_id']
    photo_before = services['db'].one('SELECT * FROM photos')
    response = remove(client, phone)
    assert response.status_code == 200, response.text
    assert response.json()['shared_archive_retained'] and not response.json()['archive_removed']
    assert services['db'].one('SELECT * FROM photos') == photo_before
    sources = services['db'].all('SELECT * FROM photo_sources')
    assert len(sources) == 1 and json.loads(sources[0]['metadata'])['uploaded_by'] == other['user']['id']
    assert client.get('/api/camera/official-captures/' + other['meta']['capture_id'], headers=other['headers']).status_code == 200
    assert upload(client, phone).status_code == 410
    assert upload(client, other).json()['duplicate']


def test_busy_video_job_rolls_back_delete_and_allows_retry_when_idle(client, phone, services):
    receipt = upload_video(client, phone).json()
    task = services['db'].one('SELECT * FROM camera_video_times')
    with services['db'].tx() as con:
        con.execute("UPDATE jobs SET state='running' WHERE id=?", (task['job_id'],))
    assert remove(client, phone).status_code == 409
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')
    assert services['db'].one('SELECT id FROM photos WHERE id=?', (receipt['photo_id'],))
    with services['db'].tx() as con:
        con.execute("UPDATE jobs SET state='queued' WHERE id=?", (task['job_id'],))
    assert remove(client, phone).status_code == 200
    assert services['db'].one('SELECT state FROM jobs WHERE id=?', (task['job_id'],))['state'] == 'cancelled'
    assert services['db'].one('SELECT state FROM camera_video_times')['state'] == 'skipped'


def test_deletion_and_archive_cleanup_rollback_together(client, phone, services, monkeypatch):
    receipt = upload(client, phone).json()
    before = services['db'].one('SELECT * FROM photos')
    import idi.camera_capture_deletion as module
    def fail_audit(*args, **kwargs):
        raise RuntimeError('injected deletion audit failure')
    monkeypatch.setattr(module, 'audit', fail_audit)
    with pytest.raises(RuntimeError, match='injected deletion audit failure'):
        remove(client, phone)
    assert services['db'].one('SELECT * FROM photos') == before
    assert services['db'].one('SELECT photo_id,state FROM camera_official_captures') == {
        'photo_id': receipt['photo_id'], 'state': 'not_requested'}
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')
    assert not services['db'].all("SELECT * FROM audit WHERE action='delete'")


def test_delete_cleans_local_draft_selection_but_preserves_remote_receipt(client, phone, services):
    receipt = upload(client, phone).json()
    case_id, photo_id = phone['meta']['case_id'], receipt['photo_id']
    with services['db'].tx() as con:
        con.execute("INSERT INTO platform_drafts(case_id,stage,files_json,updated_at) VALUES(?,'repair',?,?)",
                    (case_id, dump({'images': [photo_id]}), now()))
        con.execute("INSERT INTO platform_review_batches(id,case_id,stage,state,fingerprint,snapshot,created_at) VALUES('saved',?,'repair','save_verified','sig','{}',?)",
                    (case_id, now()))
    assert remove(client, phone).status_code == 200
    assert json.loads(services['db'].one('SELECT files_json FROM platform_drafts')['files_json']) == {'images': []}
    assert services['db'].one("SELECT state FROM platform_review_batches WHERE id='saved'")['state'] == 'save_verified'


def test_cross_process_late_upload_checks_tombstone_again_inside_archive_transaction(client, phone, services, monkeypatch):
    original_put = services['storage'].put
    device = services['mobile_camera'].effective_device(services['db'].one(
        'SELECT * FROM camera_devices WHERE id=?', (phone['device']['device_id'],)))
    def delayed_put(*args, **kwargs):
        blob = original_put(*args, **kwargs)
        # Model a second process committing a deletion after upload validation,
        # without sharing this process's Python locks.
        with services['db'].tx() as con:
            services['official_camera'].deletions._delete(con, device, phone['meta']['capture_id'], phone['meta']['case_id'])
        return blob
    monkeypatch.setattr(services['storage'], 'put', delayed_put)
    response = upload(client, phone)
    assert response.status_code == 410, response.text
    assert not services['db'].all('SELECT * FROM camera_official_captures')
    assert not services['db'].all('SELECT * FROM photos')
    assert len(services['db'].all('SELECT * FROM camera_capture_deletions')) == 1


@pytest.mark.parametrize('phase', ['create', 'query_processing', 'query_verified', 'query_rejected', 'query_error'])
def test_cross_process_late_verifier_callback_cannot_overwrite_deleted_state(client, phone, services, phase):
    service, db = services['official_camera'], services['db']
    service.settings.save({'auto_verify': True})
    assert upload(client, phone).status_code == 200
    if phase != 'create':
        assert service.process_one()  # The external task has been submitted.
    device = services['mobile_camera'].effective_device(db.one('SELECT * FROM camera_devices WHERE id=?',
                                                               (phone['device']['device_id'],)))
    callbacks = []
    def concurrent_delete():
        # Another server process need not share this process's verifier lock.
        with db.tx() as con:
            service.deletions._delete(con, device, phone['meta']['capture_id'], phone['meta']['case_id'])
        callbacks.append(phase)
    class LateClient:
        def create(self, image):
            concurrent_delete()
            return 'late-task'
        def query(self, task):
            concurrent_delete()
            if phase == 'query_error':
                raise XheyError('late verification error')
            state = phase.removeprefix('query_')
            return state, {'photoTime': '2026-10-28 20:00:00', 'antiFakeCode': 'LATE-CODE'} if state == 'verified' else {}
    service.client_factory = lambda config: LateClient()
    assert service.process_one()
    row = db.one('SELECT * FROM camera_official_captures')
    assert callbacks == [phase] and row['state'] == 'deleted' and row['photo_id'] is None
    assert not db.all('SELECT * FROM photos') and not service.process_one()
    assert client.get('/api/camera/official-captures/' + phone['meta']['capture_id'], headers=phone['headers']).status_code == 410
    with pytest.raises(DomainError) as retry_error:
        service.retry(device['id'], phone['meta']['capture_id'])
    assert retry_error.value.status == 410
    with pytest.raises(DomainError) as report_error:
        service.report(device['id'], phone['meta']['capture_id'])
    assert report_error.value.status == 410


def test_upload_in_progress_then_delete_cannot_leave_active_record(client, phone, services, monkeypatch):
    entered, release, deleting = threading.Event(), threading.Event(), threading.Event()
    original_put = services['storage'].put
    original_delete = services['official_camera'].deletions.delete
    def blocked_put(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_put(*args, **kwargs)
    def marked_delete(*args, **kwargs):
        deleting.set()
        return original_delete(*args, **kwargs)
    monkeypatch.setattr(services['storage'], 'put', blocked_put)
    monkeypatch.setattr(services['official_camera'].deletions, 'delete', marked_delete)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_upload = pool.submit(upload, client, phone)
        assert entered.wait(5)
        future_delete = pool.submit(remove, client, phone)
        assert deleting.wait(5)
        release.set()
        assert future_upload.result(timeout=10).status_code in (200, 410)
        assert future_delete.result(timeout=10).status_code == 200
    assert not services['db'].all('SELECT * FROM photos')
    assert upload(client, phone).status_code == 410


def test_device_revoked_while_waiting_for_upload_lock_cannot_delete(client, phone, services, monkeypatch):
    receipt = upload(client, phone).json()
    entered = threading.Event()
    original = services['official_camera'].deletions.delete
    def reached(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(services['official_camera'].deletions, 'delete', reached)
    lock = services['mobile_camera'].upload_lock
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock:
            future = pool.submit(remove, client, phone)
            assert entered.wait(5)
            with services['db'].tx() as con:
                con.execute('UPDATE camera_devices SET revoked_at=? WHERE id=?', (now(), phone['device']['device_id']))
        assert future.result(timeout=10).status_code == 401
    assert services['db'].one('SELECT id FROM photos WHERE id=?', (receipt['photo_id'],))
    assert not services['db'].all('SELECT * FROM camera_capture_deletions')


def test_new_tombstone_migration_preserves_existing_archive_and_starts_empty(client, phone, services):
    from tests.conftest import rewind_additive_test_schema
    upload(client, phone)
    db = services['db']
    names = ['photos', 'photo_sources', 'blobs', 'camera_official_captures', 'audit']
    before = {name: db.all('SELECT * FROM ' + name) for name in names}
    with db.tx() as con:
        rewind_additive_test_schema(con, 40)
    db.initialize()
    assert db.one('SELECT version FROM schema_version WHERE version=41') == {'version': 41}
    assert not db.all('SELECT * FROM camera_capture_deletions')
    assert {name: db.all('SELECT * FROM ' + name) for name in names} == before
