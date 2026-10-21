"""Configurable Xhey client. Protocol follows docs.xhey.top/docs/trutu-build-api/.

Native/H5 signing and verification credentials remain on the server. Paired devices
receive only a fresh signed camera session; secrets are never logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from .connectors.secret_store import protect
from .db import dump
from .domain import DomainError
from .storage import Storage


class XheySettings:
    FIELDS = ('sdk_app_id', 'sdk_secret_key', 'group_key', 'group_secret', 'group_watermark_id')

    def __init__(self, data_dir):
        self.path = data_dir / 'secrets' / 'xhey-camera.bin'

    def load(self):
        if not self.path.exists():
            return {'enabled': False}
        try:
            value = json.loads(protect(self.path.read_bytes(), decrypt=True))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (OSError, ValueError, DomainError) as error:
            raise DomainError('今日相机配置无法解密，请恢复原凭据加密环境或重新配置') from error

    @staticmethod
    def ready(value):
        return value.get('enabled') is True and all(value.get(k) for k in XheySettings.FIELDS[:2])

    @staticmethod
    def verification_ready(value):
        return XheySettings.ready(value) and all(value.get(k) for k in ('group_key', 'group_secret'))

    def status(self):
        value = self.load()
        return {'enabled': value.get('enabled', False), 'ready': self.ready(value),
                'auto_verify': value.get('auto_verify') is True,
                'verification_ready': self.verification_ready(value),
                **{'has_' + k: bool(value.get(k)) for k in self.FIELDS},
                'group_watermark_id': value.get('group_watermark_id', '')}

    def save(self, data):
        if not isinstance(data, dict) or set(data) - set(self.FIELDS) - {'enabled', 'auto_verify'}:
            raise DomainError('今日相机配置字段无效')
        value = self.load()
        for flag in ('enabled', 'auto_verify'):
            if flag in data:
                if type(data[flag]) is not bool:
                    raise DomainError('启用状态无效')
                value[flag] = data[flag]
        for key in self.FIELDS:
            if key not in data:
                continue
            if not isinstance(data[key], str) or len(data[key]) > 4096:
                raise DomainError('今日相机配置格式无效')
            # Blank retains a saved credential. Disabling never deletes it.
            if data[key].strip():
                value[key] = data[key].strip()
        if value.get('enabled') and not self.ready(value):
            raise DomainError('启用前请填写 SDK App ID 与 Secret Key；团队水印模板 ID 可选')
        if value.get('enabled') and value.get('auto_verify') and not self.verification_ready(value):
            raise DomainError('开启本地自动验真前请填写验真 Group Key 与 Group Secret')
        Storage._atomic(self.path, protect(dump(value).encode('utf-8')))
        return self.status()

    def h5_status(self):
        value = self.load()
        return {'ready': self.ready(value), 'mode': 'h5',
                'auto_verify': value.get('auto_verify') is True,
                'has_team_watermark': bool(value.get('group_watermark_id')),
                'message': '' if self.ready(value) else '请在电脑端启用已保存的 SDK 授权'}

    def h5_signature(self):
        value = self.load()
        if not self.ready(value):
            raise DomainError('电脑端官方授权尚未启用', 409)
        params = {'appid': value['sdk_app_id'], 'noncestr': secrets.token_hex(16), 'timestamp': int(time.time())}
        params['signature'] = sdk_signature(value['sdk_secret_key'], params)
        return params, value.get('group_watermark_id', '')


def sdk_signature(secret, params):
    """Official H5 SDK auth: sorted appid/noncestr/timestamp, HMAC SHA256, Base64."""
    body = '&'.join(f'{key}={params[key]}' for key in sorted(params))
    return base64.b64encode(hmac.digest(secret.encode('utf-8'), body.encode('utf-8'), 'sha256')).decode('ascii')


class XheyError(Exception):
    """A sanitized message, never a remote response body or authorization header."""


def signed_request(group_key, group_secret, payload, timestamp):
    body = dump(payload).encode('utf-8')

    def sign(data):
        return base64.b64encode(hmac.new(group_secret.encode('utf-8'), data, hashlib.sha256).digest()).decode('ascii')

    data_sign = sign(body)
    text = f'groupKey={group_key}&sign={data_sign}&timestamp={timestamp}'
    return body, {'GroupKey': group_key, 'Timestamp': str(timestamp), 'Signature': sign(text.encode('utf-8')),
                  'Content-Type': 'application/json; charset=utf-8'}


def official_time(value):
    if not isinstance(value, str):
        raise XheyError('官方结果缺少有效拍摄时间，未归档')
    for pattern in ('%Y年%m月%d日 %H:%M:%S', '%Y年%m月%d日 %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value.strip(), pattern).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
    raise XheyError('官方拍摄时间格式未识别，未归档')


def parse_verdict(data):
    # One create call contains one base64 image. Index 0 is the binding to those bytes.
    if type(data.get('status')) is not int or data['status'] != 0:
        raise XheyError('官方验真任务返回异常，未归档')
    task_status = data.get('taskStatus')
    if type(task_status) is not int:
        raise XheyError('官方验真任务状态缺失，未归档')
    if task_status in (1, 2):
        return 'processing', {}
    if task_status == 6:
        return 'cancelled', {}
    if task_status != 5:
        raise XheyError('官方验真任务状态未知，未归档')
    photos = data.get('list')
    if not isinstance(photos, list) or len(photos) != 1 or not isinstance(photos[0], dict):
        raise XheyError('官方验真照片数量不匹配，未归档')
    photo = photos[0]
    if type(photo.get('photoBase64Idx')) is not int or photo['photoBase64Idx'] != 0:
        raise XheyError('官方验真照片索引不匹配，未归档')
    status = photo.get('status')
    if type(status) is not int:
        raise XheyError('官方验真照片状态缺失，未归档')
    if status == 1:
        return 'processing', {}
    # Only documented definitive negatives are a rejection; algorithm/network errors are unknown.
    if status in (-2301, -2303, -2305):
        return 'rejected', {'status': status}
    if status != 0:
        raise XheyError(f'官方验真未给出通过结果（状态 {status}），请人工核对')
    code = photo.get('antiFakeCode')
    if not isinstance(code, str) or not code.strip() or len(code) > 256:
        raise XheyError('官方结果缺少防伪码，未归档')
    result = {'status': 0, 'antiFakeCode': code.strip(), 'photoTime': official_time(photo.get('photoTime')),
              'photoBase64Idx': 0}
    # Bound and select public result fields, never persist raw responses or vendor messages.
    for key in ('photoAddress', 'lat', 'lng'):
        if isinstance(photo.get(key), (str, int, float)):
            result[key] = str(photo[key])[:500]
    return 'verified', result


class XheyClient:
    BASE = 'https://openapi.xhey.top'

    def __init__(self, config, *, transport=None):
        self.config, self.transport = config, transport

    def request(self, action, payload, *, timeout=20):
        body, headers = signed_request(self.config['group_key'], self.config['group_secret'], payload, int(time.time()))
        try:
            with httpx.Client(transport=self.transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(self.BASE + '/v2/truth_build/' + action, content=body, headers=headers)
            if response.status_code != 200:
                raise XheyError(f'官方验真服务 HTTP {response.status_code}，未归档')
            if len(response.content) > 1_000_000:
                raise XheyError('官方验真响应过大，未归档')
            result = response.json()
        except (httpx.HTTPError, ValueError) as error:
            detail = ('创建结果可能未知；重试可能新建任务并重复计费，请先核对官方任务'
                      if action == 'create' else '可稍后继续查询原任务')
            raise XheyError('官方验真连接或响应异常；' + detail) from error
        if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 200 or not isinstance(result.get('data'), dict):
            raise XheyError('官方验真业务响应异常，未归档')
        return result['data']

    def create(self, image):
        data = self.request('create', {'photoBase64List': [base64.b64encode(image).decode('ascii')]})
        rows = data.get('data')
        if type(data.get('status')) is not int or data['status'] != 0 or not isinstance(rows, list) or len(rows) != 1:
            raise XheyError('官方任务未确认创建；请核对后手动重试')
        task = rows[0].get('taskID') if isinstance(rows[0], dict) else None
        if not isinstance(task, str) or not task or len(task) > 256:
            raise XheyError('官方响应缺少任务编号；请核对后手动重试')
        return task

    def query(self, task):
        return parse_verdict(self.request('query', {'taskID': task}))

    def report(self, task):
        data = self.request('verify_report', {'taskID': task, 'timeout': 30}, timeout=40)
        url = data.get('ossUrl', '')
        parts = urlsplit(url) if isinstance(url, str) else urlsplit('')
        if (data.get('taskID') != task or type(data.get('status')) is not int or data['status'] != 0
                or parts.scheme != 'https' or parts.hostname != 'net-cloud.xhey.top'
                or parts.username or parts.password or parts.port not in (None, 443)
                or not parts.path.startswith('/authVerify/reports/')):
            raise XheyError('官方报告尚未就绪或地址不匹配')
        return {'taskID': task, 'url': url, 'timestamp': data.get('timestamp')}
