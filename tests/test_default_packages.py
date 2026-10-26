import json
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from idi.db import dump, now, uid
from idi.default_packages import DefaultPackages
from idi.domain import DomainError
from idi.picc_records import PICC_FORMAT
from tests.test_platform_preparation import platform as base_platform, enter_stage


@pytest.fixture
def platform(base_platform):
    # These tests exercise the default-package lifecycle with local photos.
    # Official download/merge/failure behavior has dedicated package-media tests.
    for detail in (base_platform.test_survey, base_platform.test_repair):
        for key in ('buildingFileInfoList', 'houseFileInfoList', 'fileInfoList', 'preRepairFileInfo', 'recoveredFileInfo'):
            if key in detail:
                detail[key] = []
    return base_platform


def submitted(services, platform, case, state='1'):
    platform.test_row['repairAcceptanceState'] = state
    enter_stage(platform, case['id'], 'acceptance')
    if state != '0':
        verified_submission(services, case['id'])
    services['default_packages'].run_once()


def verified_submission(services, case_id, *, confirmed_at=None, state='verified', action='submit', save_only=0):
    """A durable submission receipt; real confirmation is covered by upload tests."""
    review_id, stamp = uid(), confirmed_at or now()
    snapshot = {'link': services['platform'].link(case_id), 'file_fields': []}
    with services['db'].tx() as con:
        con.execute('INSERT INTO platform_review_batches(id,case_id,stage,state,fingerprint,snapshot,result,created_at,confirmed_at,finished_at,save_only) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (review_id, case_id, 'repair', state, 'test-submission', dump(snapshot), dump({'action': action, 'business_submitted': True}), stamp, stamp, stamp, save_only))
    return review_id


def finish(services, case):
    while services['jobs'].run_one():
        pass
    services['default_packages'].run_once()
    return services['default_packages'].get(case['id'])


def test_project_default_inheritance_and_existing_case_override(client, services, project, case):
    assert project['insurer_template_id'] == case['insurer_template_id'] == 'insurer-v2'
    edited = client.patch('/api/projects/' + project['id'], json={**project, 'insurer_template_id': PICC_FORMAT})
    assert edited.status_code == 200 and edited.json()['insurer_template_id'] == PICC_FORMAT
    assert services['domain'].case(case['id'])['insurer_template_id'] == 'insurer-v2'
    created = client.post('/api/cases', json={'project_id': project['id'], 'title': '新案件'}).json()
    assert created['insurer_template_id'] == PICC_FORMAT
    # Older clients editing other project fields must preserve the selected default.
    assert client.patch('/api/projects/' + project['id'], json={'name': project['name']}).json()['insurer_template_id'] == PICC_FORMAT
    assert client.patch('/api/projects/' + project['id'], json={**project, 'insurer_template_id': 'missing'}).status_code == 400


def test_official_new_case_inherits_project_default(services, project):
    services['domain'].save_project({**project, 'insurer_template_id': PICC_FORMAT}, project['id'])
    result = services['workflow'].apply_snapshot({'row': {'reportReceivingId': 'new-remote', 'reportCode': 'NEW001',
        'communityName': project['name'], 'flowStatus': '2', 'siteSurveyState': '1'}, 'scope': 'repair', 'fetched_at': now()})
    assert services['domain'].case(result['case_id'])['insurer_template_id'] == PICC_FORMAT


def test_prepare_only_after_verified_submission_and_reuse_across_restart(client, services, platform, case, photo):
    p = photo(stage='work', role='construction')
    auto = services['default_packages']
    enter_stage(platform, case['id'], 'repair')
    auto.run_once()
    assert not services['jobs'].list() and auto.get(case['id'])['state'] == 'waiting'
    # A stale/unsubmitted repair state is not a formal submit even at a later node.
    submitted(services, platform, case, state='0')
    assert auto.get(case['id'])['state'] == 'waiting' and not services['jobs'].list()
    submitted(services, platform, case)
    assert auto.get(case['id'])['state'] == 'preparing'
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: auto.prepare(case['id']), range(4)))
    assert len(services['jobs'].list()) == 1
    assert services['db'].one('SELECT user_id FROM jobs')['user_id'] is None
    status = finish(services, case)
    assert status['state'] == 'ready' and status['package']['version'] == 1
    downloaded = client.get(status['package']['download_url'])
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as bundle:
        assert len([n for n in bundle.namelist() if not n.endswith('/')]) == 1
    restarted = DefaultPackages(services['db'], services['domain'], services['reporter'], services['jobs'])
    restarted.run_once()
    assert restarted.get(case['id'])['package'] == status['package'] and len(services['jobs'].list()) == 1
    # A normal official refresh only changes timestamps, never creates another version.
    submitted(services, platform, case)
    assert finish(services, case)['package'] == status['package']
    assert services['domain'].photo(p['id'])['blob_hash'] == p['blob_hash']


def test_changed_inputs_and_template_require_manual_new_version(client, services, platform, project, case, photo):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    original = client.get(first['package']['download_url']).content
    photo('red', stage='acceptance', role='finished')
    services['default_packages'].run_once()
    assert finish(services, case)['package'] == first['package']
    services['default_packages'].retry(case['id'])
    assert finish(services, case)['package']['version'] == 2
    services['domain'].save_project({**project, 'insurer_template_id': PICC_FORMAT}, project['id'])
    services['default_packages'].run_once()
    assert services['default_packages'].get(case['id'])['state'] == 'manual'
    services['default_packages'].retry(case['id'])
    third = finish(services, case)
    assert third['package']['version'] == 3 and third['template_id'] == PICC_FORMAT
    assert 'wordprocessingml' in client.get(third['package']['download_url']).headers['content-type']
    assert client.get(first['package']['download_url']).content == original


@pytest.mark.parametrize('template_id', ['insurer-v2', PICC_FORMAT])
def test_case_requirement_switch_keeps_project_default_package(client, services, platform, project, case, photo, template_id):
    services['domain'].save_project({**project, 'insurer_template_id': template_id}, project['id'])
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    original = client.get(first['package']['download_url']).content
    for preference in (PICC_FORMAT, 'insurer-v2', PICC_FORMAT):
        services['domain'].save_case({'insurer_template_id': preference}, case['id'])
        submitted(services, platform, case)
        assert finish(services, case)['package'] == first['package']
        assert len(services['jobs'].list()) == 1
    assert client.get(first['package']['download_url']).content == original
    assert len(services['reporter'].packages()) == 1


@pytest.mark.parametrize('deleted', [False, True])
def test_completed_historical_default_not_rebuilt_on_restart(services, platform, case, photo, deleted):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    package = services['reporter'].package(first['package']['id'])
    if deleted:
        services['reporter'].delete(package['id'], package['deletion_token'])
    # Completed requests do not enroll an old case for future rebuilds.
    with services['db'].tx() as con:
        con.execute("UPDATE case_default_packages SET fingerprint='legacy-fingerprint' WHERE case_id=?", (case['id'],))
    services['domain'].save_case({'insurer_template_id': PICC_FORMAT}, case['id'])
    restarted = DefaultPackages(services['db'], services['domain'], services['reporter'], services['jobs'])
    restarted.run_once()
    status = restarted.get(case['id'])
    assert status['state'] == ('deleted' if deleted else 'ready')
    assert status['package'] == (None if deleted else first['package'])
    tracker = services['db'].one('SELECT * FROM case_default_packages WHERE case_id=?', (case['id'],))
    assert tracker['fingerprint'] == 'legacy-fingerprint'
    assert tracker['package_id'] == package['id']
    assert len(services['jobs'].list()) == 1
    assert services['db'].one('SELECT COUNT(*) AS n FROM packages')['n'] == 1


def test_deleted_default_not_recreated_until_explicit_retry(client, services, platform, case, photo):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    package = services['reporter'].package(first['package']['id'])
    services['reporter'].delete(package['id'], package['deletion_token'])
    services['default_packages'].run_once()
    assert services['default_packages'].get(case['id'])['state'] == 'deleted'
    assert len(services['jobs'].list()) == 1
    assert client.post(f"/api/cases/{case['id']}/default-package").status_code == 200
    assert finish(services, case)['package']['version'] == 2


def test_failure_is_visible_and_retry_recovers(client, services, platform, case, photo, monkeypatch):
    photo(stage='work', role='construction')
    build = services['reporter']._build
    def fail(*args):
        raise DomainError('隔离测试：磁盘暂不可用')
    monkeypatch.setattr(services['reporter'], '_build', fail)
    submitted(services, platform, case)
    status = finish(services, case)
    assert status['state'] == 'failed' and '磁盘' in status['error']
    services['default_packages'].run_once()
    assert len(services['jobs'].list()) == 1
    monkeypatch.setattr(services['reporter'], '_build', build)
    assert client.post(f"/api/cases/{case['id']}/default-package").status_code == 200
    assert finish(services, case)['state'] == 'ready'


def test_missing_classification_waits_then_prepares_and_withdrawal_hides_link(client, services, platform, case, photo):
    p = photo()
    submitted(services, platform, case)
    blocked = services['default_packages'].get(case['id'])
    assert blocked['state'] == 'blocked' and '分类' in blocked['error']
    services['domain'].update_photo(p['id'], {'photo_group': 'construction'})
    services['default_packages'].run_once()
    status = finish(services, case)
    assert status['state'] == 'ready'
    platform.test_row['repairAcceptanceState'] = '0'
    enter_stage(platform, case['id'], 'repair')
    services['default_packages'].run_once()
    assert services['default_packages'].get(case['id'])['state'] == 'waiting'
    assert client.post(f"/api/cases/{case['id']}/default-package").status_code == 409
    assert client.get(status['package']['download_url']).status_code == 200


def test_status_and_retry_keep_project_scope(app, services, case, photo, platform):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    finish(services, case)
    user = services['access'].save_user({'username': 'default-worker', 'display_name': '项目人员', 'password': 'person-test-password', 'system_role': 'user'})
    with TestClient(app, headers={'X-IDI-Request': '1'}) as client:
        assert client.post('/api/auth/login', json={'username': 'default-worker', 'password': 'person-test-password'}).status_code == 200
        url = f"/api/cases/{case['id']}/default-package"
        assert client.get(url).status_code == 403 and client.post(url).status_code == 403
        services['access'].save_members(case['project_id'], {'members': [{'user_id': user['id'], 'role': 'member'}]})
        assert client.get(url).status_code == 200 and client.post(url).status_code == 403


def test_picc_auto_respects_dedup_and_exclusions(services, platform, project, case, photo):
    from docx import Document
    services['domain'].save_project({**project, 'insurer_template_id': PICC_FORMAT}, project['id'])
    p = photo('blue', stage='survey', role='building_checkin', watermark_time='2026-10-23T08:00:00')
    services['domain'].reuse_checkin(p['id'], {'case_id': case['id'], 'revision': p['revision']})
    other = photo('red', stage='work', role='construction')
    picc = services['reporter'].picc
    draft = picc.get(case['id'])
    picc.save(case['id'], {**draft, 'photo_action': {'id': p['id'], 'excluded': True}})
    submitted(services, platform, case)
    status = finish(services, case)
    package = services['reporter'].package(status['package']['id'])
    assert [p['id'] for p in package['manifest']['photos']] == [other['id']]
    assert len(Document(services['storage'].resolve(package['zip_path'])).inline_shapes) == 1
    assert services['domain'].photos(case_id=case['id'])['total'] == 3


def test_idle_scan_reuses_one_connection_and_skips_content(services, platform, case, photo, monkeypatch):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    auto, db = services['default_packages'], services['db']
    auto.run_once()  # Observe the final ready tracker after completion.
    connections = []
    connect = db.connect
    def counted():
        connections.append(1)
        return connect()
    monkeypatch.setattr(db, 'connect', counted)
    monkeypatch.setattr(services['reporter'], '_content', lambda *args: pytest.fail('Idle scan assembled content'))
    for _ in range(3):
        auto.run_once()
    assert len(connections) == 3
    assert auto.get(case['id'])['package'] == first['package']


@pytest.mark.parametrize('change', ['source', 'visit'])
def test_source_and_visit_edits_wait_for_manual_request(services, platform, case, photo, change):
    p = photo(stage='work', role='construction')
    visit = services['domain'].save_visit(case['id'], {'stage': 'work', 'visit_date': '2026-10-23',
        'notes': '原维修记录', 'photo_ids': [p['id']]})
    submitted(services, platform, case)
    first = finish(services, case)
    auto = services['default_packages']
    auto.run_once()
    # Neither edit updates the photo revision. Both affect exported content.
    with services['db'].tx() as con:
        if change == 'source':
            con.execute("UPDATE photo_sources SET original_name='更正来源.png' WHERE photo_id=?", (p['id'],))
        else:
            con.execute("UPDATE visits SET notes='已复核的维修记录' WHERE id=?", (visit['id'],))
    auto.run_once()
    assert finish(services, case)['package'] == first['package']
    auto.retry(case['id'])
    second = finish(services, case)
    assert second['package']['version'] == first['package']['version'] + 1
    manifest = services['reporter'].package(second['package']['id'])['manifest']
    if change == 'source':
        assert manifest['photos'][0]['sources'][0]['original_name'] == '更正来源.png'
    else:
        assert manifest['visits'][0]['notes'] == '已复核的维修记录'


def test_completed_default_is_not_rebuilt_by_periodic_audit(services, platform, case, photo, monkeypatch):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    auto = services['default_packages']
    auto.run_once()
    calls = []
    prepare = auto.prepare
    def counted(*args, **kwargs):
        calls.append(args[0])
        return prepare(*args, **kwargs)
    monkeypatch.setattr(auto, 'prepare', counted)
    auto.run_once()
    assert calls == []
    monkeypatch.setattr('idi.default_packages.RECHECK_SECONDS', 0)
    auto.run_once()
    assert calls == []
    monkeypatch.setattr('idi.default_packages.RECHECK_SECONDS', 300)
    package = services['reporter'].package(first['package']['id'])
    services['storage'].resolve(package['zip_path']).unlink()
    auto.run_once()
    assert auto.get(case['id'])['state'] == 'failed'
    assert calls == []
    auto.retry(case['id'])
    assert finish(services, case)['state'] == 'ready'


def test_new_submission_during_preparation_is_checked_next_poll(services, platform, case, photo, monkeypatch):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    finish(services, case)
    auto = services['default_packages']
    auto.run_once()
    prepare, calls = auto.prepare, []
    def edit_during_prepare(case_id, **kwargs):
        calls.append(case_id)
        prepare(case_id, **kwargs)
        if len(calls) == 1:
            services['domain'].save_case({'description': '核对时刚收到的新资料'}, case_id)
            verified_submission(services, case_id)
    monkeypatch.setattr(auto, 'prepare', edit_during_prepare)
    verified_submission(services, case['id'])
    auto.run_once()
    auto.run_once()
    assert calls == [case['id'], case['id']]
    assert finish(services, case)['package']['version'] == 2


@pytest.mark.parametrize('stage', ['acceptance', 'settlement', 'closed'])
@pytest.mark.parametrize('has_local_photo', [False, True])
def test_historical_status_and_refresh_never_create_requests(services, platform, case, photo, stage, has_local_photo):
    if has_local_photo:
        photo(stage='work', role='construction')
    platform.test_row['repairAcceptanceState'] = '1'
    auto = services['default_packages']
    for _ in range(2):
        enter_stage(platform, case['id'], stage)
        auto.run_once()
    assert auto.get(case['id'])['state'] == 'manual'
    assert not services['jobs'].list()
    assert not services['db'].all('SELECT * FROM default_package_requests')
    assert not services['db'].all('SELECT * FROM case_default_packages')


@pytest.mark.parametrize('state,action,save_only,old', [
    ('verified', 'submit', 0, True), ('submitted', 'submit', 0, False),
    ('uncertain', 'submit', 0, False), ('reviewed', 'submit', 0, False),
    ('verified', 'save', 0, False), ('verified', 'submit', 1, False),
])
def test_old_or_unconfirmed_submission_cannot_trigger_history(services, platform, case, photo, state, action, save_only, old):
    photo(stage='work', role='construction')
    platform.test_row['repairAcceptanceState'] = '1'
    enter_stage(platform, case['id'], 'acceptance')
    verified_submission(services, case['id'], confirmed_at='2020-01-01T00:00:00Z' if old else None,
                        state=state, action=action, save_only=save_only)
    services['default_packages'].run_once()
    assert not services['jobs'].list()
    assert not services['db'].all('SELECT * FROM default_package_requests')


def test_new_submission_waits_for_verified_receipt_and_only_runs_once(services, platform, case, photo):
    photo(stage='work', role='construction')
    platform.test_row['repairAcceptanceState'] = '1'
    enter_stage(platform, case['id'], 'acceptance')
    review_id = verified_submission(services, case['id'], state='submitted')
    auto = services['default_packages']
    auto.run_once()
    assert not services['jobs'].list()
    with services['db'].tx() as con:
        con.execute("UPDATE platform_review_batches SET state='verified' WHERE id=?", (review_id,))
    auto.run_once()
    first = finish(services, case)
    assert first['state'] == 'ready'
    restarted = DefaultPackages(services['db'], services['domain'], services['reporter'], services['jobs'])
    for _ in range(2):
        enter_stage(platform, case['id'], 'acceptance')
        restarted.run_once()
    assert len(services['jobs'].list()) == 1
    assert services['db'].all('SELECT state,source_id FROM default_package_requests') == [{'state': 'handled', 'source_id': review_id}]


def test_upgrade_baseline_preserves_packages_without_enrolling_old_cases(services, platform, case, photo):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    package = services['reporter'].package(first['package']['id'])
    original = services['storage'].resolve(package['zip_path']).read_bytes()
    db = services['db']
    # Reconstruct the prior schema in this test's isolated database, then run
    # the real migration. Previously confirmed submissions must stay historical.
    with db.tx() as con:
        con.execute("UPDATE platform_review_batches SET confirmed_at='2020-01-01T00:00:00Z'")
        con.execute('DROP TABLE default_package_requests')
        con.execute("DELETE FROM settings WHERE key='default_package_auto_policy'")
        con.execute('DELETE FROM schema_version WHERE version=39')
    db.initialize()
    services['domain'].save_case({'description': '历史案件备注修订'}, case['id'])
    photo('red', stage='work', role='construction')
    services['default_packages'].run_once()
    assert len(services['jobs'].list()) == 1
    assert not db.all('SELECT * FROM default_package_requests')
    assert services['storage'].resolve(package['zip_path']).read_bytes() == original
    assert services['default_packages'].get(case['id'])['package'] == first['package']
