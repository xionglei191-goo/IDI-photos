import asyncio
import json
import re
import threading
import time

import httpx

from idi.camera_timing import CameraTimingMiddleware, SCOPE_KEY
from tests.test_official_camera import official, upload


def metrics(response):
    return {key: float(value) for key, value in re.findall(r'idi_(\w+);dur=([\d.]+)', response.headers['server-timing'])}


def test_timing_locates_storage_delay_and_keeps_idempotency(client, official, services, monkeypatch, caplog):
    official['service'].settings.save({'auto_verify': False})
    original = services['storage'].put
    def delayed(*args, **kwargs):
        time.sleep(.12)
        return original(*args, **kwargs)
    monkeypatch.setattr(services['storage'], 'put', delayed)
    caplog.set_level('INFO', logger='uvicorn.error.camera_timing')
    trace = 'a' * 32
    official['headers']['X-IDI-Camera-Trace'] = trace
    response = upload(client, official)
    assert response.status_code == 200, response.text
    measured = metrics(response)
    assert measured['store'] >= 110
    assert measured['total'] >= measured['store']
    assert set(measured) >= {'receive', 'dispatch', 'queue', 'auth', 'file_read', 'lock', 'validate', 'store', 'archive', 'total'}
    assert response.headers['x-idi-camera-trace'] == trace
    receipt = response.json()
    again = upload(client, official)
    assert again.status_code == 200 and again.json()['duplicate']
    assert again.json()['photo_id'] == receipt['photo_id']
    assert 'store' not in metrics(again)
    assert len(services['db'].all('SELECT * FROM photos')) == 1
    assert not official['calls']
    log = next(record.message for record in caplog.records if trace in record.message)
    assert 'body_bytes' in log and 'durations_ms' in log
    assert official['meta']['case_number'] not in log
    assert official['headers']['Authorization'] not in log


def test_receipt_errors_have_timings_and_reject_unsafe_trace(client, official):
    response = client.get('/api/camera/official-captures/' + official['meta']['capture_id'],
                          headers={**official['headers'], 'X-IDI-Camera-Trace': 'sensitive-invalid-trace'})
    assert response.status_code == 404
    assert metrics(response)['lookup'] >= 0
    assert re.fullmatch('[a-f0-9]{32}', response.headers['x-idi-camera-trace'])
    assert 'server-timing' not in client.get('/api/health').headers


def test_asgi_receiving_separates_slow_body_from_processing():
    sent = []
    async def run():
        async def app(scope, receive, send):
            while (await receive()).get('more_body'):
                pass
            timing = scope[SCOPE_KEY]
            timing.handler()
            with timing.measure('archive'):
                await asyncio.sleep(.02)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
        count = 0
        async def receive():
            nonlocal count
            await asyncio.sleep(.06)
            count += 1
            return {'type': 'http.request', 'body': b'original', 'more_body': count == 1}
        async def send(message):
            sent.append(message)
        await CameraTimingMiddleware(app)({'type': 'http', 'method': 'POST',
            'path': '/api/camera/official-captures', 'headers': []}, receive, send)
    asyncio.run(run())
    response = httpx.Response(200, headers=dict(sent[0]['headers']))
    measured = metrics(response)
    assert measured['receive'] >= 110
    assert measured['archive'] >= 15
    assert measured['total'] >= measured['receive'] + measured['archive'] - 1


def test_slow_camera_auth_does_not_block_health_request(client, official, monkeypatch):
    camera = official['service'].camera
    original = camera.authenticate
    entered = threading.Event()
    release = threading.Event()
    def delayed(request):
        entered.set()
        assert release.wait(4)
        return original(request)
    monkeypatch.setattr(camera, 'authenticate', delayed)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://testserver') as transport:
            pending = asyncio.create_task(transport.post('/api/camera/official-captures',
                headers={**official['headers'], 'X-IDI-Request': '1'},
                data={'metadata': json.dumps(official['meta'])},
                files={'image': ('photo.jpg', official['image'], 'image/jpeg')}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                health = await asyncio.wait_for(transport.get('/api/health'), timeout=.8)
                assert health.status_code == 200
                assert not pending.done()
            finally:
                release.set()
            assert (await pending).status_code == 200
    asyncio.run(run())
