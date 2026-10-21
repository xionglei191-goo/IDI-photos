import json

import pytest

from conftest import image_bytes
from idi.db import dump, now
from idi.domain import DomainError
from idi.photo_deletion import delete_photos


def test_delete_exception_updates_lists_and_counts_keeps_audit_and_original(client, services):
    photo_id = services['importer'].add_file(image_bytes(), '不用的照片.png')['photo_id']
    db = services['db']
    before_tree = db.one('SELECT revision FROM archive_tree_state')['revision']
    preview = client.post('/api/photos/delete-preview', json={'photo_ids': [photo_id]}).json()
    assert preview['items'][0]['original_name'] == '不用的照片.png'
    revision = preview['items'][0]['revision']
    assert services['domain'].summary()['exceptions'] == 1
    response = client.delete(f'/api/photos/{photo_id}?revision={revision}')
    assert response.status_code == 200 and response.json()['deleted_count'] == 1
    assert client.get('/api/photos?review=exceptions').json()['total'] == 0
    assert services['domain'].summary()['exceptions'] == 0
    assert client.get(f'/api/photos/{photo_id}').status_code == 404
    assert client.get(f'/api/photos/{photo_id}/original').status_code == 404
    assert client.get(f'/api/photos/{photo_id}/thumbnail').status_code == 404
    record = db.one("SELECT * FROM audit WHERE entity_id=? AND action='delete'", (photo_id,))
    assert json.loads(record['before_json'])['sources'][0]['original_name'] == '不用的照片.png'
    blob = db.one('SELECT * FROM blobs')
    assert services['storage'].resolve(blob['stored_path']).read_bytes() == image_bytes()
    assert db.one('SELECT revision FROM archive_tree_state')['revision'] > before_tree
    assert db.all('PRAGMA foreign_key_check') == []


def test_bulk_delete_is_atomic_for_missing_and_changed_photos(client, services, photo):
    one, two = photo(), photo('red')
    ids = [one['id'], two['id']]
    revisions = {p['id']: p['revision'] for p in (one, two)}
    services['domain'].update_photo(two['id'], {'notes': '同时进行的核对'})
    result = client.post('/api/photos/delete', json={'photo_ids': ids, 'revisions': revisions})
    assert result.status_code == 409
    assert services['domain'].photos()['total'] == 2
    assert client.post('/api/photos/delete', json={'photo_ids': [one['id'], 'missing']}).status_code == 409
    assert client.post('/api/photos/delete', json={'photo_ids': ids, 'revisions': {one['id']: 1}}).status_code == 400
    assert client.post('/api/photos/delete', json={'photo_ids': []}).status_code == 400
    assert services['db'].one("SELECT COUNT(*) AS n FROM audit WHERE action='delete'")['n'] == 0


def test_bulk_delete_preserves_other_cases_sharing_original_and_prunes_jobs(client, services, case, project, photo):
    one, two = photo(), photo('red')
    other_case = services['domain'].save_case({'project_id': project['id'], 'title': '保留的其他案件'})
    kept = services['importer'].add_file(image_bytes(), '其他案件相同原图.png', case_id=other_case['id'])['photo_id']
    mixed = services['jobs'].enqueue('recognize', {'photo_ids': [one['id'], kept]})
    cancelled = services['jobs'].enqueue('recognize', {'photo_ids': [two['id']]})
    result = client.post('/api/photos/delete', json={'photo_ids': [one['id'], two['id'], one['id']]})
    assert result.status_code == 200 and result.json()['deleted_count'] == 2
    assert client.get(f'/api/photos/{kept}/original').content == image_bytes()
    assert services['domain'].projects()[0]['photo_count'] == 1
    assert next(c for c in services['domain'].cases() if c['id'] == case['id'])['photo_count'] == 0
    assert services['jobs'].get(mixed['id'])['payload']['photo_ids'] == [kept]
    assert services['jobs'].get(cancelled['id'])['state'] == 'cancelled'
    assert client.post(f"/api/jobs/{cancelled['id']}/retry").status_code == 409


def test_delete_cleans_relationships_and_local_drafts_preserves_remote_evidence(client, services, case, photo):
    one, kept = photo(), photo('red')
    pid, cid, db, stamp = one['id'], case['id'], services['db'], now()
    connector = services['connectors'].save({'name': '删除验证入口', 'kind': 'feed', 'config': {}})
    batch = services['importer'].create_batch('upload', case_id=cid)
    with db.tx() as con:
        con.execute("INSERT INTO messages(id,connector_id,source_key,received_at,media_state,photo_id) VALUES('message',?,'source',?,'saved',?)", (connector['id'], stamp, pid))
        con.execute("INSERT INTO import_items VALUES('item',?,'a.png','imported',?,NULL,?)", (batch, pid, stamp))
        con.execute("INSERT INTO visits(id,case_id,stage,visit_date,created_at) VALUES('visit',?,'work','2026-10-19',?)", (cid, stamp))
        con.execute("INSERT INTO visit_shared_checkins VALUES(?,'visit',?)", (pid, stamp))
        con.execute("INSERT INTO photo_quality_reviews VALUES(?,'sig','completed','{}','[]','',NULL,?)", (pid, stamp))
        con.execute("INSERT INTO photo_rule_reviews VALUES(?,'sig','completed','{}','[]',NULL,?)", (pid, stamp))
        con.execute("INSERT INTO photo_ai_tasks(id,task_key,photo_id,kind,source,signature,config_key,created_at,updated_at) VALUES('ai','ai',?,'quality','followup','sig','config',?,?)", (pid, stamp, stamp))
        con.execute("INSERT INTO recognitions VALUES('recognition',?,'local_ocr','completed','文字','{}','[]',NULL,?)", (pid, stamp))
        con.execute("INSERT INTO followup_issues(id,issue_key,project_id,case_id,photo_id,kind,title,detail,action,evidence,signature,first_seen_at,last_seen_at,changed_at) VALUES('issue','issue',?, ?,?,'quality','照片模糊','detail','action','{}','sig',?,?,?)", (case['project_id'], cid, pid, stamp, stamp, stamp))
        con.execute("INSERT INTO feishu_agent_tasks(id,dedupe_key,connector_id,binding_revision,kind,chat_id,input_json,created_at) VALUES('task','task',?,1,'images','chat',?,?)", (connector['id'], dump({'photo_ids': [pid], 'total_images': 1}), stamp))
        con.execute("INSERT INTO feishu_agent_images VALUES('task',?,?,1,?,'summary','{}','needs_check',?)", (pid, one['blob_hash'], cid, stamp))
        con.execute("INSERT INTO platform_drafts(case_id,stage,values_json,files_json,updated_at) VALUES(?,'survey',?,?,?)", (cid, dump({'auto': '删除的依据', 'manual': '人工保留'}), dump({'images': [pid, kept['id']]}), stamp))
        con.execute("INSERT INTO platform_draft_field_sources VALUES(?,'survey','auto','automatic','删除的依据','OCR',?,1,'sig',?)", (cid, dump([pid]), stamp))
        con.execute("INSERT INTO platform_draft_exclusions VALUES(?,'survey','images',?,?)", (cid, pid, stamp))
        for review_id, state in [('old', 'prepared'), ('remote', 'save_verified')]:
            con.execute('INSERT INTO platform_review_batches(id,case_id,stage,state,fingerprint,snapshot,created_at) VALUES(?,?,\'survey\',?,\'sig\',\'{}\',?)', (review_id, cid, state, stamp))
    result = client.post('/api/photos/delete', json={'photo_ids': [pid]})
    assert result.status_code == 200, result.text
    assert db.all('PRAGMA foreign_key_check') == []
    assert db.one("SELECT media_state,photo_id FROM messages WHERE id='message'") == {'media_state': 'deleted', 'photo_id': None}
    assert db.one("SELECT status FROM import_items WHERE id='item'")['status'] == 'deleted'
    assert db.one("SELECT state FROM feishu_agent_tasks WHERE id='task'")['state'] == 'cancelled'
    assert db.one("SELECT state,photo_id FROM followup_issues WHERE id='issue'") == {'state': 'dismissed', 'photo_id': None}
    assert db.one("SELECT revision FROM visits WHERE id='visit'")['revision'] > 1
    draft = db.one('SELECT * FROM platform_drafts')
    assert json.loads(draft['files_json']) == {'images': [kept['id']]}
    assert json.loads(draft['values_json']) == {'manual': '人工保留'}
    assert db.one("SELECT state FROM platform_review_batches WHERE id='old'")['state'] == 'stale'
    assert db.one("SELECT state FROM platform_review_batches WHERE id='remote'")['state'] == 'save_verified'
    assert not db.one('SELECT * FROM photo_pipeline WHERE photo_id=?', (pid,))


@pytest.mark.parametrize('worker', ['recognize', 'photo_ai', 'feishu'])
def test_running_photo_job_blocks_entire_delete(client, services, photo, worker):
    one, two = photo(), photo('red')
    if worker == 'recognize':
        job = services['jobs'].enqueue('recognize', {'photo_ids': [one['id']]})
        with services['db'].tx() as con:
            con.execute("UPDATE jobs SET state='running' WHERE id=?", (job['id'],))
    elif worker == 'photo_ai':
        with services['db'].tx() as con:
            con.execute("INSERT INTO photo_ai_tasks(id,task_key,photo_id,kind,source,signature,config_key,state,created_at,updated_at) VALUES('ai','ai',?,'quality','followup','sig','config','running',?,?)", (one['id'], now(), now()))
    else:
        connector = services['connectors'].save({'name': '正在处理的入口', 'kind': 'feed', 'config': {}})
        with services['db'].tx() as con:
            con.execute("INSERT INTO feishu_agent_tasks(id,dedupe_key,connector_id,binding_revision,kind,chat_id,input_json,state,created_at) VALUES('task','task',?,1,'images','chat',?,'running',?)", (connector['id'], dump({'photo_ids': [one['id']]}), now()))
    assert client.post('/api/photos/delete', json={'photo_ids': [one['id'], two['id']]}).status_code == 409
    assert services['domain'].photos()['total'] == 2


def test_retry_old_import_does_not_restore_deleted_photo(client, services):
    response = client.post('/api/import/upload', data={'auto_ocr': 'false'}, files=[
        ('files', ('good.png', image_bytes(), 'image/png')),
        ('files', ('bad.png', b'broken', 'image/png'))]).json()
    services['jobs'].run_one()
    photo_id = services['jobs'].get(response['job']['id'])['result']['photo_ids'][0]
    assert client.delete('/api/photos/' + photo_id).status_code == 200
    assert client.post('/api/jobs/' + response['job']['id'] + '/retry').status_code == 200
    services['jobs'].run_one()
    assert services['domain'].photos()['total'] == 0
    replacement = services['importer'].add_file(image_bytes(), '手工重新导入.png')
    assert replacement['photo_id'] != photo_id


def test_late_ocr_does_not_restore_deleted_photo_or_write_failed_record(services, photo, monkeypatch):
    item = photo()
    def late_ocr(path):
        delete_photos(services['domain'], [item['id']])
        return '2026-10-19 10:00', []
    monkeypatch.setattr(services['recognizer'], 'local_ocr', late_ocr)
    with pytest.raises(DomainError, match='已删除'):
        services['recognizer'].recognize(item['id'])
    assert services['db'].all('SELECT * FROM recognitions') == []
    assert services['db'].all('PRAGMA foreign_key_check') == []
