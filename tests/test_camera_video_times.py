"""New video uploads extract visible times without changing original media."""
import hashlib
import json
import threading

import pytest

from idi.domain import DomainError
from test_official_camera import official, upload_video, video_container


VISIBLE_TIME = '2026-10-26 10:52:14'


def recorded_time(**changes):
    return {'value': '2026-10-26T10:52:14+08:00', 'source': 'sdk_watermark_render',
            'event': 'recording_start', 'precision': 'second',
            'observed_at': '2026-10-26T02:52:14.125Z',
            'sdk_version': '1.3.70.9b61b4a6', **changes}


def recognized_result():
    return {'state': 'recognized', 'watermark_time': VISIBLE_TIME,
            'time_basis': 'video_watermark_ocr', 'method': 'first_frame',
            'confidence': .99, 'evidence': {'raw_text': '时间 ' + VISIBLE_TIME}, 'reason': ''}


@pytest.fixture
def extraction(services, monkeypatch):
    calls = []

    def extract(path):
        calls.append(path)
        return recognized_result()

    monkeypatch.setattr(services['camera_video_times'], 'extract', extract)
    return calls


def video_task(services):
    return services['db'].one('SELECT * FROM camera_video_times')


def source_metadata(services, photo_id):
    source = services['db'].one("SELECT metadata FROM photo_sources WHERE photo_id=? AND source_type='xhey_camera'",
                                (photo_id,))
    return json.loads(source['metadata'])


@pytest.mark.parametrize('value', ['2026-10-26T10:52:14+08:00', '2026-10-26T02:52:14Z'])
def test_recorded_video_time_archives_immediately_without_ocr_or_photo_verification(
        client, official, services, extraction, value):
    original = video_container()
    # Even with OCR disabled and the phone's launch/observation clocks wrong, only
    # the SDK renderer's start value is used. Observation is provenance, not time evidence.
    services['db'].set_setting('recognition', {'local_ocr': False})
    envelope = recorded_time(value=value, observed_at='2099-01-01T00:00:00+08:00')
    response = upload_video(client, official, original, video_capture_time=envelope,
                            captured_at='2099-01-01T01:00:00+08:00', sdk_photo_time='2099-02-02 02:02:02')
    assert response.status_code == 200, response.text
    receipt = response.json()
    photo = services['domain'].photo(receipt['photo_id'])
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert photo['watermark_time'] == capture['photo_time'] == VISIBLE_TIME
    assert capture['time_state'] == 'recorded' and capture['time_basis'] == 'app_sdk_watermark_render'
    assert capture['verification_state'] == 'not_requested' and capture['verified_at'] is None
    source = source_metadata(services, photo['id'])
    assert source['video_capture_time'] == envelope
    assert source['time_basis'] == 'app_sdk_watermark_render'
    assert 'video_time_extraction' not in source
    assert not video_task(services) and not services['db'].all("SELECT * FROM jobs WHERE kind='camera_video_time'")
    assert not services['jobs'].run_one() and not extraction and not official['calls']
    assert client.get('/api/photos/' + photo['id'] + '/original').content == original
    assert receipt['original_hash'] == hashlib.sha256(original).hexdigest()

    retry = upload_video(client, official, original, video_capture_time=envelope,
                         captured_at='2099-01-01T01:00:00+08:00', sdk_photo_time='2099-02-02 02:02:02')
    assert retry.status_code == 200 and retry.json()['duplicate'] is True
    assert retry.json()['photo_id'] == photo['id']
    assert services['domain'].photo(photo['id']) == photo
    assert len(services['db'].all('SELECT * FROM photos')) == 1
    changed = upload_video(client, official, original,
                           video_capture_time={**envelope, 'value': '2026-10-26T10:52:15+08:00'},
                           captured_at='2099-01-01T01:00:00+08:00', sdk_photo_time='2099-02-02 02:02:02')
    assert changed.status_code == 409


@pytest.mark.parametrize('template_id', [10, 20])
def test_recorded_video_time_preserves_android_renderer_evidence_without_ocr(
        client, official, services, extraction, template_id):
    raw_text = '时   间：2026-10-26 10:52:14' if template_id == 10 else '2026.10.26 星期一 10:52:14'
    envelope = recorded_time(template_id=template_id, raw_text=raw_text)
    response = upload_video(client, official, video_capture_time=envelope)
    assert response.status_code == 200, response.text
    photo_id = response.json()['photo_id']
    assert services['domain'].photo(photo_id)['watermark_time'] == VISIBLE_TIME
    source = source_metadata(services, photo_id)
    assert source['video_capture_time'] == envelope
    assert source['time_basis'] == 'app_sdk_watermark_render'
    assert not video_task(services) and not services['jobs'].run_one() and not extraction


@pytest.mark.parametrize('evidence', [{'raw_text': ''}, {'raw_text': 'x' * 121}, {'raw_text': []},
                                    {'template_id': '20'}, {'template_id': True}, {'template_id': 21}])
def test_recorded_time_rejects_invalid_optional_renderer_evidence(evidence):
    from idi.camera_video_times import app_video_time
    assert app_video_time({'source': 'xhey_sdk', 'media_type': 'video',
                           'video_capture_time': recorded_time(**evidence)}) is None


@pytest.mark.parametrize('envelope', [
    {}, recorded_time(value=''), recorded_time(value='2026-10-26 10:52:14'),
    recorded_time(value='2026-10-26T10:52:14.125+08:00'),
    recorded_time(value='2026-02-30T10:52:14+08:00'),
    recorded_time(value='2099-10-26T10:52:14+08:00'),
    recorded_time(value='1999-10-26T10:52:14+08:00'),
    recorded_time(value='2026-10-26T10:52:14+23:00'),
    recorded_time(value=1792983134), recorded_time(source='sdk_callback'),
    recorded_time(event='recording_saved'), recorded_time(precision='minute'),
    recorded_time(observed_at='2026-10-26T02:52:14'),
    recorded_time(observed_at='not-a-time'), recorded_time(sdk_version=''),
    recorded_time(extra='unrecognized contract'),
])
def test_invalid_recorded_time_uses_ocr_without_polluting_the_archived_time(
        client, official, services, extraction, envelope):
    response = upload_video(client, official, video_capture_time=envelope,
                            captured_at='2099-01-01T01:00:00+08:00')
    assert response.status_code == 200, response.text
    photo_id = response.json()['photo_id']
    assert services['domain'].photo(photo_id)['watermark_time'] is None
    assert source_metadata(services, photo_id)['time_basis'] == 'unavailable'
    assert video_task(services)['state'] == 'queued'
    assert services['jobs'].run_one() and len(extraction) == 1
    assert services['domain'].photo(photo_id)['watermark_time'] == VISIBLE_TIME
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert capture['time_state'] == 'recognized' and capture['time_basis'] == 'video_watermark_ocr'


def test_optional_video_time_keeps_legacy_request_hash_and_does_not_affect_photos(
        client, official, services):
    from idi.db import dump
    from idi.mobile_camera import digest
    from test_official_camera import upload

    original = video_container()
    response = upload_video(client, official, original)
    assert response.status_code == 200
    row = services['db'].one('SELECT * FROM camera_official_captures')
    meta = json.loads(row['metadata'])
    assert 'video_capture_time' not in meta
    assert row['request_hash'] == digest(dump(meta) + hashlib.sha256(original).hexdigest())
    assert upload_video(client, official, original, video_capture_time=None).json()['duplicate'] is True

    official['service'].settings.save({'auto_verify': False})
    photo = upload(client, official, capture_id='23456789-1234-1234-1234-123456789abc',
                   video_capture_time=recorded_time(), sdk_photo_time='2026-10-26 11:12:13')
    assert photo.status_code == 200, photo.text
    assert services['domain'].photo(photo.json()['photo_id'])['watermark_time'] == '2026-10-26 11:12:13'
    assert source_metadata(services, photo.json()['photo_id'])['time_basis'] == 'sdk_returned_unverified'


def test_new_video_queues_then_displays_visible_seconds_without_altering_original(
        client, official, services, extraction):
    original = video_container()
    response = upload_video(client, official, original, captured_at='2099-01-01T01:00:00+08:00',
                            sdk_photo_time='2099-02-02 02:02:02')
    assert response.status_code == 200, response.text
    receipt = response.json()
    task = video_task(services)
    job = services['jobs'].get(task['job_id'])
    assert receipt['state'] == 'uploaded' and task['state'] == 'queued'
    assert job['state'] == 'queued' and job['kind'] == 'camera_video_time'
    assert job['payload']['photo_ids'] == [receipt['photo_id']]
    assert task['blob_hash'] == job['payload']['blob_hash'] == receipt['original_hash']
    assert not extraction and not official['calls']
    before = services['domain'].photo(receipt['photo_id'])
    assert before['watermark_time'] is None
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert capture['time_state'] == 'queued' and capture['photo_time'] is None

    assert services['jobs'].run_one()
    assert len(extraction) == 1 and not services['jobs'].run_one()
    task = video_task(services)
    result = json.loads(task['result'])
    assert task['state'] == 'recognized' and result['applied_to_photo'] is True
    assert services['jobs'].get(task['job_id'])['state'] == 'completed'
    photo = services['domain'].photo(receipt['photo_id'])
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert capture['photo_time'] == photo['watermark_time'] == VISIBLE_TIME
    assert capture['time_state'] == 'recognized' and capture['time_basis'] == 'video_watermark_ocr'
    assert photo['revision'] == before['revision'] + 1
    source = source_metadata(services, photo['id'])
    assert source['time_basis'] == 'video_watermark_ocr'
    assert source['video_time_extraction']['watermark_time'] == VISIBLE_TIME
    assert source['video_time_extraction']['original_hash'] == receipt['original_hash']
    assert source['verification_state'] == 'not_requested' and not official['calls']
    stored = services['storage'].resolve(services['db'].one(
        'SELECT stored_path FROM blobs WHERE hash=?', (receipt['original_hash'],))['stored_path'])
    assert stored.read_bytes() == original
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == receipt['original_hash']
    assert client.get('/api/photos/' + photo['id'] + '/original').content == original


def test_queue_failure_rolls_back_new_capture_photo_and_job(client, official, services, monkeypatch):
    service = services['camera_video_times']
    enqueue = service.enqueue

    def fail_after_enqueue(con, row, meta):
        enqueue(con, row, meta)
        raise DomainError('模拟排队失败')

    monkeypatch.setattr(service, 'enqueue', fail_after_enqueue)
    response = upload_video(client, official)
    assert response.status_code == 400, response.text
    for table in ('camera_official_captures', 'photos', 'photo_sources', 'camera_video_times', 'jobs'):
        assert not services['db'].all('SELECT * FROM ' + table)


def test_replay_never_creates_a_second_time_job(client, official, services, extraction):
    first = upload_video(client, official).json()
    assert upload_video(client, official).json()['duplicate'] is True
    job_id = video_task(services)['job_id']
    assert services['jobs'].run_one()
    replay = upload_video(client, official).json()
    assert replay['duplicate'] is True and replay['photo_id'] == first['photo_id']
    assert video_task(services)['job_id'] == job_id
    assert len(services['db'].all("SELECT * FROM jobs WHERE kind='camera_video_time'")) == 1
    assert len(services['db'].all('SELECT * FROM camera_video_times')) == 1
    assert len(extraction) == 1 and not services['jobs'].run_one()


def test_old_capture_without_time_job_stays_untouched_on_replay(client, official, services, extraction):
    first = upload_video(client, official).json()
    job_id = video_task(services)['job_id']
    # Model a capture created before this feature, with no extraction record.
    with services['db'].tx() as con:
        con.execute('DELETE FROM camera_video_times')
        con.execute('DELETE FROM jobs WHERE id=?', (job_id,))
    before = services['domain'].photo(first['photo_id'])
    replay = upload_video(client, official)
    assert replay.status_code == 200 and replay.json()['duplicate'] is True
    assert not video_task(services) and not services['db'].all("SELECT * FROM jobs WHERE kind='camera_video_time'")
    assert not services['jobs'].run_one() and not extraction
    assert services['domain'].photo(first['photo_id']) == before
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert capture['photo_time'] is None and capture['time_state'] is None
    assert capture['time_basis'] == 'unavailable'


@pytest.mark.parametrize('change', ['confirmed_field', 'confirmed_photo', 'existing_time'])
@pytest.mark.parametrize('when', ['queued', 'extracting'])
def test_recognition_preserves_human_confirmations_and_existing_time(
        client, official, services, monkeypatch, change, when):
    receipt = upload_video(client, official).json()
    photo_id = receipt['photo_id']
    protected = {}

    def change_photo():
        if change == 'confirmed_field':
            services['domain'].update_photo(photo_id, {'watermark_time': None})
        elif change == 'confirmed_photo':
            services['domain'].update_photo(photo_id, {'confirm': True})
        else:
            with services['db'].tx() as con:
                con.execute('UPDATE photos SET watermark_time=? WHERE id=?', ('2026-10-25 16:12:34', photo_id))
        protected.update(services['db'].one('SELECT * FROM photos WHERE id=?', (photo_id,)))

    if when == 'queued':
        change_photo()

    def extract(path):
        if when == 'extracting':
            change_photo()
        return recognized_result()

    monkeypatch.setattr(services['camera_video_times'], 'extract', extract)
    assert services['jobs'].run_one()
    assert services['db'].one('SELECT * FROM photos WHERE id=?', (photo_id,)) == protected
    result = json.loads(video_task(services)['result'])
    assert result['state'] == 'recognized' and result['applied_to_photo'] is False
    assert result['preserved_existing_fields'] is True
    assert source_metadata(services, photo_id)['time_basis'] == 'unavailable'


@pytest.mark.parametrize('when', ['queued', 'extracting'])
def test_replaced_original_rejects_stale_ocr_result(client, official, services, monkeypatch, when):
    receipt = upload_video(client, official).json()
    replacement = services['storage'].put(video_container(seconds=4), 'replacement.mp4')
    calls = []

    def replace():
        with services['db'].tx() as con:
            con.execute('UPDATE photos SET blob_hash=? WHERE id=?', (replacement['hash'], receipt['photo_id']))

    if when == 'queued':
        replace()

    def extract(path):
        calls.append(path)
        replace()
        return recognized_result()

    monkeypatch.setattr(services['camera_video_times'], 'extract', extract)
    assert services['jobs'].run_one()
    assert len(calls) == (1 if when == 'extracting' else 0)
    task = video_task(services)
    result = json.loads(task['result'])
    photo = services['domain'].photo(receipt['photo_id'])
    assert task['state'] == 'skipped' and result['applied_to_photo'] is False
    assert result['watermark_time'] is None
    assert photo['blob_hash'] == replacement['hash'] and photo['watermark_time'] is None
    assert client.get('/api/camera/admin/official').json()['captures'][0]['photo_time'] is None


def test_deleting_queued_video_cancels_job_without_recreating_photo(client, official, services, extraction):
    receipt = upload_video(client, official).json()
    job_id = video_task(services)['job_id']
    response = client.delete('/api/photos/' + receipt['photo_id'])
    assert response.status_code == 200, response.text
    assert services['jobs'].get(job_id)['state'] == 'cancelled'
    assert not services['jobs'].run_one() and not extraction
    assert services['db'].one('SELECT id FROM photos WHERE id=?', (receipt['photo_id'],)) is None
    assert upload_video(client, official).status_code == 410
    assert not services['db'].all('SELECT * FROM photos')


def test_failed_extraction_keeps_upload_and_can_retry(client, official, services, monkeypatch):
    def fail(path):
        raise RuntimeError('模拟首帧读取失败')

    monkeypatch.setattr(services['camera_video_times'], 'extract', fail)
    original = video_container()
    response = upload_video(client, official, original)
    assert response.status_code == 200 and response.json()['state'] == 'uploaded'
    receipt = response.json()
    assert services['jobs'].run_one()
    task = video_task(services)
    assert task['state'] == 'failed' and services['jobs'].get(task['job_id'])['state'] == 'failed'
    assert services['domain'].photo(receipt['photo_id'])['watermark_time'] is None
    assert client.get('/api/photos/' + receipt['photo_id'] + '/original').content == original
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert capture['photo_time'] is None and capture['time_state'] == 'failed'

    monkeypatch.setattr(services['camera_video_times'], 'extract', lambda path: recognized_result())
    assert services['jobs'].retry(task['job_id'])['state'] == 'queued'
    assert services['jobs'].run_one()
    assert video_task(services)['state'] == 'recognized'
    assert services['jobs'].get(task['job_id'])['attempts'] == 2
    assert services['domain'].photo(receipt['photo_id'])['watermark_time'] == VISIBLE_TIME
    assert client.get('/api/photos/' + receipt['photo_id'] + '/original').content == original


def test_disabled_local_ocr_leaves_uploaded_video_time_unknown(client, official, services, extraction):
    services['db'].set_setting('recognition', {'local_ocr': False})
    response = upload_video(client, official)
    assert response.status_code == 200 and response.json()['state'] == 'uploaded'
    assert services['jobs'].run_one() and not extraction
    task = video_task(services)
    assert task['state'] == 'failed' and '已关闭' in json.loads(task['result'])['reason']
    assert services['domain'].photo(response.json()['photo_id'])['watermark_time'] is None
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert capture['photo_time'] is None and capture['time_basis'] == 'unavailable'


def test_ambiguous_time_keeps_photo_unknown_without_using_phone_time(client, official, services, monkeypatch):
    monkeypatch.setattr(services['camera_video_times'], 'extract', lambda path: {
        'state': 'needs_review', 'watermark_time': None, 'reason': 'conflicting_times',
        'time_basis': 'video_watermark_ocr', 'method': 'first_frame'})
    receipt = upload_video(client, official, captured_at='2099-01-01T01:00:00+08:00').json()
    assert services['jobs'].run_one()
    assert video_task(services)['state'] == 'needs_review'
    assert services['domain'].photo(receipt['photo_id'])['watermark_time'] is None
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert capture['time_state'] == 'needs_review' and capture['photo_time'] is None
    assert capture['time_basis'] == 'unavailable'


def test_worker_restart_resumes_interrupted_video_time_job(client, official, services, monkeypatch):
    receipt = upload_video(client, official).json()
    task = video_task(services)
    with services['db'].tx() as con:
        con.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (task['job_id'],))
        con.execute("UPDATE camera_video_times SET state='running'")
    extracted = threading.Event()
    calls = []

    def extract(path):
        calls.append(path)
        extracted.set()
        return recognized_result()

    monkeypatch.setattr(services['camera_video_times'], 'extract', extract)
    jobs = services['jobs']
    try:
        jobs.start()
        assert extracted.wait(timeout=5), 'Interrupted video extraction was not resumed'
    finally:
        # stop joins the worker so its final database commit has finished.
        jobs.stop()
    assert not jobs._thread.is_alive() and not jobs._network_thread.is_alive()
    job = jobs.get(task['job_id'])
    assert job['state'] == 'completed' and job['attempts'] == 2
    assert video_task(services)['state'] == 'recognized'
    assert services['domain'].photo(receipt['photo_id'])['watermark_time'] == VISIBLE_TIME
    assert len(calls) == 1 and not official['calls']


def test_migration_preserves_old_video_and_does_not_enqueue_history(client, official, services, extraction):
    from tests.conftest import rewind_additive_test_schema
    receipt = upload_video(client, official).json()
    db = services['db']
    task = video_task(services)
    capture_before = db.one('SELECT * FROM camera_official_captures')
    photo_before = db.one('SELECT * FROM photos WHERE id=?', (receipt['photo_id'],))
    sources_before = db.all('SELECT * FROM photo_sources WHERE photo_id=?', (receipt['photo_id'],))
    with db.tx() as con:
        # Replay all later additive migrations from a real schema-39 layout.
        rewind_additive_test_schema(con, 39)
        con.execute('DELETE FROM jobs WHERE id=?', (task['job_id'],))

    db.initialize()

    assert db.one('SELECT version FROM schema_version WHERE version=40') == {'version': 40}
    assert not db.all('SELECT * FROM camera_video_times')
    assert not db.all("SELECT * FROM jobs WHERE kind='camera_video_time'")
    assert db.one('SELECT * FROM camera_official_captures') == capture_before
    assert db.one('SELECT * FROM photos WHERE id=?', (receipt['photo_id'],)) == photo_before
    assert db.all('SELECT * FROM photo_sources WHERE photo_id=?', (receipt['photo_id'],)) == sources_before
    assert not services['jobs'].run_one() and not extraction
    replay = upload_video(client, official)
    assert replay.status_code == 200 and replay.json()['duplicate'] is True
    assert not db.all('SELECT * FROM camera_video_times')
    assert not db.all("SELECT * FROM jobs WHERE kind='camera_video_time'")
    capture = client.get('/api/camera/admin/official').json()['captures'][0]
    assert capture['photo_time'] is None and capture['time_state'] is None
