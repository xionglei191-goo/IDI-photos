"""Explicit reply trees, separate from each speaker's private memory identity."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .connectors.feishu import content_parts
from .context_memory import signature
from .db import now, uid


def reference(value, prefix='om_'):
    return value if isinstance(value, str) and re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_-]{1,180}', value) else ''


def speaker(payload):
    sender = payload.get('sender') or {}
    return (sender.get('sender_id') or {}).get('open_id') or (sender.get('id') if sender.get('id_type') in (None, 'open_id') else '') or ''


def speaker_tag(sender):
    return '成员' + signature(sender)[:8]


class FeishuTopics:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _scope(binding):
        return binding['connector_id'], binding['chat_id'], binding.get('binding_revision', binding.get('revision'))

    def _local(self, binding, message_id):
        row = self.db.one("SELECT payload FROM feishu_inbox WHERE connector_id=? AND message_id=? AND state='completed' AND received_at>=?",
            (binding['connector_id'], message_id, binding['activated_at']))
        if not row:
            return None
        payload = json.loads(row['payload'])
        try:
            sent = datetime.fromtimestamp(int(payload.get('create_time') or 0) / 1000, timezone.utc)
            active = datetime.fromisoformat(binding['activated_at'])
        except (TypeError, ValueError, OverflowError):
            return None
        if (payload.get('chat_id') != binding['chat_id'] or payload.get('deleted') or sent < active
            or payload.get('sender', {}).get('sender_type') != 'user'):
            return None
        return payload

    def _sent_task(self, binding, message_id):
        return self.db.one("SELECT t.* FROM feishu_agent_outbox o JOIN feishu_agent_tasks t ON t.id=o.task_id WHERE o.remote_message_id=? AND o.state='sent' AND t.connector_id=? AND t.chat_id=? AND t.binding_revision=? AND t.kind IN ('chat','images')",
            (message_id, *self._scope(binding)))

    def capture(self, binding, payload):
        """Index explicit topic messages, including non-triggering participants.

        Only locally captured, post-activation ancestors are copied. No previous
        member summary or unrelated private conversation is promoted into a topic.
        """
        message_id = reference(payload.get('message_id'))
        root = reference(payload.get('root_id'))
        parent = reference(payload.get('parent_id'))
        thread = reference(payload.get('thread_id'), 'omt_')
        if any(payload.get(name) and not value for name, value in (('root_id', root), ('parent_id', parent), ('thread_id', thread))):
            return {'mode': 'isolated', 'key': 'isolated:' + signature([*self._scope(binding), message_id]),
                'root_id': root, 'parent_id': parent, 'thread_id': thread,
                'notice': '回复标识无效，本次仅使用当前消息，请核对回复位置。'}
        if not (root or parent or thread):
            return {'mode': 'member', 'key': '', 'root_id': '', 'parent_id': '', 'thread_id': ''}
        refs = {'message:' + message_id}
        if thread:
            refs.add('thread:' + thread)
        ancestors, seen, sent_keys = {}, {message_id}, set()
        pending = [value for value in (root, parent) if value and value != message_id]
        missing = set()
        # Direct reply metadata and verified local ancestry are the only links.
        for _ in range(12):
            if not pending:
                break
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            refs.add('message:' + current)
            source = self._local(binding, current)
            sent = self._sent_task(binding, current) if not source else None
            if sent:
                if sent['conversation_key']:
                    sent_keys.add(sent['conversation_key'])
                source_id = reference(sent['source_message_id'])
                if source_id:
                    pending.append(source_id)
            elif source:
                ancestors[current] = source
                source_thread = reference(source.get('thread_id'), 'omt_')
                if source_thread:
                    refs.add('thread:' + source_thread)
                pending.extend(value for value in (reference(source.get('root_id')), reference(source.get('parent_id'))) if value and value not in seen)
            else:
                missing.add(current)
        if pending:
            missing.update(pending)
        scope = self._scope(binding)
        existing = self.db.all('SELECT topic_key FROM feishu_topic_aliases WHERE connector_id=? AND chat_id=? AND binding_revision=? AND reference IN (' + ','.join('?' for _ in refs) + ')', (*scope, *sorted(refs)))
        keys = {row['topic_key'] for row in existing} | sent_keys
        if len(keys) > 1:
            # Conflicting established trees are never silently combined.
            return {'mode': 'isolated', 'key': 'isolated:' + signature([*scope, message_id]),
                'root_id': root, 'parent_id': parent, 'thread_id': thread,
                'notice': '回复标识对应不同话题，本次仅使用当前消息，请核对回复位置。'}
        key = next(iter(keys), '') or 'topic:' + signature([*scope, thread or root or parent])
        with self.db.tx() as con:
            for ref in sorted(refs):
                con.execute('INSERT OR IGNORE INTO feishu_topic_aliases VALUES(?,?,?,?,?,?)', (*scope, ref, key, now()))
            # Seed only the explicitly referenced root/parents, not that author's
            # other conversations. Insert ancestors first to preserve the cutoff.
            sources = sorted(ancestors.values(), key=lambda value: int(value.get('create_time') or 0)) + [payload]
            for source in sources:
                mid = reference(source.get('message_id'))
                if not mid or not speaker(source):
                    continue
                text, resources = content_parts(source)
                if not text and resources:
                    text = f'上传了 {len(resources)} 项图片或附件；具体内容需读取原件核对。'
                instant = datetime.fromtimestamp(int(source['create_time']) / 1000, timezone.utc).isoformat(timespec='milliseconds')
                con.execute('INSERT OR IGNORE INTO feishu_topic_messages VALUES(?,?,?,?,?,?,?,?,?)',
                    (uid(), *scope, key, mid, speaker(source), text[:6000], instant))
        return {'mode': 'topic', 'key': key, 'root_id': root, 'parent_id': parent, 'thread_id': thread,
            'notice': '部分被回复的原消息未在当前群设置生效后收录；本次仅使用已核对的话题记录。' if missing else ''}

    def record_reply(self, task, message):
        key = task.get('conversation_key')
        if not key or not key.startswith('topic:'):
            return
        refs = ['message:' + value for value in (reference(message.get('message_id')), reference(message.get('root_id'))) if value]
        thread = reference(message.get('thread_id'), 'omt_')
        if thread:
            refs.append('thread:' + thread)
        with self.db.tx() as con:
            for ref in refs:
                con.execute('INSERT OR IGNORE INTO feishu_topic_aliases VALUES(?,?,?,?,?,?)', (*self._scope(task), ref, key, now()))

    def pending_images(self, binding, conversation, sender, before_task_id=None):
        scope = self._scope(binding)
        cutoff = (datetime.now(timezone.utc).timestamp() - 1800)
        instant = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec='milliseconds')
        before = self.db.one('SELECT rowid seq FROM feishu_agent_tasks WHERE id=?', (before_task_id,)) if before_task_id else None
        limit = (before or {}).get('seq', 2**63 - 1)
        if conversation['mode'] == 'topic':
            return self.db.one("SELECT t.id,t.source_message_id,t.input_json FROM feishu_agent_tasks t JOIN feishu_topic_messages m ON m.source_message_id=t.source_message_id AND m.connector_id=t.connector_id AND m.chat_id=t.chat_id AND m.binding_revision=t.binding_revision WHERE t.connector_id=? AND t.chat_id=? AND t.binding_revision=? AND m.topic_key=? AND t.kind='images' AND t.state IN ('partial','completed') AND t.created_at>=? AND t.rowid<? ORDER BY t.rowid DESC LIMIT 1",
                (*scope, conversation['key'], instant, limit))
        if conversation['mode'] == 'member':
            return self.db.one("SELECT id,source_message_id,input_json FROM feishu_agent_tasks WHERE connector_id=? AND chat_id=? AND binding_revision=? AND sender_id=? AND conversation_key='' AND kind='images' AND state IN ('partial','completed') AND created_at>=? AND rowid<? ORDER BY rowid DESC LIMIT 1", (*scope, sender, instant, limit))
        return None
