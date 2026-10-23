import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from idi.db import dump, now, uid
from idi.domain import DomainError


@pytest.fixture
def case(case, services):
    return services['domain'].save_case({'platform_report_code': 'ZLBH202610132005010577'}, case['id'])


@pytest.fixture
def photo(photo):
    def add(*args, **metadata):
        return photo(*args, **{'stage': 'survey', **metadata})
    return add


def generate(client, services, ids, **options):
    response = client.post('/api/packages', json={'photo_ids': ids, **options})
    assert response.status_code == 200, response.text
    while services['jobs'].run_one():
        pass
    job = client.get('/api/jobs/' + response.json()['id']).json()
    assert job['state'] == 'completed', job
    return job


def remove(client, package):
    current = client.get('/api/packages/' + package['id']).json()
    return client.request('DELETE', '/api/packages/' + package['id'], json={'expected_token': current['deletion_token']})


def test_repeated_requests_share_pending_job_and_existing_zip(client, services, case, photo):
    ids = [photo()['id'], photo('red')['id']]
    first = client.post('/api/packages', json={'photo_ids': ids}).json()
    pending = client.post('/api/packages', json={'photo_ids': list(reversed(ids))}).json()
    assert pending['id'] == first['id']
    while services['jobs'].run_one():
        pass
    original = client.get('/api/jobs/' + first['id']).json()['result']['packages'][0]
    before = client.get(original['download_url']).content
    with services['db'].tx() as con:
        con.execute('UPDATE cases SET revision=revision+1,updated_at=? WHERE id=?', (now(), case['id']))
        con.execute('UPDATE photos SET revision=revision+1 WHERE case_id=?', (case['id'],))
    repeat = generate(client, services, ids, title='重复点击')
    assert repeat['result']['reused_count'] == 1
    assert repeat['result']['packages'][0]['id'] == original['id']
    assert client.get(original['download_url']).content == before
    assert len(client.get('/api/packages').json()) == 1
    assert len(list((services['storage'].settings.data_dir / 'exports').iterdir())) == 1


def test_concurrent_enqueue_and_build_do_not_add_versions(services, case, photo):
    ids = [photo()['id']]
    reporter, jobs = services['reporter'], services['jobs']
    with ThreadPoolExecutor(max_workers=4) as pool:
        queued = list(pool.map(lambda _: reporter.enqueue_export(jobs, ids), range(4)))
    assert len({job['id'] for job in queued}) == 1
    while jobs.run_one():
        pass
    with ThreadPoolExecutor(max_workers=4) as pool:
        packages = list(pool.map(lambda _: reporter.build(uid(), case['id'], ids), range(4)))
    assert len({p['id'] for p in packages}) == 1
    assert len(reporter.packages()) == 1


@pytest.mark.parametrize('template_id', ['insurer-v2', 'picc-record-v1'])
def test_case_template_preference_does_not_change_explicit_export(client, services, case, photo, template_id):
    ids = [photo()['id']]
    body = {'photo_ids': ids, 'template_id': template_id}
    first = client.post('/api/packages', json=body).json()
    alternate = 'picc-record-v1' if case['insurer_template_id'] == 'insurer-v2' else 'insurer-v2'
    services['domain'].save_case({'insurer_template_id': alternate}, case['id'])
    pending = client.post('/api/packages', json=body).json()
    assert pending['id'] == first['id']
    while services['jobs'].run_one():
        pass
    original = client.get('/api/jobs/' + first['id']).json()['result']['packages'][0]
    downloaded = client.get(original['download_url']).content
    services['domain'].save_case({'insurer_template_id': case['insurer_template_id']}, case['id'])
    repeat = generate(client, services, ids, template_id=template_id)['result']
    assert repeat['packages'][0]['id'] == original['id'] and repeat['reused_count'] == 1
    assert client.get(original['download_url']).content == downloaded
    assert len(client.get('/api/packages').json()) == 1


@pytest.mark.parametrize('change', ['photo', 'selection', 'case', 'template', 'visit'])
def test_actual_exported_content_changes_get_new_version(client, services, case, photo, change):
    domain = services['domain']
    visit = domain.save_visit(case['id'], {'stage': 'survey', 'visit_date': '2026-10-23', 'notes': '初次查勘'})
    first_photo = photo(stage='survey', role='problem', visit_id=visit['id'])
    ids = [first_photo['id']]
    first = generate(client, services, ids)['result']['packages'][0]
    before = client.get(first['download_url']).content
    if change == 'photo':
        domain.update_photo(ids[0], {'process': '检查客厅墙面'})
    elif change == 'selection':
        ids = [photo('red')['id']]
    elif change == 'case':
        domain.save_case({'title': '补充核对后的案件内容'}, case['id'])
    elif change == 'template':
        template = next(t for t in domain.templates() if t['id'] == 'general')
        template['requirements'][0]['minimum'] += 1
        domain.save_template(template, 'general')
    else:
        current = next(v for v in domain.visits(case['id']) if v['id'] == visit['id'])
        domain.save_visit(case['id'], {'stage': visit['stage'], 'visit_date': visit['visit_date'],
                                     'revision': current['revision'], 'notes': '补充查勘记录'}, visit['id'])
    second = generate(client, services, ids)['result']['packages'][0]
    assert second['version'] == 2 and second['id'] != first['id']
    assert client.get(first['download_url']).content == before


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'failed'])
def test_unusable_packages_are_not_reused(client, services, photo, damage):
    ids = [photo()['id']]
    first = generate(client, services, ids)['result']['packages'][0]
    row = services['reporter'].package(first['id'])
    path = services['storage'].resolve(row['zip_path'])
    if damage == 'missing':
        path.unlink()
    elif damage == 'corrupt':
        path.write_bytes(b'damaged')
    else:
        with services['db'].tx() as con:
            con.execute("UPDATE packages SET state='failed' WHERE id=?", (first['id'],))
    second = generate(client, services, ids)['result']['packages'][0]
    assert second['version'] == 2
    assert client.get(second['download_url']).content[:2] == b'PK'


def test_legacy_duplicate_packages_reuse_latest_valid_snapshot(client, services, photo):
    ids = [photo()['id']]
    first = generate(client, services, ids)['result']['packages'][0]
    row = services['db'].one('SELECT * FROM packages WHERE id=?', (first['id'],))
    # Existing installations may already contain repeated snapshots; keep their history intact.
    newer = uid()
    manifest = json.loads(row['manifest'])
    manifest.update(package_id=newer, version=2, created_at=now())
    with services['db'].tx() as con:
        con.execute('INSERT INTO packages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (newer, row['case_id'], row['template_id'], row['parent_id'], 2, row['title'], row['state'], dump(manifest), row['zip_path'], row['zip_hash'], now()))
    repeat = generate(client, services, ids)['result']
    assert repeat['packages'][0]['id'] == newer and repeat['reused_count'] == 1
    assert len(client.get('/api/packages').json()) == 2


def test_delete_hides_package_keeps_originals_receipts_and_version_sequence(client, services, case, photo):
    p = photo()
    first = generate(client, services, [p['id']])['result']['packages'][0]
    receipt = client.post(f"/api/packages/{first['id']}/receipt", data={'state': 'accepted', 'note': '已接收'},
                          files={'file': ('回执.txt', b'received', 'text/plain')}).json()
    zip_path = services['storage'].resolve(services['reporter'].package(first['id'])['zip_path'])
    original_zip = zip_path.read_bytes()
    child = services['reporter'].build(uid(), case['id'], [p['id']], parent_id=first['id'])
    assert remove(client, first).status_code == 200
    assert client.get('/api/packages/' + first['id']).status_code == 404
    assert client.get(first['download_url']).status_code == 404
    for url in ('/api/packages', '/api/packages?case_id=' + case['id']):
        assert [item['id'] for item in client.get(url).json()] == [child['id']]
    assert zip_path.read_bytes() == original_zip
    assert services['domain'].photo(p['id'])['blob_hash'] == p['blob_hash']
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (p['blob_hash'],))
    assert services['storage'].verify(blob)
    assert client.get(f"/api/receipts/{receipt['events'][0]['id']}/attachment").content == b'received'
    assert client.get(child['download_url']).status_code == 200
    with pytest.raises(DomainError, match='已删除'):
        services['reporter'].build(first['id'], case['id'], [p['id']])
    assert client.post(f"/api/packages/{first['id']}/events", json={'state': 'submitted'}).status_code == 404
    assert remove(client, child).status_code == 200
    third = generate(client, services, [p['id']])['result']['packages'][0]
    assert third['version'] == 3 and third['id'] not in (first['id'], child['id'])
    assert services['db'].one("SELECT COUNT(*) AS n FROM audit WHERE entity_type='package' AND action='delete'")['n'] == 2


def test_delete_rejects_stale_preview_and_active_generation(client, services, photo):
    p = photo()
    first = generate(client, services, [p['id']])['result']['packages'][0]
    token = client.get('/api/packages/' + first['id']).json()['deletion_token']
    assert client.post(f"/api/packages/{first['id']}/events", json={'state': 'submitted'}).status_code == 200
    assert client.request('DELETE', '/api/packages/' + first['id'], json={'expected_token': token}).status_code == 409
    services['domain'].update_photo(p['id'], {'process': '新内容'})
    assert client.post('/api/packages', json={'photo_ids': [p['id']]}).json()['state'] == 'queued'
    assert remove(client, first).status_code == 409
    while services['jobs'].run_one():
        pass
    assert remove(client, first).status_code == 200


def test_delete_requires_admin(app, client, services, photo):
    first = generate(client, services, [photo()['id']])['result']['packages'][0]
    data = client.get('/api/packages/' + first['id']).json()
    with TestClient(app, headers={'X-IDI-Request': '1'}) as anonymous:
        assert anonymous.request('DELETE', '/api/packages/' + first['id'], json={'expected_token': data['deletion_token']}).status_code == 401
        services['access'].save_user({'username': 'worker', 'password': 'isolated-worker-password', 'display_name': '只读成员', 'system_role': 'user'})
        assert anonymous.post('/api/auth/login', json={'username': 'worker', 'password': 'isolated-worker-password'}).status_code == 200
        assert anonymous.request('DELETE', '/api/packages/' + first['id'], json={'expected_token': data['deletion_token']}).status_code == 403


def test_interrupted_matching_snapshot_is_finished_before_success(services, case, photo):
    p = photo()
    reporter = services['reporter']
    original_id = uid()
    reporter._snapshot(original_id, case['id'], [p['id']], 'general', None, '')
    result = reporter.build(uid(), case['id'], [p['id']])
    assert result['id'] == original_id and result['state'] == 'ready'
    assert services['storage'].resolve(result['zip_path']).is_file()
    assert len(reporter.packages()) == 1
