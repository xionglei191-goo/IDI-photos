"""Background savings must preserve evidence, concurrent edits and live guards."""
import json
import os

import pytest

from idi.db import now, uid
from idi.project_followup import ProjectFollowup
from tests.conftest import image_bytes
from tests.test_archive_tree import managed_file
from tests.test_automatic_uploads import tick
from tests.test_background_review_performance import count_connections
from tests.test_checkin_reminders import gap_flow
from tests.test_platform_preparation import platform
from tests.test_project_followup import followup, channel, stage


def automatic(followup):
    return followup.review(followup.request_review(automatic=True)['id'])


def two_cases(services, project, case, photo):
    stage(services, case, 'survey')
    other = services['domain'].save_case({'project_id': project['id'], 'title': '第二案件'})
    stage(services, other, 'survey')
    first = photo('navy', stage='survey', role='problem')
    second = services['importer'].add_file(image_bytes('red'), '另一案件.png', case_id=other['id'],
                                          metadata={'stage': 'survey', 'role': 'problem'})
    return other, first, second


def test_new_photos_review_one_case_and_leave_other_issues_untouched(services, followup, project, case, photo):
    other, _, _ = two_cases(services, project, case, photo)
    assert followup.review()['cases'] == 2
    before = services['db'].all('SELECT * FROM followup_issues WHERE case_id=? ORDER BY id', (other['id'],))
    for color in ('green', 'blue', 'yellow'):
        photo(color, stage='survey', role='problem')
    result = automatic(followup)
    assert result['review_mode'] == 'incremental'
    assert (result['cases'], result['photos'], result['eligible_cases']) == (1, 4, 2)
    assert services['db'].all('SELECT * FROM followup_issues WHERE case_id=? ORDER BY id', (other['id'],)) == before
    assert automatic(followup)['cases'] == 0


def test_reassignment_reviews_source_and_destination(services, followup, project, case, photo):
    other, first, _ = two_cases(services, project, case, photo)
    followup.review()
    services['domain'].update_photo(first['id'], {'case_id': other['id']})
    result = automatic(followup)
    assert result['cases'] == 2 and result['photos'] == 2
    assert all(i['case_id'] == other['id'] for i in followup.issues() if i['photo_id'] == first['id'])


@pytest.mark.parametrize('change', ['template', 'configuration'])
def test_global_review_inputs_invalidate_every_case(services, followup, project, case, photo, change):
    two_cases(services, project, case, photo)
    followup.review()
    if change == 'configuration':
        services['db'].set_setting('recognition', {'model': 'new-model', 'vision_enabled': False})
    else:
        with services['db'].tx() as con:
            con.execute("UPDATE templates SET requirements='[]' WHERE id='insurer-v2'")
    assert automatic(followup)['cases'] == 2


def test_audit_readback_does_not_recheck_cases(services, followup, project, case, photo):
    two_cases(services, project, case, photo)
    followup.review()
    with services['db'].tx() as con:
        con.execute('UPDATE cases SET revision=revision+1,updated_at=?,workflow_verified_at=?,remote_updated_at=?',
                    (now(), now(), now()))
    assert automatic(followup)['cases'] == 0


def test_incremental_basis_survives_restart_and_manual_request_is_full(services, followup, project, case, photo):
    two_cases(services, project, case, photo)
    followup.review()
    restarted = ProjectFollowup(followup.settings, followup.db, followup.domain, followup.storage,
                                followup.connectors, followup.recognizer)
    restarted.photo_ai = followup.photo_ai
    photo('green', stage='survey', role='problem')
    assert automatic(restarted)['cases'] == 1
    queued = restarted.request_review(automatic=True)
    assert restarted.request_review()['id'] == queued['id']
    result = restarted.review(queued['id'])
    assert result['review_mode'] == 'full' and result['cases'] == 2


def test_changes_during_review_remain_due(services, followup, project, case, photo, monkeypatch):
    other, _, _ = two_cases(services, project, case, photo)
    followup.review()
    photo('green', stage='survey', role='problem')
    original = followup.analyse_photo
    edited = []

    def analyse(*args, **kwargs):
        if not edited:
            with services['db'].tx() as con:
                con.execute('UPDATE cases SET title=? WHERE id=?', ('检查期间修改的事项', other['id']))
            edited.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(followup, 'analyse_photo', analyse)
    assert automatic(followup)['cases'] == 1
    assert automatic(followup)['cases'] == 1
    assert automatic(followup)['cases'] == 0


def test_completed_ai_work_only_invalidates_its_photo_case(services, followup, project, case, photo):
    _, first, _ = two_cases(services, project, case, photo)
    followup.review()
    with services['db'].tx() as con:
        con.execute("INSERT INTO photo_ai_tasks(id,task_key,photo_id,kind,source,signature,config_key,state,created_at,updated_at) VALUES(?,?,?,'quality','followup',?,?,'completed',?,?)",
                    (uid(), uid(), first['id'], uid(), uid(), now(), now()))
    assert automatic(followup)['cases'] == 1


def traced_connections(db, monkeypatch):
    queries = []
    original = db.connect

    def connect():
        con = original()
        con.set_trace_callback(queries.append)
        return con

    monkeypatch.setattr(db, 'connect', connect)
    return queries


def test_idle_archive_scan_does_not_write_file_rows(services, photo, monkeypatch):
    for i in range(12):
        photo('#%06x' % (i * 1000), stage='survey')
    tree = services['archive_tree']
    assert tree.sync()['state'] == 'completed'
    queries = traced_connections(services['db'], monkeypatch)
    assert tree.sync(force=True)['state'] == 'completed'
    assert not [q for q in queries if q.startswith('UPDATE archive_tree_entries')]
    assert queries.count('BEGIN IMMEDIATE') == 2  # Start/end status only.


def test_archive_changed_stats_are_verified_then_batched(services, photo, monkeypatch):
    pictures = [photo(color, stage='survey') for color in ('red', 'green', 'blue')]
    tree = services['archive_tree']
    tree.sync()
    for picture in pictures:
        path = managed_file(services, 'photo:' + picture['id'])
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
    queries = traced_connections(services['db'], monkeypatch)
    assert tree.sync()['state'] == 'completed'
    assert len([q for q in queries if q.startswith('UPDATE archive_tree_entries')]) == 3
    assert queries.count('BEGIN IMMEDIATE') == 3  # One metadata batch.
    for picture in pictures:
        row = services['db'].one('SELECT * FROM archive_tree_entries WHERE entry_key=?', ('photo:' + picture['id'],))
        assert row['mtime_ns'] == managed_file(services, 'photo:' + picture['id']).stat().st_mtime_ns


def test_unchanged_official_recheck_skips_rule_rewrites_but_still_reads_website(gap_flow, services, case, monkeypatch):
    auto, _, _, _, writes, row = gap_flow()
    before = services['domain'].case(case['id'])['workflow_verified_at']
    original_writes = list(writes)
    monkeypatch.setattr(auto.rules, 'process', lambda *a, **k: pytest.fail('unchanged photo rules rerun'))
    latest = tick(auto, case['id'])
    assert latest['state'] == 'awaiting_confirmation' and latest['detail']['pending_checkins']
    assert services['domain'].case(case['id'])['workflow_verified_at'] != before
    assert writes == original_writes


@pytest.mark.parametrize('change', ['new_photo', 'dirty_pipeline', 'damaged_original'])
def test_changed_or_damaged_material_is_reprocessed(gap_flow, services, case, monkeypatch, change):
    auto, add, _, _, _, _ = gap_flow()
    picture = services['db'].one('SELECT p.id,b.stored_path FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE case_id=?', (case['id'],))
    if change == 'new_photo':
        add('purple', 'work', 'building_checkin')
    elif change == 'dirty_pipeline':
        with services['db'].tx() as con:
            con.execute('UPDATE photo_pipeline SET dirty=1 WHERE photo_id=?', (picture['id'],))
    else:
        services['storage'].resolve(picture['stored_path']).write_bytes(b'damaged original')
    calls, original = [], auto.rules.process

    def process(photo_id, *args, **kwargs):
        calls.append(photo_id)
        return original(photo_id, *args, **kwargs)

    monkeypatch.setattr(auto.rules, 'process', process)
    tick(auto, case['id'])
    assert calls
    if change == 'damaged_original':
        assert services['db'].one('SELECT state FROM photo_pipeline WHERE photo_id=?', (picture['id'],))['state'] == 'storage_error'


def test_automatic_signature_reuses_one_read_connection(gap_flow, case, monkeypatch):
    auto, _, _, _, _, _ = gap_flow()
    calls = count_connections(auto.db, monkeypatch)
    auto.signature(case['id'])
    assert len(calls) == 1
