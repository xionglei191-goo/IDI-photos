from copy import deepcopy

import pytest

from idi.domain import DomainError
from idi.platform_client import PlatformWriteError
from tests.test_platform_preparation import platform, draft_data
from tests.test_automatic_uploads import setup_flow, tick


def partial(services, platform, case, photo, monkeypatch):
    auto, add, uploads, writes, write = setup_flow(services, platform, case, photo, monkeypatch)
    for color, role in [('red', 'building_checkin'), ('blue', 'door_checkin'), ('green', 'problem')]:
        add(color, 'survey', role)
    row = tick(auto, case['id'])
    review = services['reviews'].get(row['review_id'])
    assert review['snapshot']['review_only']
    return review, auto, add, uploads, writes, write


def test_edit_partial_review_and_submit_once_with_existing_receipts(platform, services, case, photo, monkeypatch, client):
    review, auto, _, uploads, writes, _ = partial(services, platform, case, photo, monkeypatch)
    before = deepcopy(platform.draft(case['id'], 'survey'))
    sources = platform.field_sources(case['id'], 'survey')
    edits = {'defectDesc': '现场核实的原因', 'defectiveSite': '卫生间门套'}
    url = f"/api/workflow/reviews/{review['id']}"
    preview = client.post(url + '/preview', json={'fingerprint': review['fingerprint'], 'values': edits})
    assert preview.status_code == 200, preview.text
    assert preview.json()['error_count'] == 0
    assert preview.json()['values']['defectiveSite'] == '卫生间门套'
    assert platform.draft(case['id'], 'survey') == before
    body = {'fingerprint': review['fingerprint'], 'values': edits}
    assert client.post(url + '/confirm', json=body).status_code == 400
    body['acknowledged'] = True
    result = client.post(url + '/confirm', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['id'] == review['id'] and result.json()['state'] == 'verified'
    assert not result.json()['snapshot'].get('review_only')
    assert services['domain'].case(case['id'])['workflow_stage'] == 'repair'
    assert len(uploads) == 3
    assert [p['state'] for _, p in writes].count(1) == 1
    assert writes[-1][1]['defectDesc'] == edits['defectDesc']
    assert writes[-1][1]['defectiveSite'] == edits['defectiveSite']
    after = platform.field_sources(case['id'], 'survey')
    assert after['defectDesc']['origin'] == after['defectiveSite']['origin'] == 'manual'
    assert all(after[k] == v for k, v in sources.items() if k not in edits)
    assert platform.draft(case['id'], 'survey')['files'] == before['files']
    # A lost HTTP response can retry the original confirmation verbatim.
    retry = client.post(url + '/confirm', json=body)
    assert retry.status_code == 200 and retry.json()['state'] == 'verified', retry.text
    assert [p['state'] for _, p in writes].count(1) == 1


@pytest.mark.parametrize('edits', [
    {'defectDesc': '原因', 'repairMethod': '2', 'planRepairDate': ''},
    {'defectDesc': '原因', 'repairMethod': '3', 'zeroRejectReason': ''},
    {'defectDesc': '原因', 'siteSurveyDate': 'tomorrow'},
    {'defectDesc': '原因', 'divisionWorksCode': 'unknown-code'},
    {'defectDesc': '原因', 'repairUserId': 'not-the-official-person'},
    {'repairPersonPhone': 'invented-number'},
])
def test_invalid_edits_leave_draft_and_review_untouched(platform, services, case, photo, monkeypatch, edits):
    review, _, _, _, writes, _ = partial(services, platform, case, photo, monkeypatch)
    before = platform.draft(case['id'], 'survey')
    with pytest.raises(DomainError):
        services['reviews'].confirm(review['id'], review['fingerprint'], values=edits)
    assert platform.draft(case['id'], 'survey') == before
    assert services['reviews'].get(review['id'])['fingerprint'] == review['fingerprint']
    assert all(p['state'] == 0 for _, p in writes)


@pytest.mark.parametrize('change', ['photo', 'draft', 'account', 'stage'])
def test_review_edits_cannot_approve_concurrent_changes(platform, services, case, photo, monkeypatch, change):
    review, _, add, _, writes, _ = partial(services, platform, case, photo, monkeypatch)
    if change == 'photo':
        add('yellow', 'survey', 'problem')
    elif change == 'draft':
        data = draft_data(platform, case['id'], 'survey', {**platform.draft(case['id'], 'survey')['values'], 'defectiveSite': '别人修改的部位'})
        platform.save_draft(case['id'], 'survey', data)
    elif change == 'account':
        with services['db'].tx() as con:
            con.execute('UPDATE platform_links SET revision=revision+1 WHERE case_id=?', (case['id'],))
    else:
        with services['db'].tx() as con:
            con.execute("UPDATE cases SET workflow_stage='repair' WHERE id=?", (case['id'],))
    before = platform.draft(case['id'], 'survey')
    with pytest.raises(DomainError):
        services['reviews'].confirm(review['id'], review['fingerprint'], values={'defectDesc': '原因'})
    assert platform.draft(case['id'], 'survey') == before
    assert all(p['state'] == 0 for _, p in writes)


def test_new_photo_after_edit_still_blocks_submission(platform, services, case, photo, monkeypatch):
    review, _, add, _, writes, _ = partial(services, platform, case, photo, monkeypatch)
    reviews = services['reviews']
    original = reviews._confirm
    def arriving_photo(*args, **kwargs):
        add('yellow', 'survey', 'problem')
        return original(*args, **kwargs)
    monkeypatch.setattr(reviews, '_confirm', arriving_photo)
    with pytest.raises(DomainError, match='新资料|资料已修改'):
        reviews.confirm(review['id'], review['fingerprint'], values={'defectDesc': '原因'})
    assert all(p['state'] == 0 for _, p in writes)


def test_failed_write_preserves_inline_edit_and_retry_is_idempotent(platform, services, case, photo, monkeypatch):
    review, _, _, _, writes, write = partial(services, platform, case, photo, monkeypatch)
    def rejected(*args):
        raise PlatformWriteError('模拟官网暂时拒绝', rejected=True)
    monkeypatch.setattr(platform.client, 'submit', rejected)
    reviews = services['reviews']
    edits = {'defectDesc': '审核补充的原因'}
    with pytest.raises(DomainError):
        reviews.confirm(review['id'], review['fingerprint'], values=edits)
    assert platform.draft(case['id'], 'survey')['values']['defectDesc'] == edits['defectDesc']
    assert reviews.get(review['id'])['state'] == 'failed'
    monkeypatch.setattr(platform.client, 'submit', write)
    assert reviews.confirm(review['id'], review['fingerprint'], values=edits)['state'] == 'verified'
    assert [p['state'] for _, p in writes].count(1) == 1
