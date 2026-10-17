import json
from copy import deepcopy

import httpx
import pytest

from idi.domain import DomainError, PHOTO_GROUPS
from idi.platform_client import PlatformClient
from idi.recognition import extract_fields
from idi.workflow import remote_stage
from tests.test_platform_preparation import platform, draft_data, remote_file, enter_stage


def test_case_identifiers_sequence_collision_and_project_prefix(services, project, case):
    domain = services['domain']
    assert case['short_code'] == 'CHBYF-1-201-01'
    first = domain.save_case({'project_id': project['id'], 'title': '同一户另一问题', 'building': '1', 'room': '201', 'report_number': '000001', 'policy_number': '000009'})
    second = domain.save_case({'project_id': project['id'], 'title': '同一保单另一次报修', 'building': '1', 'room': '201', 'report_number': '000001', 'policy_number': '000009'})
    assert first['short_code'].endswith('-02') and second['short_code'].endswith('-03')
    assert len(domain.cases(query='000001')) == 2
    assert domain.cases(query=second['short_code'])[0]['id'] == second['id']
    with pytest.raises(DomainError, match='项目简称'):
        domain.save_case({'short_code': 'OTHER-1-201-01'}, case['id'])
    domain.save_project({**project, 'short_prefix': 'BYF'}, project['id'])
    third = domain.save_case({'project_id': project['id'], 'title': '新楼栋', 'building': '2', 'room': '301'})
    assert third['short_code'] == 'BYF-2-301-01'
    current = domain.case(case['id'])
    assert current['short_code'] == 'BYF-1-201-01'
    assert current['short_code_aliases'] == ['CHBYF-1-201-01']
    assert domain.cases(query=case['short_code'])[0]['id'] == case['id']


@pytest.mark.parametrize('content,group', [('查勘打卡', 'survey_checkin'), ('墙面起皮问题', 'survey_problem'), ('施工打卡', 'work_checkin'), ('墙面第二遍灰施工', 'construction'), ('墙面完工', 'completion')])
def test_four_watermark_fields_five_groups_and_exact_case(services, case, content, group):
    text = f"编号：{case['short_code']}\n施工项目：从化铂悦府\n施工区域：1栋-201号-客厅\n施工内容：{content}\n2026-10-15 10:35"
    fields = extract_fields(text)
    assert fields['short_code'] == case['short_code']
    assert fields['photo_group'] == group
    assert fields['building'] == '1' and fields['room'] == '201'
    assert fields['watermark_time'] == '2026-10-15 10:35'
    assert services['recognizer'].match(text, fields)[1] == case['id']
    assert services['recognizer'].match(text, {**fields, 'room': '301'})[1] is None
    assert services['recognizer'].match(text, {**fields, 'short_code': 'CHBYF-1-201-99'})[1] is None


def test_board_counts_only_nonconflicting_images_and_insurer_rules(services, case, photo):
    domain = services['domain']
    pictures = []
    for color, group in zip(['red', 'green', 'blue', 'pink', 'yellow'], PHOTO_GROUPS):
        item = photo(color, group + '.png')
        pictures.append(domain.update_photo(item['id'], {'photo_group': group}))
    services['importer'].add_file(b'%PDF-1.4\nfixture\n%%EOF', '业主验收.pdf', case_id=case['id'])
    row = services['workflow'].board()['items'][0]
    assert row['photo_counts'] == dict.fromkeys(PHOTO_GROUPS, 1)
    assert domain.completeness(case['id'], 'insurer-v2')['missing_count'] == 0
    with services['db'].tx() as con:
        con.execute("UPDATE photos SET recognition_state='conflict' WHERE id=?", (pictures[0]['id'],))
    row = services['workflow'].board()['items'][0]
    assert row['photo_counts']['survey_checkin'] == 0 and row['exception_count'] == 1
    assert domain.summary()['exceptions'] == 1
    assert domain.completeness(case['id'], 'insurer-v2')['missing_count'] == 1
    domain.update_photo(pictures[1]['id'], {'photo_group': 'unknown'})
    assert domain.photo(pictures[1]['id'])['stage'] == 'unknown'


def test_site_survey_acceptance_enters_work_without_insurer_gate(services, case):
    workflow = services['workflow']
    def record(action, current):
        return workflow.record_result(case['id'], {'action': action, 'revision': current['revision'], 'event_at': '2026-10-15 11:20', 'note': '测试外部办理凭据'})
    with pytest.raises(DomainError, match='不能跳过'):
        record('survey_completed', case)
    surveying = record('accepted', case)
    assert surveying['workflow_stage'] == 'survey'
    assert workflow.board('work')['total'] == 1 and workflow.board('intake')['total'] == 0
    assert len(workflow.detail(case['id'])['events']) == 1


@pytest.mark.parametrize('row,expected', [({'flowStatus': '1'}, 'pending_intake'), ({'flowStatus': '3', 'siteSurveyState': '1'}, 'insurer_review'), ({'flowStatus': '2', 'siteSurveyState': '0'}, 'survey'), ({'flowStatus': '2', 'siteSurveyState': '1', 'repairAcceptanceFlowStatus': '0'}, 'repair'), ({'flowStatus': '2', 'siteSurveyState': '1', 'repairAcceptanceFlowStatus': '1'}, 'acceptance'), ({'flowStatus': '2', 'siteSurveyState': '1', 'repairAcceptanceFlowStatus': '2'}, 'settlement'), ({'flowStatus': '9'}, 'closed'), ({'passStatus': '1', 'acceptanceResult': '2'}, 'direct_payment'), ({'passStatus': '1', 'acceptanceResult': '3'}, 'noncoverage'), ({'flowStatus': 'unexpected'}, None)])
def test_observed_official_states(row, expected):
    assert remote_stage({'scope': 'intake', 'row': row}) == expected


@pytest.mark.parametrize('flow,receiving', [(None, '0'), ('', 0)])
def test_saved_legacy_report_is_visible_but_cannot_start_intake(platform, services, case, flow, receiving):
    platform.test_row.update(flowStatus=flow, reportReceivingState=receiving, endStatus='0')
    before_requests = len(platform.test_requests)
    snapshot = platform.client.snapshot_from_row(platform.test_row, 'intake')
    assert len(platform.test_requests) == before_requests  # No invented or unnecessary stage detail reads.
    assert snapshot['details_deferred'] == 'unsubmitted_report'
    assert snapshot['claim'] == {} and snapshot['survey'] == {} and snapshot['repair'] == {}
    assert remote_stage(snapshot) == 'pending_intake'
    services['workflow'].apply_snapshot(snapshot, case['id'])
    data = draft_data(platform, case['id'], 'intake', {'acceptanceResult': '2', 'reasonDescription': '已核对原因'})
    plan = platform.save_draft(case['id'], 'intake', data)
    assert not plan['prepared_for_review']
    assert any(i['field'] == 'node' and '保存状态' in i['message'] for i in plan['issues'])
    board = services['workflow'].board('intake')
    assert board['items'][0]['next_action'] == '官网报案待提交'
    assert board['items'][0]['platform_status'] == '报案待提交'
    with pytest.raises(DomainError):
        services['reviews'].prepare(case['id'], 'intake', plan['draft']['revision'])
    assert services['workflow'].board('work')['total'] == 0


@pytest.mark.parametrize('failure', ['', 'repeated', 'changing', 'missing_count', 'early_empty'])
def test_full_pagination_requires_exact_complete_coverage(failure):
    seen = []
    def transport(request):
        body = json.loads(request.content)
        seen.append(body)
        page = body['page']
        start = 0 if page == 1 or failure == 'repeated' else 40
        items = [{'reportReceivingId': str(2100000000000000000 + i), 'endStatus': '1'} for i in range(start, 40 if page == 1 else start + 3)]
        result = {'code': 0, 'data': [] if failure == 'early_empty' and page == 2 else items, 'count': 44 if failure == 'changing' and page == 2 else 43}
        if failure == 'missing_count':
            result.pop('count')
        return httpx.Response(200, json=result)
    client = PlatformClient(httpx.MockTransport(transport))
    client._token = 'fixture-only'
    try:
        if failure:
            with pytest.raises(DomainError):
                client.all_rows('intake', '1')
        else:
            assert len(client.all_rows('intake', '1')) == 43
            assert [s['page'] for s in seen] == [1, 2]
        assert all(s['reportSubmitState'] == 1 and s['endStatus'] == '1' for s in seen)
    finally:
        client.close()


def test_full_sync_preserves_local_edits_and_reports_partial(platform, services, case, monkeypatch):
    workflow, domain = services['workflow'], services['domain']
    domain.save_case({'title': '管理人员补充说明', 'responsible': '张工'}, case['id'])
    enter_stage(platform, case['id'], 'intake')
    platform.save_draft(case['id'], 'intake', draft_data(platform, case['id'], 'intake', {'acceptanceResult': '2', 'reasonDescription': '本地草稿'}))
    platform.test_row.update(flowStatus='9', siteSurveyState='1', repairAcceptanceFlowStatus='0')
    result = workflow.sync({}, lambda *_: None)
    assert result['state'] == 'completed' and len(result['scopes']) == 2
    assert result['list_counts'] == {'intake': 1, 'repair': 1}
    assert domain.case(case['id'])['title'] == '管理人员补充说明'
    assert platform.draft(case['id'], 'intake')['values']['reasonDescription'] == '本地草稿'
    last_complete = workflow.sync_status()['last_complete']['id']
    platform.test_row['updateTime'] = '2026-10-16 09:00'
    def fail(*args):
        raise DomainError('模拟一个详情回读失败', 502)
    monkeypatch.setattr(platform.client, 'snapshot_from_row', fail)
    assert workflow.sync({}, lambda *_: None)['state'] == 'partial'
    assert workflow.sync_status()['last_complete']['id'] == last_complete
    assert domain.case(case['id'])['responsible'] == '张工'
    with pytest.raises(DomainError, match='官网回读'):
        workflow.record_result(case['id'], {})


def intake_review(platform, services, case):
    platform.test_row['flowStatus'] = '1'
    services['workflow'].refresh_case(case['id'])
    data = draft_data(platform, case['id'], 'intake', {'acceptanceResult': '2', 'reasonDescription': '已核对的实际原因'})
    plan = platform.save_draft(case['id'], 'intake', data)
    return services['reviews'].prepare(case['id'], 'intake', plan['draft']['revision'])


def test_readonly_review_is_local_and_edits_invalidate(platform, services, case):
    batch = intake_review(platform, services, case)
    reviews = services['reviews']
    with pytest.raises(DomainError, match='只读'):
        reviews.confirm(batch['id'], batch['fingerprint'])
    assert reviews.confirm(batch['id'], batch['fingerprint'], True)['state'] == 'reviewed'
    assert all('upload' not in path and 'addOrEdit' not in path for path, _ in platform.test_requests)
    data = draft_data(platform, case['id'], 'intake', {'acceptanceResult': '2', 'reasonDescription': '审核后更改'})
    platform.save_draft(case['id'], 'intake', data)
    with pytest.raises(DomainError, match='重新审核'):
        reviews.confirm(batch['id'], batch['fingerprint'], True)
    assert reviews.get(batch['id'])['state'] == 'stale'


def test_mock_submission_readback_and_idempotency(platform, services, case, monkeypatch):
    batch = intake_review(platform, services, case)
    platform.client.write_enabled = True
    submitted = []
    def submit(stage, payload):
        submitted.append(deepcopy(payload))
        platform.test_claim.update(payload)
        platform.test_row['flowStatus'] = '3'
        return {'code': 0}
    monkeypatch.setattr(platform.client, 'submit', submit)
    reviews = services['reviews']
    assert reviews.confirm(batch['id'], batch['fingerprint'])['state'] == 'verified'
    assert reviews.confirm(batch['id'], batch['fingerprint'])['state'] == 'verified'
    assert len(submitted) == 1
    assert services['domain'].case(case['id'])['workflow_stage'] == 'insurer_review'


def test_unknown_response_blocks_another_batch(platform, services, case, monkeypatch):
    batch = intake_review(platform, services, case)
    second = services['reviews'].prepare(case['id'], 'intake', batch['snapshot']['draft']['revision'])
    platform.client.write_enabled = True
    sent = []
    def submit(*args):
        sent.append(1)
        raise DomainError('结果未知', 502)
    monkeypatch.setattr(platform.client, 'submit', submit)
    with pytest.raises(DomainError, match='结果未知'):
        services['reviews'].confirm(batch['id'], batch['fingerprint'])
    assert services['reviews'].get(batch['id'])['state'] == 'uncertain'
    assert services['reviews'].reconcile(batch['id'])['state'] == 'uncertain'
    with pytest.raises(DomainError, match='待回读核实'):
        services['reviews'].confirm(second['id'], second['fingerprint'])
    assert len(sent) == 1


def test_mock_upload_preserves_receipts_and_readback(platform, services, case, photo, monkeypatch):
    platform.test_row.update(flowStatus='2', siteSurveyState='0')
    services['workflow'].refresh_case(case['id'])
    picture = photo('purple', '实际问题.png', stage='survey', role='problem')
    plan = platform.save_draft(case['id'], 'survey', draft_data(platform, case['id'], 'survey', files={'fileInfoList': [picture['id']]}))
    batch = services['reviews'].prepare(case['id'], 'survey', plan['draft']['revision'])
    platform.client.write_enabled = True
    uploads = []
    def upload(path, name, mime):
        uploads.append(path.read_bytes())
        return remote_file('uploaded-1', name)
    def submit(stage, payload):
        platform.test_survey.update(deepcopy(payload))
        platform.test_row['siteSurveyState'] = '1'
        return {'code': 0}
    monkeypatch.setattr(platform.client, 'upload', upload)
    monkeypatch.setattr(platform.client, 'submit', submit)
    result = services['reviews'].confirm(batch['id'], batch['fingerprint'])
    assert result['state'] == 'verified'
    assert len(uploads) == 1
    assert result['result']['uploaded'][picture['id']]['id'] == 'uploaded-1'
    assert services['storage'].verify(services['db'].one('SELECT * FROM blobs WHERE hash=?', (picture['blob_hash'],)))


def test_ledger_preserves_distinct_text_identifiers(services, project):
    importer = services['importer']
    preview = importer.ledger_preview('案件内容,报修编号,保单号,报案号,栋号,房号\n实际问题,ZLBH-01,000123,0009876543210987654321,1,201\n'.encode(), '台账.csv', project['id'])
    importer.ledger_commit({'batch_id': preview['batch_id']}, lambda *_: None)
    item = services['domain'].cases(query='ZLBH-01')[0]
    assert item['policy_number'] == '000123' and item['report_number'] == '0009876543210987654321'


def test_watermark_overrides_path_hint_without_overwriting_manual_confirmation(services, case, photo, monkeypatch):
    item = photo('lime', '施工.png', stage='work', role='construction')
    text = f"编号：{case['short_code']}\n施工项目：从化铂悦府\n施工区域：1栋201号\n施工内容：查勘打卡"
    monkeypatch.setattr(services['recognizer'], 'local_ocr', lambda _: (text, []))
    services['recognizer'].recognize(item['id'])
    result = services['domain'].photo(item['id'])
    assert result['stage'] == 'survey' and result['photo_group'] == 'survey_checkin'
    services['domain'].update_photo(item['id'], {'photo_group': 'construction', 'confirm': True})
    services['recognizer'].recognize(item['id'])
    result = services['domain'].photo(item['id'])
    assert result['stage'] == 'work' and result['photo_group'] == 'construction'
    assert result['recognition_state'] == 'confirmed'


def test_network_sync_does_not_block_local_tasks(services):
    import threading
    entered, release, local_done = threading.Event(), threading.Event(), threading.Event()
    jobs = services['jobs']
    def sync(*_):
        entered.set()
        release.wait(5)
        return {}
    jobs.register('platform_sync', sync)
    jobs.register('local_probe', lambda *_: local_done.set() or {})
    jobs.enqueue('platform_sync', {})
    jobs.start()
    try:
        assert entered.wait(2)
        jobs.enqueue('local_probe', {})
        assert local_done.wait(2)
    finally:
        release.set()
        jobs.stop()


def test_later_folder_copy_keeps_existing_assignment_and_adds_provenance(services, case, project):
    from tests.conftest import image_bytes
    importer = services['importer']
    first = importer.add_file(image_bytes('orange'), '原件.png', case_id=case['id'], source_path='first/原件.png')
    repeated = importer.add_file(image_bytes('orange'), '再次收到.png', source_type='folder', source_path='inbox/再次收到.png')
    assert repeated['duplicate'] and repeated['photo_id'] == first['photo_id']
    assert services['domain'].photo(first['photo_id'])['source_count'] == 2
    assert services['domain'].summary()['total'] == 1
    other = services['domain'].save_project({'name': '明确指定的另一项目'})
    separate = importer.add_file(image_bytes('orange'), '原件.png', project_id=other['id'])
    assert separate['photo_id'] != first['photo_id']
