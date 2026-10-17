"""Read-only group-session views over existing Feishu task records.

Display sessions collect a group's turns; model/member/topic memory identities
and delivery state machines remain owned by the original task services.
"""
from __future__ import annotations

from .context_memory import signature
from .domain import DomainError


class FeishuHistory:
    def __init__(self, agent):
        self.agent, self.db = agent, agent.db

    def _groups(self, query=''):
        rows = self.db.all("""SELECT t.connector_id,t.chat_id,
            CASE WHEN t.kind='brief' THEN 'brief' ELSE 'group' END session_kind,
            CASE WHEN t.kind='brief' THEN COALESCE(t.project_id,'') ELSE '' END session_project,
            COUNT(*) turn_count,COUNT(DISTINCT NULLIF(t.sender_id,'')) member_count,
            SUM(CASE WHEN t.state IN ('partial','failed','missed') OR o.state IN ('failed','unknown') THEN 1 ELSE 0 END) attention_count,
            SUM(CASE WHEN t.state IN ('queued','running') OR o.state IN ('queued','sending') THEN 1 ELSE 0 END) active_count,
            SUM(CASE WHEN json_array_length(COALESCE(json_extract(t.input_json,'$.photo_ids'),'[]'))>0 THEN 1 ELSE 0 END) image_turns,
            MIN(t.created_at) first_at,MAX(t.rowid) latest_sequence,
            MAX(CASE WHEN ?='' OR instr(lower(COALESCE(json_extract(t.input_json,'$.text'),'') || ' ' ||
                COALESCE(json_extract(t.result_json,'$.text'),'') || ' ' || c.name || ' ' ||
                COALESCE(json_extract(c.config,'$.chat_name'),'') || ' ' || COALESCE(p.name,'')),lower(?))>0 THEN 1 ELSE 0 END) matched
            FROM feishu_agent_tasks t JOIN connectors c ON c.id=t.connector_id
            LEFT JOIN projects p ON p.id=t.project_id
            LEFT JOIN feishu_agent_outbox o ON o.task_id=t.id
            GROUP BY t.connector_id,t.chat_id,session_kind,session_project
            ORDER BY latest_sequence DESC""", (query.strip(), query.strip()))
        for row in rows:
            row['id'] = 'fs_' + signature([row['connector_id'], row['chat_id'], row['session_kind'], row['session_project']])[:24]
        return rows

    @staticmethod
    def _summary(group, latest):
        return {**{k: v for k, v in group.items() if k not in ('matched', 'session_project')},
            'project_id': group['session_project'] or None,
            'title': latest['chat_name'] or latest['connector_name'],
            'subtitle': latest['project_name'] + ' · 每日简报' if group['session_kind'] == 'brief' else '群内问答与图片处理',
            'last_at': latest['created_at'], 'latest': latest}

    def sessions(self, *, query='', kind='group', attention=False, offset=0, limit=20):
        groups = [row for row in self._groups(query) if row['matched'] and (not attention or row['attention_count'])]
        counts = {key: sum(row['session_kind'] == key for row in groups) for key in ('group', 'brief')}
        selected = [row for row in groups if kind == 'all' or row['session_kind'] == kind]
        page = selected[offset:offset + limit]
        latest = self.agent.record_rows('t.rowid IN (' + ','.join('?' for _ in page) + ')',
            tuple(row['latest_sequence'] for row in page), limit=len(page)) if page else []
        by_sequence = {row['sequence']: row for row in latest}
        return {'sessions': [self._summary(row, by_sequence[row['latest_sequence']]) for row in page],
            'total': len(selected), 'counts': counts, 'offset': offset, 'limit': limit,
            'has_more': offset + len(page) < len(selected)}

    def transcript(self, session_id, *, before=0, limit=30):
        group = next((row for row in self._groups() if row['id'] == session_id), None)
        if not group:
            raise DomainError('群会话不存在或已无可用记录', 404)
        where = 't.connector_id=? AND t.chat_id=?'
        args = [group['connector_id'], group['chat_id']]
        if group['session_kind'] == 'brief':
            where += " AND t.kind='brief' AND COALESCE(t.project_id,'')=?"
            args.append(group['session_project'])
        else:
            where += " AND t.kind IN ('chat','images')"
        if before:
            where += ' AND t.rowid<?'
            args.append(before)
        rows = self.agent.record_rows(where, tuple(args), limit=limit + 1, full=True)
        has_more = len(rows) > limit
        rows = rows[:limit]
        # One batch lookup for all visible image references, including images
        # that are still queued for analysis. Never call get() for every turn.
        photo_refs = {row['id']: row.pop('photo_ids') for row in rows}
        photo_ids = {photo for photos in photo_refs.values() for photo in photos}
        available = {row['id'] for row in self.db.all('SELECT id FROM photos WHERE id IN (' + ','.join('?' for _ in photo_ids) + ')',
            tuple(photo_ids))} if photo_ids else set()
        for row in rows:
            row['photos'] = [{'id': photo} for photo in dict.fromkeys(photo_refs[row['id']]) if photo in available]
        latest = self.agent.record_rows('t.rowid=?', (group['latest_sequence'],), limit=1)[0]
        return {'session': self._summary(group, latest), 'turns': list(reversed(rows)),
            'has_more': has_more, 'before': min((row['sequence'] for row in rows), default=before),
            'latest_sequence': group['latest_sequence']}
