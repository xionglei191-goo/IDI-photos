from copy import deepcopy

import pytest

from idi.domain import DomainError
from tests.test_platform_preparation import draft_data, enter_stage, platform, remote_file


@pytest.fixture
def survey_review(platform, services, case, monkeypatch):
    platform.test_survey['fileInfoList'].append(remote_file('defect-2', '第二张问题照片.jpg'))
    for key in ('buildingFileInfoList', 'houseFileInfoList', 'fileInfoList'):
        for item in platform.test_survey[key]:
            item['updateTime'] = '2026-10-31T09:36:25'
    platform.test_survey['updateTime'] = '2026-10-31T09:36:25'
    enter_stage(platform, case['id'], 'survey')
    plan = platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey'))
    review = services['reviews'].prepare(case['id'], 'survey', plan['draft']['revision'])
    platform.client.write_enabled = True
    writes = []

    def submit(stage, payload):
        writes.append(deepcopy(payload))
        platform.test_survey.update(deepcopy(payload))
        platform.test_row['siteSurveyState'] = '1'
        return {'code': 0}

    monkeypatch.setattr(platform.client, 'submit', submit)
    return review, writes


@pytest.mark.parametrize('field', ['buildingFileInfoList', 'houseFileInfoList', 'fileInfoList'])
@pytest.mark.parametrize('change', ['new_time', 'missing', 'null'])
def test_attachment_timestamp_refresh_keeps_review_and_sends_latest_metadata(
        survey_review, services, platform, case, field, change):
    review, writes = survey_review
    revision = platform.link(case['id'])['revision']
    item = platform.test_survey[field][0]
    if change == 'missing':
        item.pop('updateTime')
    else:
        item['updateTime'] = '2026-10-31T10:00:01' if change == 'new_time' else None
    expected = deepcopy(item)
    services['workflow'].refresh_case(case['id'])
    link = platform.link(case['id'])
    assert link['revision'] == revision
    assert link['snapshot']['survey'][field][0] == expected  # Original metadata remains stored.
    assert services['reviews'].snapshot(case['id'], 'survey')[1] == review['fingerprint']
    result = services['reviews'].confirm(review['id'], review['fingerprint'])
    assert result['state'] == 'verified', result
    assert len(writes) == 1 and writes[0][field][0] == expected
    assert services['reviews'].confirm(review['id'], review['fingerprint'])['state'] == 'verified'
    assert len(writes) == 1


def test_timestamp_change_during_final_remote_refresh_uses_fresh_payload(survey_review, services, platform):
    review, writes = survey_review
    platform.test_survey['fileInfoList'][0]['updateTime'] = '2026-10-31T10:00:01'
    platform.test_survey['updateTime'] = '2026-10-31T10:00:11'
    result = services['reviews'].confirm(review['id'], review['fingerprint'])
    assert result['state'] == 'verified', result
    assert writes[0]['fileInfoList'][0]['updateTime'] == '2026-10-31T10:00:01'
    assert review['snapshot']['file_fields'][2]['existing'][0]['updateTime'] == '2026-10-31T09:36:25'


@pytest.mark.parametrize('section', ['claim', 'survey', 'repair'])
def test_detail_system_update_time_does_not_invalidate_review(survey_review, services, platform, case, section):
    review, writes = survey_review
    revision = platform.link(case['id'])['revision']
    getattr(platform, 'test_' + section)['updateTime'] = '2026-10-31T10:00:11'
    services['workflow'].refresh_case(case['id'])
    link = platform.link(case['id'])
    assert link['revision'] == revision
    assert link['snapshot'][section]['updateTime'] == '2026-10-31T10:00:11'
    assert services['reviews'].snapshot(case['id'], 'survey')[1] == review['fingerprint']
    assert services['reviews'].confirm(review['id'], review['fingerprint'])['state'] == 'verified'
    assert len(writes) == 1


@pytest.mark.parametrize('change', ['id', 'fileUrl', 'fileName', 'fileSize', 'contentHash', 'createDate',
                                    'invalid', 'delete', 'add', 'reorder', 'nested_update_time'])
def test_real_attachment_changes_still_reject_old_review(survey_review, services, platform, case, change):
    review, writes = survey_review
    revision = platform.link(case['id'])['revision']
    items = platform.test_survey['fileInfoList']
    if change == 'delete':
        items.pop(0)
    elif change == 'add':
        items.append(remote_file('new-photo'))
    elif change == 'reorder':
        items.reverse()
    elif change == 'nested_update_time':
        items[0]['contentMetadata'] = {'updateTime': '2026-10-31T10:00:01'}
    else:
        items[0][change] = {'id': 'replacement', 'fileUrl': '/profile/replacement.jpg', 'fileName': '替换照片.jpg',
                            'fileSize': 256, 'contentHash': 'different-content', 'createDate': '2026-10-31 10:00:01',
                            'invalid': True}[change]
    with pytest.raises(DomainError):
        services['reviews'].confirm(review['id'], review['fingerprint'])
    result = services['reviews'].get(review['id'])
    assert result['state'] == 'stale' and not writes
    assert result['result']['remote_written'] is False and result['result']['business_submitted'] is False
    assert platform.link(case['id'])['revision'] > revision


@pytest.mark.parametrize('change', ['business', 'business_time', 'case_time', 'stage', 'personnel', 'placeholder_time'])
def test_non_attachment_changes_still_invalidate_old_review(survey_review, services, platform, change):
    review, writes = survey_review
    if change == 'business':
        platform.test_survey['defectDesc'] = '官网更新了原因说明'
    elif change == 'business_time':
        platform.test_survey['siteSurveyDate'] = '2026-10-31 09:00:00'
    elif change == 'case_time':
        platform.test_row['updateTime'] = '2026-10-31T10:00:01'
    elif change == 'stage':
        platform.test_row['siteSurveyState'] = '1'
    elif change == 'personnel':
        platform.test_personnel[0]['phone'] = '13800000003'
    else:
        platform.test_repair['supplementaryMaterialsFileInfo'][0]['updateTime'] = '2026-10-31T10:00:01'
    with pytest.raises(DomainError):
        services['reviews'].confirm(review['id'], review['fingerprint'])
    assert services['reviews'].get(review['id'])['state'] == 'stale' and not writes


def test_pre_upgrade_review_is_not_silently_approved(survey_review, services, platform, case, monkeypatch):
    from idi import review_batches

    _, writes = survey_review
    with monkeypatch.context() as old:
        old.setattr(review_batches, 'attachment_version_data', lambda items: items)
        legacy = services['reviews'].prepare(case['id'], 'survey', platform.draft(case['id'], 'survey')['revision'])
    with pytest.raises(DomainError, match='重新审核'):
        services['reviews'].confirm(legacy['id'], legacy['fingerprint'])
    assert services['reviews'].get(legacy['id'])['state'] == 'stale' and not writes
    current = services['reviews'].prepare(case['id'], 'survey', platform.draft(case['id'], 'survey')['revision'])
    assert current['fingerprint'] != legacy['fingerprint']
    assert services['reviews'].confirm(current['id'], current['fingerprint'])['state'] == 'verified'
    assert len(writes) == 1
