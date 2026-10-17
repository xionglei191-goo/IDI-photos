import pytest

from idi.domain import DomainError
from idi.platform import PlatformPreparation
from tests.test_platform_preparation import platform, enter_stage, draft_data


LOW = 'estimate_restoration_cost_01'
HIGH = 'estimate_restoration_cost_02'
KEY = 'estimateRestorationCostCode'


def ready(platform, case):
    platform.test_survey.clear()
    platform.test_survey['repairMethod'] = '1'
    enter_stage(platform, case['id'], 'survey')


def test_default_is_explicit_persistent_and_only_fills_eligible_empty_field(platform, services, case, client):
    ready(platform, case)
    assert KEY not in platform.prefill_draft(case['id'], 'survey', set())['plan']['draft']['values']
    response = client.put('/api/platform/prefill-defaults', json={KEY: LOW})
    assert response.status_code == 200 and response.json() == {KEY: LOW}
    restarted = PlatformPreparation(services['db'], services['domain'], services['storage'], platform.client)
    assert restarted.prefill_defaults() == {KEY: LOW}
    plan = platform.prefill_draft(case['id'], 'survey', set())['plan']
    assert plan['values'][KEY] == LOW
    assert plan['field_sources'][KEY]['origin'] == 'automatic'
    assert '已确认的预填默认设置' in plan['field_sources'][KEY]['source']
    assert not plan['field_sources'][KEY]['needs_review']
    assert 'repairUserId' not in plan['values']
    assert client.get('/api/platform/prefill-defaults').json() == {KEY: LOW}


@pytest.mark.parametrize('value', [HIGH, ''])
def test_default_never_overwrites_manual_cost_or_explicit_clear(platform, case, value):
    ready(platform, case)
    platform.save_prefill_defaults({KEY: LOW})
    platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', {'repairMethod': '1', KEY: value}))
    plan = platform.prefill_draft(case['id'], 'survey', set())['plan']
    assert plan['draft']['values'][KEY] == value
    assert plan['field_sources'][KEY]['origin'] == 'manual'


def test_actual_official_cost_has_priority_over_default(platform, case):
    ready(platform, case)
    platform.test_survey[KEY] = HIGH
    enter_stage(platform, case['id'], 'survey')
    platform.save_prefill_defaults({KEY: LOW})
    plan = platform.prefill_draft(case['id'], 'survey', set())['plan']
    assert plan['values'][KEY] == HIGH
    # A value already loaded from the official record is preserved as-is; it
    # must not be relabelled as a value derived from the user's default.
    assert KEY not in plan['field_sources']
    assert plan['field_suggestions'][KEY]['source'] == '官网已保存的现场查勘记录'


def test_changing_and_clearing_default_invalidates_then_recomputes_automatic_value(platform, services, case):
    ready(platform, case)
    platform.save_prefill_defaults({KEY: LOW})
    platform.prefill_draft(case['id'], 'survey', set())
    signature = services['automatic_uploads'].signature(case['id'])
    platform.save_prefill_defaults({KEY: HIGH})
    assert services['automatic_uploads'].signature(case['id']) != signature
    assert platform.preview(case['id'], 'survey')['field_sources'][KEY]['stale']
    assert platform.prefill_draft(case['id'], 'survey', set())['plan']['values'][KEY] == HIGH
    platform.save_prefill_defaults({KEY: None})
    assert KEY not in platform.prefill_draft(case['id'], 'survey', set())['plan']['draft']['values']


@pytest.mark.parametrize('data', [{KEY: 'unknown'}, {KEY: True}, {'repairUserId': 'someone'}])
def test_invalid_default_does_not_change_saved_configuration(platform, data):
    platform.save_prefill_defaults({KEY: LOW})
    with pytest.raises(DomainError):
        platform.save_prefill_defaults(data)
    assert platform.prefill_defaults() == {KEY: LOW}


def test_default_does_not_apply_to_no_repair_or_closed_stage(platform, case):
    platform.save_prefill_defaults({KEY: LOW})
    platform.test_survey.clear()
    platform.test_survey['repairMethod'] = '3'
    enter_stage(platform, case['id'], 'survey')
    assert KEY not in platform.prefill_draft(case['id'], 'survey', set())['plan']['draft']['values']
    enter_stage(platform, case['id'], 'repair')
    assert not platform.prefill_draft(case['id'], 'survey', set())['changed']
