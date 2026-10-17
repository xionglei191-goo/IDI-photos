import json
from copy import deepcopy

import pytest

from idi.domain import DomainError
from idi.workflow import remote_stage
from tests.test_platform_preparation import platform


@pytest.mark.parametrize('row,expected', [
    ({}, 'survey'),
    ({'siteSurveyState': '1', 'repairMethod': '2'}, 'repair'),
    ({'siteSurveyState': '1', 'repairAcceptanceFlowStatus': '1'}, 'acceptance'),
    ({'repairAcceptanceFlowStatus': '2'}, 'settlement'),
    ({'repairAcceptanceFlowStatus': '9'}, 'closed'),
])
def test_finished_intake_does_not_close_repair_subprocess(row, expected):
    assert remote_stage({'scope': 'repair', 'row': row, 'claim': {'flowStatus': '9', 'endStatus': '1'}}) == expected


def test_pending_insurer_review_still_blocks_repair():
    assert remote_stage({'scope': 'repair', 'row': {'siteSurveyState': '1'}, 'claim': {'flowStatus': '3'}}) == 'insurer_review'


def test_independent_official_lists_exclude_local_sample_and_keep_stage_filters(platform, services, case, monkeypatch):
    workflow, domain = services['workflow'], services['domain']
    local = domain.save_case({'project_id': case['project_id'], 'title': '本地样例', 'building': '2', 'room': '201'})
    intake = deepcopy(platform.test_row)
    intake.update(flowStatus='9', endStatus='0')
    repair = deepcopy(platform.test_row)
    platform.test_claim.update(flowStatus='9', endStatus='0')
    lists = {'intake': [intake], 'repair': [repair]}
    def search(**args):
        assert args.get('end_status') is None
        values = deepcopy(lists[args['scope']])
        return {'items': values, 'total': len(values)}
    monkeypatch.setattr(platform.client, 'search', search)
    result = workflow.sync({}, lambda *_: None)
    assert result['state'] == 'completed'
    assert domain.case(case['id'])['workflow_stage'] == 'repair'
    assert domain.case(case['id'])['platform_lists'] == ['intake', 'repair']
    assert workflow.board('intake')['total'] == workflow.board('work')['total'] == 1
    assert workflow.board('work')['items'][0]['id'] == case['id']
    assert workflow.board('intake', status='pending_intake')['total'] == 0
    assert workflow.board('intake', status='pending_intake')['source_total'] == 1
    assert workflow.board('intake', source='local')['items'][0]['id'] == local['id']
    assert workflow.board('work', source='local')['total'] == 0
    # A case can leave a source list without deleting its archive or remaining list membership.
    lists['repair'] = []
    assert workflow.sync({}, lambda *_: None)['state'] == 'completed'
    assert workflow.board('work')['total'] == 0
    assert workflow.board('intake')['total'] == 1
    assert domain.case(case['id'])['platform_lists'] == ['intake']


def test_incomplete_sync_keeps_previous_published_lists(platform, services, case, monkeypatch):
    workflow = services['workflow']
    workflow.sync({}, lambda *_: None)
    before = services['db'].all('SELECT * FROM platform_list_entries ORDER BY scope,position')
    marker = workflow.sync_status()['lists']
    platform.test_row['updateTime'] = '2026-10-16 08:00:00'
    monkeypatch.setattr(platform.client, 'snapshot_from_row', lambda *_: (_ for _ in ()).throw(DomainError('fixture failure', 502)))
    assert workflow.sync({}, lambda *_: None)['state'] == 'partial'
    assert services['db'].all('SELECT * FROM platform_list_entries ORDER BY scope,position') == before
    assert workflow.sync_status()['lists'] == marker


def test_completed_empty_official_lists_do_not_fall_back_to_old_local_workflow(platform, services, case, monkeypatch):
    workflow = services['workflow']
    workflow.sync({}, lambda *_: None)
    monkeypatch.setattr(platform.client, 'search', lambda **_: {'items': [], 'total': 0})
    assert workflow.sync({}, lambda *_: None)['state'] == 'completed'
    assert workflow.board('intake')['source'] == 'platform'
    assert workflow.board('work')['total'] == 0
    assert services['domain'].case(case['id'])


def test_ended_repair_remains_in_unfiltered_official_list(platform, services, monkeypatch):
    platform.test_row['repairAcceptanceFlowStatus'] = '9'
    platform.test_claim['flowStatus'] = '9'
    services['workflow'].sync({}, lambda *_: None)
    board = services['workflow'].board('work')
    assert board['total'] == 1 and board['counts'] == {'closed': 1}
    assert board['items'][0]['stage_label'] == '已结束'
