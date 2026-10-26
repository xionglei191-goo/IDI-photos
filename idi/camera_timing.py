"""Request-scoped camera timings; no credentials or image metadata are logged."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextlib import contextmanager

LOG = logging.getLogger('uvicorn.error.camera_timing')
SCOPE_KEY = 'idi_camera_timing'


class CameraTiming:
    def __init__(self, trace=''):
        self.trace = trace if re.fullmatch(r'[a-f0-9]{32}', trace) else uuid.uuid4().hex
        self.started = time.perf_counter()
        self.body_finished = None
        self.body_started = None
        self.bytes = 0
        self.phases = {}

    @contextmanager
    def measure(self, name):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0) + (time.perf_counter() - started) * 1000

    def handler(self):
        self.phases['dispatch'] = (time.perf_counter() - (self.body_finished or self.started)) * 1000

    def queued(self, submitted):
        self.phases['queue'] = self.phases.get('queue', 0) + (time.perf_counter() - submitted) * 1000

    def header(self):
        self.phases['total'] = (time.perf_counter() - self.started) * 1000
        return ', '.join(f'idi_{name};dur={duration:.1f}' for name, duration in self.phases.items())


class CameraTimingMiddleware:
    """Measure at ASGI ingress, before multipart parsing and route dispatch.

    Receiving starts at the first body read and includes overlap with parsing.
    Total covers this IDI process only;
    neither upstream proxies nor the vendor SDK are included.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        enabled = scope['type'] == 'http' and (
            scope.get('method') == 'POST' and path == '/api/camera/official-captures'
            or scope.get('method') == 'GET' and path.startswith('/api/camera/official-captures/'))
        if not enabled:
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        timing = CameraTiming(headers.get(b'x-idi-camera-trace', b'').decode('ascii', 'ignore'))
        scope[SCOPE_KEY] = timing
        status = 500

        async def timed_receive():
            if timing.body_started is None:
                timing.body_started = time.perf_counter()
            message = await receive()
            if message['type'] == 'http.request':
                timing.bytes += len(message.get('body', b''))
                if not message.get('more_body', False) and timing.body_finished is None:
                    timing.body_finished = time.perf_counter()
                    timing.phases['receive'] = (timing.body_finished - timing.body_started) * 1000
            return message

        async def timed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message = {**message, 'headers': [*message.get('headers', []),
                    (b'server-timing', timing.header().encode('ascii')),
                    (b'x-idi-camera-trace', timing.trace.encode('ascii'))]}
            await send(message)

        try:
            await self.app(scope, timed_receive, timed_send)
        finally:
            # No URL, device identifier, token, filename, metadata or vendor result.
            LOG.info('camera_upload_timing %s', json.dumps({
                'trace': timing.trace, 'method': scope['method'], 'status': status,
                'body_bytes': timing.bytes,
                'durations_ms': {name: round(value, 1) for name, value in timing.phases.items()},
            }, separators=(',', ':')))
