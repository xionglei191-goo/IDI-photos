"""Explicit project-contact feedback. Capture itself remains read-only."""
from __future__ import annotations

import re

import httpx

from ..domain import DomainError
from .feishu import FeishuClient, TOKEN_ERRORS, api_error


class UncertainDelivery(DomainError):
    pass


class FeishuFeedbackClient(FeishuClient):
    def members(self, chat_id):
        members, token, seen = [], '', set()
        while True:
            params = {'member_id_type': 'open_id', 'page_size': 100}
            if token:
                params['page_token'] = token
            data = self.get(f'im/v1/chats/{chat_id}/members', params)
            members.extend({'open_id': v['member_id'], 'name': v.get('name') or v['member_id']}
                           for v in data.get('items', []) if re.fullmatch(r'ou_[a-zA-Z0-9]+', v.get('member_id', '')))
            if not data.get('has_more'):
                return members
            token = data.get('page_token')
            if not token or token in seen:
                raise DomainError('群成员分页异常，请重新读取后选择负责人')
            seen.add(token)

    def send_feedback(self, chat_id, content, delivery_id):
        for attempt in range(2):
            # Authentication failures happen before the send and are safely retryable.
            token = self.authenticate()
            try:
                response = self.http.post('im/v1/messages', params={'receive_id_type': 'chat_id'},
                                          headers={'Authorization': 'Bearer ' + token},
                                          json={'receive_id': chat_id, 'msg_type': 'text', 'content': content, 'uuid': delivery_id})
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError):
                raise UncertainDelivery('发送结果不确定，请先在群内核对；系统不会自动重复发送') from None
            if data.get('code') in TOKEN_ERRORS and attempt == 0:
                self.expires = 0
                continue
            if data.get('code') != 0:
                raise DomainError(api_error(data.get('code', 'unknown')))
            mid = data.get('data', {}).get('message_id')
            if not mid:
                raise UncertainDelivery('飞书未返回消息编号，请先在群内核对发送结果')
            return mid
        raise DomainError('飞书登录令牌刷新失败')
