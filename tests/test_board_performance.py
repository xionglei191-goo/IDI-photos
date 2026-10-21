from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from idi.db import dump
from idi.workflow import ARCHIVED, INTAKE
from tests.test_platform_preparation import platform


def test_read_snapshot_is_consistent_nested_and_thread_scoped(services):
    db = services['db']
    db.set_setting('snapshot-test', {'value': 'before'})
    with db.read_snapshot() as outer:
        assert db.setting('snapshot-test') == {'value': 'before'}
        with db.read_snapshot() as inner:
            assert inner is outer
        # Another worker may commit normally while this reader keeps its view.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(db.set_setting, 'snapshot-test', {'value': 'after'}).result(timeout=5)
        assert db.setting('snapshot-test') == {'value': 'before'}
        with pytest.raises(RuntimeError, match='read-only'):
            db.set_setting('snapshot-test', {'value': 'not allowed'})
    assert db.setting('snapshot-test') == {'value': 'after'}
    with pytest.raises(ValueError, match='fixture'):
        with db.read_snapshot():
            raise ValueError('fixture')
    db.set_setting('snapshot-test', {'value': 'recovered'})
    assert db.setting('snapshot-test') == {'value': 'recovered'}


def test_board_pages_before_preparing_rows_and_preserves_filters(platform, services, case, monkeypatch):
    db, domain, workflow = (services[key] for key in ('db', 'domain', 'workflow'))
    workflow.sync({}, lambda *_: None)
    other = domain.save_project({'name': '分页验证第二项目'})
    stages = ['pending_intake', 'survey', 'repair', 'closed', 'insurer_review', 'acceptance']
    with db.tx() as con:
        for i in range(75):
            case_id, record_id = f'page-case-{i}', f'{900000 + i}'
            con.execute("""INSERT INTO cases(id,project_id,title,building,room,workflow_stage,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (case_id, other['id'] if i % 5 == 0 else case['project_id'],
                         f'分页案件{i}', str(i % 3 + 1), str(1000 + i), stages[i % len(stages)],
                         '2026-10-20T00:00:00Z', f'2026-10-20T00:{i // 60:02}:{i % 60:02}Z'))
            con.execute('INSERT INTO case_code_aliases VALUES(?,?,?)',
                        (f'OLD-PAGE-{i}', case_id, '2026-10-20'))
            if i % 3:
                con.execute('INSERT INTO platform_links VALUES(?,?,?,?,?,?,?,?)',
                            (case_id, record_id, '', '', dump({'scope': 'repair', 'row': {'reportDate': '2026-10-19'}}),
                             '2026-10-20' if i % 3 == 1 else None, 1, '2026-10-20'))
            for scope in ('intake', 'repair'):
                if scope == 'repair' and i % 4 == 0:
                    continue
                con.execute("""INSERT INTO platform_list_entries
                               SELECT ?,?,?,?,snapshot,sync_run_id,synced_at
                               FROM platform_list_entries WHERE case_id=? LIMIT 1""",
                            (scope, record_id, case_id, 100 - i, case['id']))
    scenarios = [
        dict(board='intake', source='platform', page=2, page_size=13),
        dict(board='work', source='platform', project_id=case['project_id'], building='2', status='repair'),
        dict(board='intake', source='platform', query='OLD-PAGE-7'),
        dict(board='work', source='local', archived=True, page_size=3, page=2),
        dict(board='work', source='local', archived=False),
        dict(board='intake', source='local', status='insurer_review'),
        dict(board='work', source='platform', query="%' OR 1=1 --"),
        dict(board='intake', source='platform', page=99),
    ]
    original_preview, original_connect = platform.preview, db.connect
    for args in scenarios:
        candidates = domain.cases(project_id=args.get('project_id'), query=args.get('query'))
        candidates = [c for c in candidates if not args.get('building') or c['building'] == args['building']]
        entries = db.all('SELECT case_id,position FROM platform_list_entries WHERE scope=?',
                         ('intake' if args['board'] == 'intake' else 'repair',))
        order = {r['case_id']: r['position'] for r in entries}
        if args['source'] == 'platform':
            expected = sorted((c for c in candidates if c['id'] in order), key=lambda c: order[c['id']])
            source_total = len(entries)
        else:
            expected = [c for c in candidates if not c['platform_linked'] and
                        (c['workflow_stage'] in INTAKE) == (args['board'] == 'intake') and
                        (args.get('archived') or c['workflow_stage'] not in ARCHIVED)]
            source_total = len(expected)
        counts = dict(Counter(c['workflow_stage'] for c in expected))
        expected = [c for c in expected if not args.get('status') or c['workflow_stage'] == args['status']]
        page_size, page = args.get('page_size', 30), args.get('page', 1)
        expected_ids = [c['id'] for c in expected[(page - 1) * page_size:page * page_size]]
        prepared, connections = [], []
        def preview(case_id, stage):
            prepared.append(case_id)
            return original_preview(case_id, stage)
        def connect():
            con = original_connect()
            connections.append(con)
            return con
        with monkeypatch.context() as patch:
            patch.setattr(platform, 'preview', preview)
            patch.setattr(db, 'connect', connect)
            patch.setattr(domain, 'cases', lambda **_: pytest.fail('board loaded the full case archive'))
            result = workflow.board(**args)
        assert [c['id'] for c in result['items']] == expected_ids
        assert prepared == expected_ids
        assert (result['total'], result['source_total'], result['counts']) == (len(expected), source_total, counts)
        assert len(connections) == 1


def test_board_refresh_reads_current_photo_and_stage_data(services, case, photo):
    workflow, db = services['workflow'], services['db']
    before = workflow.board('intake', source='local')
    assert before['items'][0]['photo_count'] == 0
    photo(stage='survey', role='problem')
    after = workflow.board('intake', source='local')
    assert after['items'][0]['photo_count'] == 1
    assert after['items'][0]['photo_counts']['survey_problem'] == 1
    with db.tx() as con:
        con.execute("UPDATE cases SET workflow_stage='repair' WHERE id=?", (case['id'],))
    assert workflow.board('intake', source='local')['total'] == 0
    assert workflow.board('work', source='local')['items'][0]['id'] == case['id']
