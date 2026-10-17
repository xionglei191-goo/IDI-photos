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
        return self._send('im/v1/messages', {'receive_id_type': 'chat_id'},
            {'receive_id': chat_id, 'msg_type': 'text', 'content': content, 'uuid': delivery_id})['message_id']

    def reply_feedback(self, chat_id, source_message_id, content, delivery_id, *, in_thread=False):
        if not isinstance(source_message_id, str) or not re.fullmatch(r'om_[A-Za-z0-9_-]{1,180}', source_message_id):
            raise DomainError('原消息标识无效，未发送', 409)
        # Reply APIs address a message, not a chat: verify the destination before
        # the write so a stale/corrupt reference can never redirect a group reply.
        message = next((item for item in self.get('im/v1/messages/' + source_message_id).get('items', [])
            if item.get('message_id') == source_message_id), None)
        if not message or message.get('chat_id') != chat_id or message.get('deleted'):
            raise DomainError('原消息已移除或不在当前群，未发送', 409)
        return self._send('im/v1/messages/' + source_message_id + '/reply', None,
            {'msg_type': 'text', 'content': content, 'uuid': delivery_id, 'reply_in_thread': bool(in_thread)})

    def _send(self, path, params, body):
        for attempt in range(2):
            # Authentication failures happen before the send and are safely retryable.
            token = self.authenticate()
            try:
                response = self.http.post(path, params=params,
                                          headers={'Authorization': 'Bearer ' + token},
                                          json=body)
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
            return data['data']
        raise DomainError('飞书登录令牌刷新失败')
