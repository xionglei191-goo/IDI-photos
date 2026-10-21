from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from idi.domain import DomainError
from idi.platform_client import PlatformClient, attachment_url
from tests.conftest import image_bytes
from tests.test_platform_preparation import platform, enter_stage, remote_file
from tests.test_platform_writes import survey_review, mock_writes


def photo_plan(platform, case):
    platform.test_survey['buildingFileInfoList'] = [remote_file('official-picture', '楼栋.png')]
    enter_stage(platform, case['id'], 'survey')
    return platform.preview(case['id'], 'survey')


def media_path(case, variant='thumbnail'):
    return f"/api/platform/cases/{case['id']}/stages/survey/files/buildingFileInfoList/official-picture/{variant}"


def test_missing_official_original_is_archived_once_without_changing_proposal(platform, services, case, client, monkeypatch):
    plan = photo_plan(platform, case)
    batch = services['reviews'].prepare(case['id'], 'survey', plan['draft']['revision'])
    before = services['reviews'].snapshot(case['id'], 'survey')[1]
    calls, content = [], image_bytes('orange')
    def download(url, limit, account):
        calls.append((url, account))
        return content
    monkeypatch.setattr(platform.client, 'download_attachment', download)
    response = client.get(media_path(case))
    assert response.status_code == 200 and response.headers['content-type'] == 'image/jpeg'
    cached = services['db'].one('SELECT * FROM platform_media WHERE case_id=?', (case['id'],))
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (cached['blob_hash'],))
    assert services['storage'].resolve(blob['stored_path']).read_bytes() == content
    assert services['storage'].verify(blob)
    # The review reads the same local original, and later access works offline.
    platform.client.logout()
    review_path = f"/api/workflow/reviews/{batch['id']}/files/buildingFileInfoList/official-picture/original"
    assert client.get(review_path).content == content
    assert client.get(media_path(case)).status_code == 200
    assert len(calls) == 1
    assert services['reviews'].snapshot(case['id'], 'survey')[1] == before
    assert not services['db'].all('SELECT id FROM photos WHERE case_id=?', (case['id'],))
    assert services['reviews'].get(batch['id'])['state'] == 'prepared'


def test_verified_upload_receipt_reuses_local_original_and_restores_thumbnail(platform, services, case, photo, monkeypatch, client):
    batch, pics = survey_review(platform, services, case, photo)
    mock_writes(platform, monkeypatch)
    saved = services['reviews'].confirm(batch['id'], batch['fingerprint'], action='save')
    assert saved['state'] == 'save_verified'
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (pics[0]['blob_hash'],))
    services['storage'].resolve(blob['thumbnail_path']).unlink()
    monkeypatch.setattr(platform.client, 'download_attachment', lambda *_: pytest.fail('Existing original must be reused'))
    platform.client.logout()
    path = f"/api/platform/cases/{case['id']}/stages/survey/files/buildingFileInfoList/uploaded-1"
    assert client.get(path + '/thumbnail').status_code == 200
    assert services['storage'].resolve(blob['thumbnail_path']).is_file()
    assert client.get(path + '/original').content == services['storage'].resolve(blob['stored_path']).read_bytes()


def test_same_filename_does_not_link_an_unrelated_local_photo(platform, services, case, photo, monkeypatch):
    local = photo('blue', '楼栋.png')
    plan = photo_plan(platform, case)
    monkeypatch.setattr(platform.client, 'download_attachment', lambda *_: image_bytes('red'))
    downloaded, _ = platform.media.ensure(plan, 'buildingFileInfoList', 'official-picture')
    assert downloaded['hash'] != local['blob_hash']
    assert services['storage'].verify(services['db'].one('SELECT * FROM blobs WHERE hash=?', (local['blob_hash'],)))


def test_parallel_thumbnail_requests_download_one_original(platform, case, monkeypatch):
    plan = photo_plan(platform, case)
    calls = []
    def download(*_):
        calls.append(1)
        return image_bytes()
    monkeypatch.setattr(platform.client, 'download_attachment', download)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: platform.media.ensure(plan, 'buildingFileInfoList', 'official-picture'), range(4)))
    assert len(calls) == 1
    assert len({blob['hash'] for blob, _ in results}) == 1


def test_attachment_must_belong_to_requested_field_and_snapshot(platform, services, case, client, monkeypatch):
    plan = photo_plan(platform, case)
    batch = services['reviews'].prepare(case['id'], 'survey', plan['draft']['revision'])
    monkeypatch.setattr(platform.client, 'download_attachment', lambda *_: pytest.fail('No download for unknown files'))
    assert client.get(media_path(case).replace('official-picture', 'another-case-file')).status_code == 404
    assert client.get(media_path(case).replace('buildingFileInfoList', 'fileInfoList')).status_code == 404
    path = f"/api/workflow/reviews/{batch['id']}/files/buildingFileInfoList/new-file/thumbnail"
    assert client.get(path).status_code == 404
    assert not services['db'].all('SELECT * FROM platform_media')


def test_failed_download_or_invalid_image_does_not_create_local_receipt(platform, services, case, client, monkeypatch):
    photo_plan(platform, case)
    monkeypatch.setattr(platform.client, 'download_attachment', lambda *_: b'not an image')
    assert client.get(media_path(case)).status_code == 422
    assert not services['db'].all('SELECT * FROM platform_media')
    def fail(*_):
        raise DomainError('官网连接暂不可用', 502)
    monkeypatch.setattr(platform.client, 'download_attachment', fail)
    assert client.get(media_path(case)).status_code == 502
    assert not services['db'].all('SELECT * FROM platform_media')


@pytest.mark.parametrize('url', [
    'https://other.example/profile/file.jpg', '//other.example/profile/file.jpg',
    'http://gzzb2021.com/iidi/profile/file.jpg', '/insurance/login', '/profile/../login',
    '/profile/%252e%252e/login', '/profile/..\\login', 'file:///profile/test.jpg',
    'https://gzzb2021.com@other.example/profile/file.jpg',
])
def test_official_session_is_never_sent_to_unapproved_urls(url):
    with pytest.raises(DomainError):
        attachment_url(url)


def test_file_reader_is_authenticated_bounded_and_does_not_follow_redirects():
    requests = []
    body = image_bytes()
    def transport(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url == 'https://gzzb2021.com/iidi/profile/real.png'
        assert request.headers['authorization'] == 'Bearer test-only'
        return httpx.Response(200, content=body, headers={'content-type': 'image/png'})
    with_client = PlatformClient(httpx.MockTransport(transport))
    with_client._token, with_client._account = 'test-only', 'test-account'
    assert with_client.download_attachment('/profile/real.png', len(body), 'test-account') == body
    with pytest.raises(DomainError, match='大小限制'):
        with_client.download_attachment('/profile/real.png', 2, 'test-account')
    with pytest.raises(DomainError, match='账号已变化'):
        with_client.download_attachment('/profile/real.png', len(body), 'another-account')
    assert len(requests) == 2
    with_client.close()
    redirected = PlatformClient(httpx.MockTransport(lambda _: httpx.Response(302, headers={'Location': 'https://other.example/'})))
    redirected._token = 'test-only'
    with pytest.raises(DomainError, match='跳转'):
        redirected.download_attachment('/profile/real.png', 10000)
    redirected.close()
