import sqlite3
from pathlib import Path

import pytest

from conftest import image_bytes
from idi import case_codes
from idi.db import Database
from idi.domain import Domain, DomainError
from idi.recognition import extract_fields
from tests.test_archive_tree import managed_file
from tests.test_platform_preparation import platform
from tests.test_workflow_v2 import intake_review


def test_prefix_change_keeps_identity_photos_search_and_watermark(services, client, project, case, photo):
    domain, db = services['domain'], services['db']
    picture = photo()
    photos_before = db.all('SELECT * FROM photos')
    sources_before = db.all('SELECT * FROM photo_sources')
    response = client.patch(f"/api/projects/{project['id']}", json={**project, 'short_prefix': 'BYF'})
    assert response.status_code == 200 and response.json()['updated_case_codes'] == 1
    current = domain.case(case['id'])
    assert current['short_code'] == 'BYF-1-201-01'
    assert current['short_code_aliases'] == [case['short_code']]
    assert current['revision'] == case['revision'] + 1
    for key in ('id', 'project_id', 'platform_report_code', 'policy_number', 'workflow_stage', 'report_at'):
        assert current[key] == case[key]
    assert db.all('SELECT * FROM photos') == photos_before
    assert db.all('SELECT * FROM photo_sources') == sources_before
    for code in (case['short_code'], current['short_code']):
        assert [r['id'] for r in client.get('/api/cases', params={'query': code}).json()] == [case['id']]
        assert [r['id'] for r in domain.photos(query=code)['items']] == [picture['id']]
        assert services['workflow'].board(query=code)['items'][0]['id'] == case['id']
    assert services['workflow'].detail(case['id'])['watermark']['text'].startswith('编号：BYF-1-201-01\n')
    assert client.patch(f"/api/cases/{case['id']}", json={'title': '旧表单', 'revision': case['revision']}).status_code == 409
    assert domain.case(case['id'])['title'] == case['title']


def test_repeated_prefix_changes_and_switch_back_retain_all_aliases(services, project, case):
    domain, db = services['domain'], services['db']
    unit = domain.save_case({'project_id': project['id'], 'title': '单元房', 'building': 'A1', 'unit': '2', 'room': '902'})
    public = domain.save_case({'project_id': project['id'], 'title': '公共走道', 'building': 'B1', 'location_type': 'public'})
    missing = domain.save_case({'project_id': project['id'], 'title': '位置待补'})
    for prefix in ('BYF', 'NEW', project['short_prefix']):
        assert domain.save_project({**project, 'short_prefix': prefix}, project['id'])['updated_case_codes'] == 3
        assert domain.case(unit['id'])['short_code'] == prefix + '-A1-U2-902-01'
        assert domain.case(public['id'])['short_code'] == prefix + '-B1-GG-01'
        assert domain.case(missing['id'])['short_code'] is None
        for known in ('CHBYF-1-201-01', 'BYF-1-201-01'):
            assert domain.cases(query=known)[0]['id'] == case['id']
    current = domain.case(case['id'])
    assert current['short_code'] == case['short_code']
    assert set(current['short_code_aliases']) == {'BYF-1-201-01', 'NEW-1-201-01'}
    before = db.all('SELECT * FROM cases ORDER BY id')
    assert domain.save_project(project, project['id'])['updated_case_codes'] == 0
    db.initialize()
    assert db.all('SELECT * FROM cases ORDER BY id') == before


def test_old_prefix_reuse_cannot_reassign_history_and_allocates_free_sequence(services, project, case):
    domain, db = services['domain'], services['db']
    domain.save_project({**project, 'short_prefix': 'BYF'}, project['id'])
    other_project = domain.save_project({'name': '另一个项目', 'short_prefix': project['short_prefix']})
    with pytest.raises(DomainError, match='历史编号'):
        domain.save_case({'project_id': other_project['id'], 'title': '不能占用旧编号', 'short_code': case['short_code']})
    second = domain.save_case({'project_id': other_project['id'], 'title': '新项目相同房号', 'building': '1', 'room': '201'})
    assert second['short_code'] == 'CHBYF-1-201-02'
    # Free the active prefix; its historical codes must still remain reserved.
    domain.save_project({**other_project, 'short_prefix': 'OTHER'}, other_project['id'])
    third = domain.save_case({'project_id': project['id'], 'title': '原项目第二案', 'building': '1', 'room': '201'})
    assert third['short_code'] == 'BYF-1-201-02'
    fourth = domain.save_case({'project_id': project['id'], 'title': '原项目第三案', 'building': '1', 'room': '201'})
    domain.save_project(project, project['id'])
    assert domain.case(case['id'])['short_code'] == 'CHBYF-1-201-01'
    assert domain.case(fourth['id'])['short_code'] == 'CHBYF-1-201-03'
    assert domain.case(third['id'])['short_code'] == 'CHBYF-1-201-04'
    assert domain.cases(query=second['short_code'])[0]['id'] == second['id']
    assert domain.cases(query=third['short_code'])[0]['id'] == third['id']
    with pytest.raises(sqlite3.IntegrityError, match='another case'):
        with db.tx() as con:
            con.execute('UPDATE cases SET short_code=? WHERE id=?', (case['short_code'], second['id']))


@pytest.mark.parametrize('conflict', [None, 'room', 'project', 'unknown'])
def test_legacy_watermark_recognition_preserves_conflict_checks(services, project, case, monkeypatch, conflict):
    domain, recognizer = services['domain'], services['recognizer']
    domain.save_project({**project, 'short_prefix': 'NEW'}, project['id'])
    code = 'CHBYF-1-201-99' if conflict == 'unknown' else case['short_code']
    room = '301' if conflict == 'room' else '201'
    project_name = '其他项目' if conflict == 'project' else project['name']
    text = f'编号：{code}\n施工项目：{project_name}\n施工区域：1栋-{room}号-客厅\n施工内容：查勘打卡'
    imported = services['importer'].add_file(image_bytes(), '旧水印.png')
    monkeypatch.setattr(recognizer, 'local_ocr', lambda path: (text, []))
    result = recognizer.recognize(imported['photo_id'])
    picture = domain.photo(imported['photo_id'])
    assert result['auto_assigned'] == (conflict is None)
    assert picture['case_id'] == (case['id'] if conflict is None else None)
    assert picture['recognition_state'] == ('recognized' if conflict is None else 'conflict')
    if conflict is None:
        assert result['candidates'][0]['reasons'] == ['历史短编号完全一致']
        assert result['candidates'][0]['short_code'] == 'NEW-1-201-01'
        assert recognizer.match(text, {**extract_fields(text), 'short_code': 'new－1－201－01'})[1] == case['id']


def test_failed_project_save_rolls_back_all_number_changes(services, project, case, monkeypatch):
    db, domain = services['db'], services['domain']
    other = domain.save_project({'name': '其他项目', 'short_prefix': 'TAKEN'})
    before = {table: db.all(f'SELECT * FROM {table}') for table in ('projects', 'cases', 'case_code_aliases', 'audit')}
    with pytest.raises(DomainError, match='已经存在'):
        domain.save_project({**project, 'short_prefix': other['short_prefix']}, project['id'])
    real = case_codes.sync_project_codes
    def fail_after_update(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError('模拟事务中断')
    monkeypatch.setattr('idi.domain.sync_project_codes', fail_after_update)
    with pytest.raises(RuntimeError, match='事务中断'):
        domain.save_project({**project, 'short_prefix': 'NEW'}, project['id'])
    for table, rows in before.items():
        assert db.all(f'SELECT * FROM {table}') == rows


def test_prefix_change_rebuilds_browsing_copy_but_preserves_original_and_user_file(services, project, case, photo):
    domain, tree = services['domain'], services['archive_tree']
    picture = photo()
    tree.sync()
    old = managed_file(services, 'photo:' + picture['id'])
    content = old.read_bytes()
    manual = old.parent / '人工备注.txt'
    manual.write_text('需要保留', encoding='utf-8')
    domain.save_project({**project, 'short_prefix': 'NEW'}, project['id'])
    assert tree.sync()['state'] == 'completed'
    updated = managed_file(services, 'photo:' + picture['id'])
    assert 'NEW-1-201-01' in str(updated) and updated.read_bytes() == content
    assert not old.exists() and manual.read_text(encoding='utf-8') == '需要保留'
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (picture['blob_hash'],))
    assert services['storage'].verify(blob)


def test_prefix_change_invalidates_prior_review_without_rewriting_snapshot(platform, services, project, case):
    batch = intake_review(platform, services, case)
    before = services['reviews'].get(batch['id'])['snapshot']
    link_before = services['db'].all('SELECT * FROM platform_links')
    services['domain'].save_project({**project, 'short_prefix': 'NEW'}, project['id'])
    assert services['reviews'].get(batch['id'])['snapshot'] == before
    assert services['db'].all('SELECT * FROM platform_links') == link_before
    with pytest.raises(DomainError, match='重新审核'):
        services['reviews'].confirm(batch['id'], batch['fingerprint'], True)
    assert services['reviews'].get(batch['id'])['state'] == 'stale'


def legacy_database(path):
    db = Database(path)
    with db.read() as con:
        for script in sorted((Path(__file__).parents[1] / 'idi/migrations').glob('*.sql')):
            version = int(script.name.split('_')[0])
            if version >= 7:
                continue
            con.executescript(script.read_text(encoding='utf-8'))
            con.execute("INSERT INTO schema_version VALUES(?,'2026-10-15')", (version,))
        con.execute("INSERT INTO projects(id,name,short_prefix,created_at,updated_at) VALUES('project','原项目','NEW','2026-10-15','2026-10-15')")
        for i, code in enumerate(('OLD-1-201-01', 'NEW-1-201-01', 'OLD-1-201-02')):
            con.execute("INSERT INTO cases(id,project_id,title,building,room,short_code,created_at,updated_at) VALUES(?,'project','已有案件','1','201',?,'2026-10-15','2026-10-15')", (f'case{i}', code))
    return db


def test_upgrade_repairs_prior_prefix_edits_keeps_suffixes_and_is_idempotent(tmp_path):
    db = legacy_database(tmp_path / 'legacy.sqlite3')
    db.initialize()
    domain = Domain(db)
    assert [domain.case(f'case{i}')['short_code'] for i in range(3)] == ['NEW-1-201-03', 'NEW-1-201-01', 'NEW-1-201-02']
    assert domain.case('case0')['short_code_aliases'] == ['OLD-1-201-01']
    assert domain.case('case2')['short_code_aliases'] == ['OLD-1-201-02']
    assert db.one('SELECT MAX(version) AS version FROM schema_version')['version'] == 8
    before = {table: db.all(f'SELECT * FROM {table}') for table in ('cases', 'case_code_aliases', 'audit')}
    db.initialize()
    for table, rows in before.items():
        assert db.all(f'SELECT * FROM {table}') == rows
    assert db.all('PRAGMA foreign_key_check') == []


def test_failed_upgrade_rolls_back_schema_and_case_numbers_then_can_retry(tmp_path, monkeypatch):
    db = legacy_database(tmp_path / 'legacy.sqlite3')
    before = db.all('SELECT * FROM cases')
    real = case_codes.sync_project_codes
    def fail_after_update(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError('模拟迁移中断')
    with monkeypatch.context() as patcher:
        patcher.setattr(case_codes, 'sync_project_codes', fail_after_update)
        with pytest.raises(RuntimeError, match='迁移中断'):
            db.initialize()
    assert db.all('SELECT * FROM cases') == before
    assert db.one('SELECT MAX(version) AS version FROM schema_version')['version'] == 6
    assert db.one("SELECT name FROM sqlite_master WHERE name='case_code_aliases'") is None
    db.initialize()
    assert Domain(db).case('case0')['short_code'] == 'NEW-1-201-03'
