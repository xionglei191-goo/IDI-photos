"""One hidden child per enabled Feishu connector; SDK callbacks only enqueue."""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

from ..api import create_app
from ..config import Settings
from ..db import now
from .feishu import FeishuCapture, FeishuClient, normalize
from .secret_store import SecretStore


def parent_guard(pid):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel.OpenProcess(0x00100000, False, pid)
        return lambda: bool(handle) and kernel.WaitForSingleObject(handle, 0) == 258
    def alive():
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    return alive


def run_capture_queue(capture, alive, stop):
    """Wake after durable enqueue; bounded deadlines also cover external writers."""
    class ParentStop:
        def is_set(self):
            return stop.is_set() or not alive()

    guard = ParentStop()
    service, runtime = capture.service, capture.db.runtime
    next_history, next_config, config_token = 0, 0, None
    while not guard.is_set():
        observed = capture._signal.token()
        with runtime.measure('capture.cycle'), capture.db.connection_scope():
            token, current = service._config_signal.token(), time.monotonic()
            if token != config_token or current >= next_config:
                latest = service.config(capture.connector_id)
                if not latest['enabled']:
                    return
                capture.connector = latest
                config_token, next_config = token, current + 15
            capture.heartbeat()
            if current >= next_history:
                capture.catch_up()
                next_history = time.monotonic() + 30
            processed = 0
            for _ in range(4):
                if guard.is_set() or not capture.process_one():
                    break
                processed += 1
                capture.heartbeat()
            current = time.monotonic()
            delay = max(0, min(capture.next_delay(), next_history - current,
                               next_config - current, capture._heartbeat_at + 15 - current))
        if processed < 4:
            runtime.wait('capture.cycle', capture._signal, observed, delay, guard)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--connector-id', required=True)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    args = parser.parse_args()
    services = create_app(Settings(Path(args.data_dir).resolve(), worker_enabled=False)).state.services
    service = services['connectors']
    connector = service.config(args.connector_id)
    if connector['kind'] != 'feishu' or not connector['enabled']:
        return
    secret = SecretStore(service.settings.data_dir).load(connector['id'])
    client = FeishuClient(connector['config']['app_id'], secret)
    capture = FeishuCapture(service, connector['id'], client)
    capture.recover()
    alive = parent_guard(args.parent_pid)
    stop = threading.Event()
    service.db.runtime.start(role='feishu-' + connector['id'])

    def work():
        code = 0
        try:
            run_capture_queue(capture, alive, stop)
        except Exception:
            capture.state(phase='error', error='接收队列暂时停止，服务将自动重启接收进程')
            code = 1
        finally:
            stop.set()
            service.db.runtime.stop()
            os._exit(code)

    thread = threading.Thread(target=work, name='feishu-download', daemon=True)
    thread.start()
    try:
        ws = create_ws(capture, connector['config']['app_id'], secret, services['review_cards'])
        ws.start()
    except Exception:
        capture.state(phase='error', error='飞书长连接启动失败，请检查凭据、应用发布及事件订阅配置')
    finally:
        stop.set()
        capture._signal.wake(False)
        service.db.runtime.stop()
        client.close()


def create_ws(capture, app_id, secret, cards=None):
    # Official SDK owns the main asyncio loop. Suppress its URL-bearing logs.
    import lark_oapi as lark
    lark.logger.disabled = True
    logging.getLogger('Lark').disabled = True

    def receive(data):
        capture.receive(normalize(json.loads(lark.JSON.marshal(data.event))), live=True)

    builder = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(receive)
    if cards is not None:
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        def card_action(data):
            response = cards.receive_callback(capture.connector_id, app_id, json.loads(lark.JSON.marshal(data)))
            return P2CardActionTriggerResponse(response)

        builder.register_p2_card_action_trigger(card_action)
    handler = builder.build()

    class ObservedClient(lark.ws.Client):
        # lark-oapi==1.7.5: _connect returns only after the socket is established.
        async def _connect(self):
            await super()._connect()
            capture.state(phase='listening', connected_at=now(), error=None)

        async def _disconnect(self):
            capture.state(phase='reconnecting', error='飞书长连接断开，正在重连；消息补收仍会重试')
            await super()._disconnect()

    ws = ObservedClient(app_id, secret, event_handler=handler, log_level=lark.LogLevel.ERROR)
    ws.on_reconnecting = lambda: capture.state(phase='reconnecting', error='正在连接飞书，请检查网络和长连接配置')
    return ws


if __name__ == '__main__':
    main()
