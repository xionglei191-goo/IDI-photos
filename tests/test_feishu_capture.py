import json
import os
import time
from pathlib import Path

import httpx
import pytest

from idi.connectors.feishu import FeishuCapture, FeishuClient, normalize
from idi.connectors.secret_store import SecretStore
from idi.domain import DomainError
from conftest import image_bytes


@pytest.fixture
def connector(services, monkeypatch):
    vault = {}
    monkeypatch.setattr(SecretStore, 'save', lambda self, key, value: vault.update({key: value}))
    monkeypatch.setattr(SecretStore, 'load', lambda self, key: vault[key])
    monkeypatch.setattr(SecretStore, 'exists', lambda self, key: key in vault)
    return services['connectors'].save({'name': '照片收集群', 'kind': 'feishu', 'enabled': True, 'app_secret': 'test-secret-only',
                                       'config': {'app_id': 'cli_test', 'chat_id': 'oc_test', 'chat_name': '照片收集群', 'auto_ocr': True}})


def event(mid='om_one', *, kind='image', content=None, chat='oc_test', sender='user', timestamp=None):
    return normalize({'sender': {'sender_type': sender, 'sender_id': {'open_id': 'ou_test'}},
                      'message': {'message_id': mid, 'chat_id': chat, 'message_type': kind,
                                  'create_time': str(timestamp or int(time.time() * 1000)),
                                  'content': json.dumps(content or {'image_key': 'img_test'})}})


class FakeClient:
    def __init__(self):
        self.calls, self.fail, self.history_items = [], set(), []

    def download(self, mid, resource, folder, limit):
        self.calls.append(resource['key'])
        if resource['key'] in self.fail:
            raise DomainError('模拟附件暂时不可用')
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (resource['key'] + '.png')
        path.write_bytes(image_bytes('red' if resource['key'].endswith('two') else 'navy'))
        return path, path.name

    def history(self, chat, start, end):
        yield from self.history_items


def test_capture_duplicate_filter_provenance_and_local_ocr(services, connector):
    client = FakeClient()
    capture = FeishuCapture(services['connectors'], connector['id'], client)
    assert not capture.receive(event(chat='oc_other'))
    assert not capture.receive(event(sender='app'))
    assert not capture.receive(event(timestamp=1))
    assert capture.receive(event(), live=True)
    assert not capture.receive(event())
    assert capture.process_one()
    assert not capture.process_one()
    assert client.calls == ['img_test']
    source = services['db'].one('SELECT * FROM photo_sources')
    assert source['source_type'] == 'feishu'
    assert source['group_name'] == '照片收集群'
    assert source['sender'] == 'ou_test'
    assert json.loads(source['metadata'])['feishu_message_id'] == 'om_one'
    photo = services['domain'].photo(source['photo_id'])
    assert photo['stage'] == 'unknown' and photo['case_id'] is None
    assert services['db'].one("SELECT COUNT(*) AS n FROM jobs WHERE kind='recognize'")['n'] == 1
    # Same bytes in a different message retain another source, not another photo.
    capture.receive(event('om_second'))
    capture.process_one()
    assert services['db'].one('SELECT COUNT(*) AS n FROM photos')['n'] == 1
    assert services['db'].one('SELECT COUNT(*) AS n FROM photo_sources')['n'] == 2


def test_post_partial_download_retry_skips_saved_attachment(services, connector):
    client = FakeClient()
    client.fail = {'img_two'}
    capture = FeishuCapture(services['connectors'], connector['id'], client)
    payload = event(kind='post', content={'zh_cn': {'title': '今日照片', 'content': [[{'tag': 'text', 'text': '现场记录'}, {'tag': 'img', 'image_key': 'img_one'}, {'tag': 'img', 'image_key': 'img_two'}]]}})
    capture.receive(payload)
    capture.process_one()
    record = services['connectors'].get(connector['id'])
    assert record['counts']['saved'] == 1 and record['counts']['failed'] == 1 and record['queue']['failed'] == 1
    failed = services['db'].one("SELECT * FROM messages WHERE media_state='failed'")
    assert services['connectors'].retry_message(failed['id'])['queued'] == 1
    client.fail.clear()
    capture.process_one()
    assert client.calls == ['img_one', 'img_two', 'img_two']
    record = services['connectors'].get(connector['id'])
    assert record['counts']['saved'] == 2 and not record['counts']['failed'] and record['queue']['completed'] == 1
    assert services['db'].one('SELECT COUNT(*) AS n FROM photos')['n'] == 2


def test_restart_and_history_failure_never_advance_cursor(services, connector):
    client = FakeClient()
    capture = FeishuCapture(services['connectors'], connector['id'], client)
    start = services['db'].one('SELECT * FROM feishu_state')['cursor']
    payload = event()
    def incomplete(*args):
        yield payload
        raise DomainError('模拟第二页断线')
    client.history = incomplete
    assert not capture.catch_up(end=start + 10)
    assert services['db'].one('SELECT * FROM feishu_state')['cursor'] == start
    assert services['db'].one('SELECT COUNT(*) AS n FROM feishu_inbox')['n'] == 1
    with services['db'].tx() as con:
        con.execute("UPDATE feishu_inbox SET state='processing'")
    restarted = FeishuCapture(services['connectors'], connector['id'], client)
    restarted.recover()
    client.history = lambda *args: iter([payload, event('om_afterrestart')])
    assert restarted.catch_up(end=start + 20)
    assert services['db'].one('SELECT * FROM feishu_state')['cursor'] == start + 20
    assert services['db'].one('SELECT COUNT(*) AS n FROM feishu_inbox')['n'] == 2
    assert restarted.process_one() and restarted.process_one()
    assert services['db'].one("SELECT COUNT(*) AS n FROM feishu_inbox WHERE state='completed'")['n'] == 2


def test_history_pagination_normalizes_body_and_sender():
    paths = []
    def respond(request):
        paths.append((request.method, request.url.path))
        if request.url.path.endswith('/internal'):
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'test-token', 'expire': 7200})
        page = request.url.params.get('page_token')
        item = {'message_id': 'om_b' if page else 'om_a', 'msg_type': 'image', 'create_time': '1800000000000', 'body': {'content': '{"image_key":"img_test"}'}, 'sender': {'sender_type': 'user', 'id': 'ou_test'}}
        return httpx.Response(200, json={'code': 0, 'data': {'items': [item], 'has_more': not bool(page), 'page_token': 'page2' if not page else ''}})
    client = FeishuClient('cli_test', 'test-secret', transport=httpx.MockTransport(respond))
    rows = list(client.history('oc_test', 1, 2))
    assert [row['message_id'] for row in rows] == ['om_a', 'om_b']
    assert rows[0]['chat_id'] == 'oc_test' and rows[0]['message_type'] == 'image'
    assert [method for method, _ in paths] == ['POST', 'GET', 'GET']
    client.close()


def test_download_bounds_refresh_and_no_redirect(tmp_path):
    calls, tokens = [], []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/internal'):
            token = f'token{len(tokens)}'
            tokens.append(token)
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': token, 'expire': 7200})
        if request.headers['authorization'] == 'Bearer token0':
            return httpx.Response(200, json={'code': 99991663})
        if request.url.path.endswith('img_redirect'):
            return httpx.Response(302, headers={'location': 'https://example.invalid/secret'})
        return httpx.Response(200, content=image_bytes(), headers={'content-type': 'image/png'})
    client = FeishuClient('cli_test', 'test-secret', transport=httpx.MockTransport(respond))
    resource = {'key': 'img_one', 'type': 'image'}
    path, name = client.download('om_test', resource, tmp_path, 1024 * 1024)
    assert path.read_bytes() == image_bytes() and name.endswith('.png') and len(tokens) == 2
    with pytest.raises(DomainError, match='大小限制'):
        client.download('om_test', resource, tmp_path, 10)
    with pytest.raises(DomainError):
        client.download('om_test', {**resource, 'key': 'img_redirect'}, tmp_path, 1024)
    assert not list(tmp_path.glob('*.partial'))
    assert all('/open-apis/' in path for path in calls)
    client.close()


def test_secret_not_returned_or_stored_in_database(client, services, connector):
    response = client.get('/api/connectors/' + connector['id'])
    assert response.json()['secret_configured'] is True
    assert 'test-secret-only' not in response.text
    for table in ('connectors', 'audit'):
        assert 'test-secret-only' not in json.dumps(services['db'].all('SELECT * FROM ' + table))
    assert client.put('/api/connectors/' + connector['id'], json={**connector, 'config': {**connector['config'], 'chat_id': 'oc_changed'}}).status_code == 400
    bad = client.post('/api/connectors/' + connector['id'] + '/events', json={'source_key': 'wrong', 'chat_id': 'oc_other'})
    assert bad.status_code == 400


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_real_dpapi_roundtrip_and_ciphertext_only(tmp_path):
    store = SecretStore(tmp_path)
    connector_id = 'a' * 32
    value = 'synthetic-test-secret-123456'
    store.save(connector_id, value)
    assert store.load(connector_id) == value
    assert value.encode() not in store.path(connector_id).read_bytes()
    with pytest.raises(DomainError):
        store.load('../outside')


def test_sdk_dispatch_is_durable_before_download_and_reports_connection(services, connector, monkeypatch):
    import asyncio
    import lark_oapi as lark
    from idi.connectors.feishu_worker import create_ws
    client = FakeClient()
    capture = FeishuCapture(services['connectors'], connector['id'], client)
    ws = create_ws(capture, 'cli_test', 'synthetic-secret')
    raw = {'schema': '2.0', 'header': {'event_id': 'evt_test', 'event_type': 'im.message.receive_v1'},
           'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'ou_test'}},
                     'message': {'message_id': 'om_sdk', 'chat_id': 'oc_test', 'chat_type': 'group', 'message_type': 'image',
                                 'create_time': str(int(time.time() * 1000)), 'content': '{"image_key":"img_sdk"}'}}}
    ws._event_handler._do_without_validation(json.dumps(raw).encode())
    assert services['db'].one('SELECT * FROM feishu_inbox')['state'] == 'pending'
    assert client.calls == []
    async def connected(self):
        return None
    monkeypatch.setattr(lark.ws.Client, '_connect', connected)
    # The SDK owns a persistent loop; do not replace or close it with asyncio.run.
    asyncio.get_event_loop().run_until_complete(ws._connect())
    assert services['connectors'].get(connector['id'])['status'] == 'listening'


def test_backfill_context_uses_original_message_time(services, connector):
    client = FakeClient()
    capture = FeishuCapture(services['connectors'], connector['id'], client)
    start = int(time.time()) - 7200
    with services['db'].tx() as con:
        con.execute('UPDATE feishu_state SET started_at=?,cursor=?', (start, start))
    # All messages are received now, but old and future captions must be excluded.
    capture.receive(event('om_old', kind='text', content={'text': '旧案件说明'}, timestamp=(start + 1) * 1000))
    capture.receive(event('om_close', kind='text', content={'text': '当前案件说明'}, timestamp=(start + 3500) * 1000))
    capture.receive(event('om_future', kind='text', content={'text': '下一案件说明'}, timestamp=(start + 4500) * 1000))
    while capture.process_one():
        pass
    capture.receive(event('om_image', timestamp=(start + 3600) * 1000))
    capture.process_one()
    metadata = json.loads(services['db'].one('SELECT * FROM photo_sources')['metadata'])
    assert '当前案件说明' in metadata['context_text']
    assert '旧案件说明' not in metadata['context_text'] and '下一案件说明' not in metadata['context_text']
