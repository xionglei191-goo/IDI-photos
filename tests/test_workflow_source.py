from copy import deepcopy

import pytest

from idi.domain import DomainError
from idi.stage_access import stage_access
from idi.workflow import remote_stage
from tests.test_platform_preparation import draft_data, platform
from tests.test_workflow_v2 import intake_review


@pytest.mark.parametrize('flow', [None, '', '0', '1', '2', '3', '9'])
def test_old_intake_without_acceptance_is_unverified_regardless_of_legacy_flow(flow):
    snapshot = {'scope': 'intake', 'row': {'reportDate': '2026-09-23', 'acceptanceResult': None, 'flowStatus': flow}}
    assert remote_stage(snapshot) == 'historical_unverified'
    # A repair-list record is actual evidence, even when its intake result is missing.
    if flow in (None, '', '2', '9'):
        assert remote_stage({**snapshot, 'scope': 'repair'}) == 'survey'


@pytest.mark.parametrize('row,expected', [
    ({'reportDate': '2026-09-23', 'flowStatus': '9', 'acceptanceResult': '1'}, 'survey'),
    ({'reportDate': '2026-09-24', 'flowStatus': '1'}, 'pending_intake'),
    ({'reportDate': '2026-10-14', 'flowStatus': '0', 'reportReceivingState': '0'}, 'insurer_intake'),
    ({'reportDate': '2026-10-14', 'flowStatus': '3', 'acceptanceResult': '3'}, 'insurer_review'),
    ({'reportDate': '2026-10-14', 'flowStatus': '3', 'acceptanceResult': '2'}, 'insurer_review'),
    ({'flowStatus': '9', 'passStatus': '1', 'acceptanceResult': '2'}, 'direct_payment'),
    ({'flowStatus': '9', 'passStatus': '1', 'acceptanceResult': '3'}, 'noncoverage'),
    ({'reportDate': 'not-a-date', 'flowStatus': 'unexpected'}, None),
])
def test_boundary_exception_and_real_acceptance_branches(row, expected):
    assert remote_stage({'scope': 'intake', 'row': row}) == expected


@pytest.mark.parametrize('node', ['historical_unverified', 'insurer_intake'])
def test_non_actionable_source_states_cannot_edit_any_business_form(platform, services, client, case, node):
    snapshot = deepcopy(platform.link(case['id'])['snapshot'])
    snapshot['scope'] = 'intake'
    snapshot['row'].update(reportDate='2026-09-23' if node == 'historical_unverified' else '2026-10-14',
                           flowStatus='2' if node == 'historical_unverified' else '0', acceptanceResult=None)
    services['workflow'].apply_snapshot(snapshot, case['id'])
    for form in ('intake', 'survey', 'repair', 'acceptance'):
        access = stage_access({'workflow_stage': node}, form)
        assert not access['editable'] and access['active_stage'] is None
        url = f"/api/platform/cases/{case['id']}/stages/{form}"
        plan = client.get(url).json()
        assert '官网报案待提交' not in plan['link']['node']
        assert not any('先完成官网报案提交' in issue['message'] for issue in plan['issues'])
        assert client.put(url, json=draft_data(platform, case['id'], form)).status_code == 409
        assert client.post(url + '/package', json={'revision': 0}).status_code == 409
        assert client.post(f"/api/workflow/cases/{case['id']}/review/{form}", json={'revision': 0}).status_code == 409
    row = services['workflow'].board('intake')['items'][0]
    assert row['workflow_stage'] == node and row['action_label'] == '查看'
    assert row['next_action'] == ('查看历史记录' if node == 'historical_unverified' else '等待保司处理')
    assert services['workflow'].board('work')['total'] == 0


@pytest.mark.parametrize('branch,approval,wrong,end', [
    ('submitted_direct_payment', 'direct_payment_approved', 'noncoverage_approved', 'direct_payment'),
    ('submitted_noncoverage', 'noncoverage_approved', 'direct_payment_approved', 'noncoverage'),
])
def test_manual_insurer_review_is_only_for_the_submitted_branch(services, case, branch, approval, wrong, end):
    workflow = services['workflow']
    def record(action):
        current = services['domain'].case(case['id'])
        return workflow.record_result(case['id'], {'action': action, 'revision': current['revision'],
                                      'event_at': '2026-10-16 10:00', 'note': '实际外部办理凭据'})
    assert record(branch)['workflow_stage'] == 'insurer_review'
    with pytest.raises(DomainError, match='不一致'):
        record(wrong)
    assert record('insurance_returned')['workflow_stage'] == 'pending_intake'
    assert record(branch)['workflow_stage'] == 'insurer_review'
    assert record(approval)['workflow_stage'] == end


def test_refresh_accepted_intake_reads_repair_details_without_intake_disappearing(platform, services, case, monkeypatch):
    original = deepcopy(platform.link(case['id'])['snapshot'])
    original['scope'] = 'intake'
    original['row'].update(flowStatus='1')
    services['workflow'].apply_snapshot(original, case['id'])
    accepted = deepcopy(original)
    accepted['row'].update(flowStatus='9', acceptanceResult='1', siteSurveyState='0')
    repair = deepcopy(accepted)
    repair['scope'] = 'repair'
    repair['row'].update(siteSurveyState='1', repairAcceptanceFlowStatus='1')
    scopes = []
    def snapshot(record_id, scope):
        scopes.append(scope)
        return deepcopy(accepted if scope == 'intake' else repair)
    monkeypatch.setattr(platform.client, 'snapshot', snapshot)
    assert services['workflow'].refresh_case(case['id'])['case']['workflow_stage'] == 'acceptance'
    assert scopes == ['intake', 'repair']


def test_insurer_intake_cannot_falsely_verify_a_submitted_intake_review(platform, services, case):
    batch = intake_review(platform, services, case)
    payload = platform.build_payload(batch['snapshot'], 'submit', {})
    services['reviews']._result(batch['id'], {'payload': payload}, state='uncertain')
    platform.test_claim.update(payload)
    platform.test_row.update(flowStatus='0', reportDate='2026-10-14')
    result = services['reviews'].reconcile(batch['id'])
    assert result['state'] == 'uncertain' and 'workflow_stage' in result['result']['mismatches']
