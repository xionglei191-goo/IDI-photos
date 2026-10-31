from copy import deepcopy

import pytest

from idi.workflow import address_fields
from tests.test_platform_preparation import platform
from tests.test_project_access import make_person, login_person


ADDRESS = '金茂万科都会四季A地块小区A8栋/未划分/27F/2702'


@pytest.mark.parametrize('address,expected', [
    (ADDRESS, {'building': 'A8', 'unit': '', 'floor': '27', 'room': '2702'}),
    ('小区8幢／2单元／27层／2702室', {'building': '8', 'unit': '2', 'floor': '27', 'room': '2702'}),
    ('小区A8号楼 / 未划分 / 27f / 2702', {'building': 'A8', 'unit': '', 'floor': '27', 'room': '2702'}),
    ('8栋/1单元/-1F/0102号', {'building': '8', 'unit': '1', 'floor': '-1', 'room': '0102'}),
    ('1栋201', {'building': '1', 'unit': '', 'room': '201'}),
    ('小区A8-2-2702', {'building': 'A8', 'unit': '2', 'room': '2702'}),
])
def test_explicit_room_addresses_keep_original_text(address, expected):
    assert address_fields(address) == {**expected, 'location_detail': address}


@pytest.mark.parametrize('address', [
    'A8栋/未划分/27F/电梯间', 'A8栋/未划分/1F/门厅',
    '地下室/未划分/-1F/配电间', 'A8栋/未划分/27F',
    'A8栋/未划分/27F/2702/2703', 'A8栋/未划分/27F/101-102',
    'A8栋/未知/27F/2702', 'A8栋/未划分/楼层待核对/2702',
])
def test_public_or_ambiguous_addresses_do_not_invent_a_room(address):
    assert address_fields(address) == {'location_detail': address}


def snapshot_for(platform, *, new=False):
    row = {**platform.test_row, 'communityDesc': ADDRESS, 'flowStatus': '2'}
    if new:
        row.update(reportReceivingId='2112820991343763456', reportCode='ZLBH202610211619130264')
    return {'row': row, 'claim': deepcopy(row), 'scope': 'repair'}


def test_new_official_case_reads_all_explicit_location_fields(platform, services):
    result = services['workflow'].apply_snapshot(snapshot_for(platform, new=True))
    case = services['domain'].case(result['case_id'])
    assert result['created']
    assert {k: case[k] for k in ('building', 'unit', 'floor', 'room', 'location_detail')} == address_fields(ADDRESS)
    assert '-A8-2702-' in case['short_code']


def test_refresh_fills_legacy_room_without_changing_existing_case_code(platform, services, case):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET building='',unit='',floor='',room='',location_detail=? WHERE id=?", (ADDRESS, case['id']))
    before = services['domain'].case(case['id'])
    services['workflow'].apply_snapshot(snapshot_for(platform), case['id'])
    after = services['domain'].case(case['id'])
    assert {k: after[k] for k in ('building', 'unit', 'floor', 'room', 'location_detail')} == address_fields(ADDRESS)
    assert after['short_code'] == before['short_code']
    assert after['revision'] == before['revision'] + 1


@pytest.mark.parametrize('manual', [{'building': 'B8'}, {'unit': '2'}, {'floor': '28'}, {'room': '2802'}, {'location_detail': '人工核实：另一处位置'}])
def test_refresh_does_not_mix_official_address_with_local_corrections(platform, services, case, manual):
    with services['db'].tx() as con:
        con.execute("UPDATE cases SET building='',unit='',floor='',room='',location_detail=? WHERE id=?", (ADDRESS, case['id']))
    before = services['domain'].save_case(manual, case['id'])
    services['workflow'].apply_snapshot(snapshot_for(platform), case['id'])
    after = services['domain'].case(case['id'])
    for key in ('building', 'unit', 'floor', 'room', 'location_detail', 'short_code'):
        assert after[key] == before[key]


def test_raw_address_search_covers_cases_boards_and_preserves_worker_scope(app, client, services, project):
    domain = services['domain']
    visible = domain.save_case({'project_id': project['id'], 'title': '房号未拆分案件', 'location_detail': ADDRESS})
    hidden = domain.save_case({'project_id': project['id'], 'title': '同项目未分配案件', 'location_detail': ADDRESS})
    other_project = domain.save_project({'name': '另一个项目'})
    other = domain.save_case({'project_id': other_project['id'], 'title': '跨项目案件', 'location_detail': ADDRESS})
    assert visible['room'] == ''
    assert {c['id'] for c in client.get('/api/cases', params={'query': '2702'}).json()} == {visible['id'], hidden['id'], other['id']}
    board = client.get('/api/workflow/board', params={'source': 'local', 'query': '2702', 'project_id': project['id']}).json()
    assert board['total'] == 2 and {c['id'] for c in board['items']} == {visible['id'], hidden['id']}
    member = make_person(services, project['id'], 'member', 'room-member')
    assert member['id']
    with login_person(app, 'room-member') as web:
        assert {c['id'] for c in web.get('/api/cases?query=2702').json()} == {visible['id'], hidden['id']}
    worker = make_person(services, project['id'], 'worker', 'room-worker')
    assert client.post('/api/access/case-assignments', json={'case_ids': [visible['id']], 'user_ids': [worker['id']],
        'mode': 'replace', 'expected_revisions': {visible['id']: 0}}).status_code == 200
    with login_person(app, 'room-worker') as web:
        assert [c['id'] for c in web.get('/api/cases?query=2702').json()] == [visible['id']]
        assert web.get('/api/cases/' + hidden['id']).status_code == 403
