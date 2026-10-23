import json
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from idi.db import now
from idi.default_packages import DefaultPackages
from idi.domain import DomainError
from idi.package_content import content_fingerprint
from idi.picc_records import PICC_FORMAT
from tests.test_platform_preparation import platform, enter_stage


def submitted(services, platform, case, state='1'):
    platform.test_row['repairAcceptanceState'] = state
    enter_stage(platform, case['id'], 'acceptance')
    services['default_packages'].run_once()


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


def test_inputs_changed_or_template_changed_create_new_immutable_version(client, services, platform, project, case, photo):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    original = client.get(first['package']['download_url']).content
    photo('red', stage='acceptance', role='finished')
    services['default_packages'].run_once()
    assert finish(services, case)['package']['version'] == 2
    services['domain'].save_project({**project, 'insurer_template_id': PICC_FORMAT}, project['id'])
    services['default_packages'].run_once()
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
def test_legacy_fingerprint_rebased_without_job_or_reviving_deleted_package(services, platform, case, photo, deleted):
    photo(stage='work', role='construction')
    submitted(services, platform, case)
    first = finish(services, case)
    package = services['reporter'].package(first['package']['id'])
    if deleted:
        services['reporter'].delete(package['id'], package['deletion_token'])
    # An upgrade must compare the saved snapshot under the current rules instead
    # of trusting the previous algorithm's stored fingerprint.
    with services['db'].tx() as con:
        con.execute("UPDATE case_default_packages SET fingerprint='legacy-fingerprint' WHERE case_id=?", (case['id'],))
    services['domain'].save_case({'insurer_template_id': PICC_FORMAT}, case['id'])
    restarted = DefaultPackages(services['db'], services['domain'], services['reporter'], services['jobs'])
    restarted.run_once()
    status = restarted.get(case['id'])
    assert status['state'] == ('deleted' if deleted else 'ready')
    assert status['package'] == (None if deleted else first['package'])
    tracker = services['db'].one('SELECT * FROM case_default_packages WHERE case_id=?', (case['id'],))
    assert tracker['fingerprint'] == content_fingerprint(package['manifest'])
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
