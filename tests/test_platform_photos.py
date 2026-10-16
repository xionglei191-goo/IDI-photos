from copy import deepcopy

import pytest

from idi.platform_photos import online_photo_counts
from tests.test_platform_preparation import platform


def file(id, kind='jpg', **extra):
    return {'id': id, 'oldFileName': f'{id}.{kind}', 'fileType': kind, 'invalid': 0, **extra}


def link(snapshot):
    return {'verified_at': '2026-10-16T10:00:00+08:00', 'report_receiving_id': 'record-1', 'snapshot': snapshot}


def test_official_counts_map_fields_deduplicate_and_keep_video_pdf_separate():
    snapshot = {'row': {'siteSurveyId': 'survey-1', 'repairAcceptanceId': 'repair-1'},
                'survey': {'buildingFileInfoList': [file('check-1')], 'houseFileInfoList': [file('check-1'), file('check-2', 'JPG')],
                           'fileInfoList': [file('problem-1'), file('video-1', 'MP4'), file('invalid', invalid=1)]},
                'repair': {'buildingFileInfoList': [file('work-1')], 'houseFileInfoList': [],
                           'preRepairFileInfo': [file('problem-1'), file('problem-copy')],
                           'recoveredFileInfo': [file('done-1'), file('not-image', 'pdf')],
                           'acceptanceCheckFileInfo': [file('acceptance', 'pdf')]}}
    before = deepcopy(snapshot)
    result = online_photo_counts(link(snapshot))
    groups = result['groups']
    assert {key: group['count'] for key, group in groups.items()} == {
        'survey_checkin': 2, 'survey_problem': 2, 'work_checkin': 1, 'completion': 1, 'construction': None}
    assert groups['survey_problem']['videos'] == 0  # The repair summary is authoritative, not added to the survey list.
    assert groups['completion']['other'] == 1
    assert groups['construction']['state'] == 'unsupported'
    assert result['read_at'] == '2026-10-16T10:00:00+08:00'
    assert snapshot == before


@pytest.mark.parametrize('snapshot,state,count', [
    ({'row': {'siteSurveyId': None}, 'survey': {}}, 'known', 0),
    ({'row': {'siteSurveyId': 'existing'}, 'survey': {}}, 'unread', None),
    ({'row': {}, 'survey': {}}, 'unread', None),
    ({'row': {}, 'survey': {'fileInfoList': []}}, 'known', 0),
    ({'row': {}, 'survey': {'fileInfoList': None}}, 'unread', None),
    ({'row': {'siteSurveyId': None}, 'details_deferred': 'unsubmitted_report'}, 'unread', None),
])
def test_absence_requires_evidence_and_never_turns_unread_into_zero(snapshot, state, count):
    value = online_photo_counts(link(snapshot))['groups']['survey_problem']
    assert value['state'] == state and value['count'] == count


def test_unlinked_case_cannot_inherit_false_zero_counts():
    groups = online_photo_counts({'snapshot': {}, 'verified_at': None})['groups']
    assert all(groups[key]['state'] == 'unlinked' and groups[key]['count'] is None
               for key in ('survey_checkin', 'survey_problem', 'work_checkin', 'completion'))


def test_partial_fields_unknown_type_and_conflicting_metadata_stay_explicit():
    groups = online_photo_counts(link({'survey': {
        'buildingFileInfoList': [file('one')],
        'fileInfoList': [file('two'), {'id': 'unknown'}, file('conflict', fileName='actually.mp4')],
    }}))['groups']
    assert groups['survey_checkin']['state'] == 'partial' and groups['survey_checkin']['count'] == 1
    assert groups['survey_problem']['state'] == 'partial' and groups['survey_problem']['count'] == 1
    assert groups['survey_problem']['unknown'] == 2


def test_normalized_names_and_url_fallback_identity():
    groups = online_photo_counts(link({'survey': {'fileInfoList': [
        {'fileName': 'image.png', 'fileUrl': '/files/image.png'},
        {'fileName': 'image.png', 'fileUrl': '/files/image.png'},
        {'fileUrl': 'https://example.invalid/files/two.JPG?download=1'},
    ]}}))['groups']
    assert groups['survey_problem']['count'] == 2
    assert groups['survey_problem']['state'] == 'known'


def test_board_keeps_official_and_local_counts_independent_without_remote_reads(platform, services, case, photo):
    platform.test_survey.update(fileInfoList=[file('remote-image'), file('remote-video', 'MP4')],
                                buildingFileInfoList=[], houseFileInfoList=[])
    platform.test_repair['preRepairFileInfo'] = [file('report-image'), *platform.test_survey['fileInfoList']]
    services['workflow'].refresh_case(case['id'])
    item = photo()
    services['domain'].update_photo(item['id'], {'photo_group': 'construction'})
    request_count = len(platform.test_requests)
    board = services['workflow'].board('work')
    assert len(platform.test_requests) == request_count
    row = board['items'][0]
    assert row['photo_counts']['survey_problem'] == 0
    assert row['photo_counts']['construction'] == 1
    assert row['online_photos']['groups']['survey_problem']['count'] == 2
    assert row['online_photos']['groups']['survey_problem']['videos'] == 1
    assert row['online_photos']['groups']['construction']['count'] is None


def test_defects_include_two_report_photos_and_two_survey_photos_without_counting_survey_twice():
    surveyed = [file('survey-1', 'png'), file('survey-2', 'png')]
    combined = [file('report-1'), file('report-2'), *surveyed, file('survey-1', 'png')]
    value = online_photo_counts(link({'survey': {'fileInfoList': surveyed},
                                     'repair': {'preRepairFileInfo': combined}}))['groups']['survey_problem']
    assert value['state'] == 'known' and value['count'] == 4
    assert '报案及查勘' in value['note']


@pytest.mark.parametrize('files,expected', [([], 0), ([file('report-only')], 1)])
def test_verified_repair_summary_is_used_even_when_survey_is_absent_or_older(files, expected):
    for survey in ({}, {'fileInfoList': [file('old-survey')]}):
        value = online_photo_counts(link({'row': {'siteSurveyId': None, 'repairAcceptanceId': 'repair-1'},
                                         'survey': survey, 'repair': {'preRepairFileInfo': files}}))['groups']['survey_problem']
        assert value['state'] == 'known' and value['count'] == expected


@pytest.mark.parametrize('repair', [{}, {'preRepairFileInfo': None}, {'preRepairFileInfo': 'unread'}])
def test_missing_repair_summary_does_not_mislabel_survey_subset_as_complete(repair):
    snapshot = {'row': {'siteSurveyId': 'survey-1', 'repairAcceptanceId': 'repair-1'},
                'survey': {'fileInfoList': [file('survey-1'), file('survey-2')]}, 'repair': repair}
    value = online_photo_counts(link(snapshot))['groups']['survey_problem']
    assert value['state'] == 'partial' and value['count'] == 2
    assert '汇总栏尚未读取' in value['note']
    snapshot['row']['siteSurveyId'] = None
    snapshot['survey'] = {}
    value = online_photo_counts(link(snapshot))['groups']['survey_problem']
    assert value['state'] == 'unread' and value['count'] is None


def test_repair_defects_keep_videos_unknown_invalid_and_unread_states_separate():
    snapshot = {'repair': {'preRepairFileInfo': [file('image'), file('video', 'mp4'), file('pdf', 'pdf'),
                                                file('invalid', invalid=1), {'id': 'unknown'}]}}
    value = online_photo_counts(link(snapshot))['groups']['survey_problem']
    assert value['state'] == 'partial' and value['count'] == 1
    assert value['videos'] == value['other'] == value['unknown'] == 1
    snapshot['details_deferred'] = 'unsubmitted_report'
    assert online_photo_counts(link(snapshot))['groups']['survey_problem']['state'] == 'unread'
