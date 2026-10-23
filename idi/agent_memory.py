"""Source-backed, scoped notes. Models can propose; people confirm and correct."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .context_memory import ConversationContext, personal_identity, signature, token_estimate
from .db import dump, now, uid
from .domain import DomainError, WORKFLOW_STAGES

MEMORY_BUDGET = 3200
CASE_SOURCE_FIELDS = ('id', 'project_id', 'platform_report_code', 'report_number', 'short_code',
    'title', 'building', 'unit', 'room', 'workflow_stage', 'workflow_source', 'workflow_verified_at')
KINDS = {'preference': '表达偏好', 'note': '事项约定', 'todo': '待办', 'fact': '台账快照'}
STATES = {'candidate': '待确认', 'active': '使用中', 'stale': '待复核', 'deleted': '已删除'}


def expiry(days):
    if type(days) is not int or days not in (0, 7, 30, 90, 365):
        raise DomainError('记忆有效期请选择7、30、90、365天或长期')
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec='milliseconds') if days else None


class AgentMemory:
    def __init__(self, db, domain):
        self.db, self.domain = db, domain
        self.context = ConversationContext(db)

    def case_signature(self, case):
        return signature({k: case.get(k) for k in CASE_SOURCE_FIELDS})

    def _source_signature(self, project_id=None, case_id=None):
        if case_id:
            return self.case_signature(self.domain.case(case_id))
        if project_id:
            row = self.db.one('SELECT id,name,aliases,insurer FROM projects WHERE id=?', (project_id,))
            if not row:
                raise DomainError('项目不存在', 404)
            return signature(row)
        return ''

    def _reason(self, row):
        if row['expires_at'] and row['expires_at'] <= now():
            return '超过设定有效期，请重新核对'
        if row['scope'] == 'member':
            binding = self.db.one('SELECT enabled,revision FROM feishu_agent_bindings WHERE connector_id=?', (row['connector_id'],))
            if not binding or not binding['enabled'] or binding['revision'] != row['binding_revision']:
                return '群设置已变化，请重新核对适用范围'
        if row['source_signature']:
            try:
                actual = self._source_signature(row['project_id'], row['case_id'])
            except DomainError:
                return '关联资料已移除'
            if actual != row['source_signature']:
                return '关联台账已变化，请重新核对'
        return ''

    def _fresh(self, row):
        reason = self._reason(row)
        if reason and row['state'] in ('active', 'candidate'):
            with self.db.tx() as con:
                con.execute("UPDATE agent_memories SET state='stale',revision=revision+1,updated_at=? WHERE id=? AND revision=? AND state IN ('active','candidate')", (now(), row['id'], row['revision']))
            row = self.db.one('SELECT * FROM agent_memories WHERE id=?', (row['id'],))
        return row, reason

    def _event(self, con, row, action, actor):
        con.execute('INSERT INTO agent_memory_events VALUES(?,?,?,?,?,?)', (uid(), row['id'], row['revision'], action, actor, now()))

    def _insert(self, data, actor):
        stamp = now()
        row = {'id': uid(), 'owner_key': '', 'connector_id': None, 'binding_revision': None, 'project_ids': '[]',
            'project_id': None, 'case_id': None, 'source_id': '', 'source_quote': '', 'source_signature': '',
            'origin_json': '{}',
            'expires_at': expiry(90), 'revision': 1, 'confirmed_at': None, 'source_time': stamp,
            'created_at': stamp, 'updated_at': stamp, **data}
        with self.db.tx() as con:
            columns = list(row)
            inserted = con.execute('INSERT OR IGNORE INTO agent_memories(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(row[k] for k in columns)).rowcount
            if inserted:
                self._event(con, row, 'create', actor)
            else:
                row = dict(con.execute('SELECT * FROM agent_memories WHERE dedupe_key=?', (row['dedupe_key'],)).fetchone())
        return row

    def observe_case(self, case):
        """Only deterministic facts from the live record are auto-confirmed."""
        fingerprint = self.case_signature(case)
        key = 'case_snapshot:' + case['id']
        previous = self.db.one('SELECT * FROM agent_memories WHERE dedupe_key=?', (key,))
        if previous and (previous['state'] == 'deleted' or previous['source_kind'] != 'case_snapshot'):
            return
        label = case.get('platform_report_code') or case.get('short_code') or case['title']
        content = '；'.join(str(v) for v in [case.get('project_name'), '案件：' + label,
            '报案号：' + case['report_number'] if case.get('report_number') else '',
            '位置：' + ' '.join(str(case.get(k) or '') for k in ('building', 'unit', 'room')).strip(),
            '台账阶段：' + WORKFLOW_STAGES.get(case['workflow_stage'], case['workflow_stage']),
            '最近核验：' + case['workflow_verified_at'] if case.get('workflow_verified_at') else '官网核验时间未登记'] if v)
        if previous:
            if previous['source_signature'] == fingerprint and previous['state'] == 'active' and not self._reason(previous):
                return
            with self.db.tx() as con:
                changed = con.execute("UPDATE agent_memories SET title=?,content=?,project_id=?,source_signature=?,state='active',source_time=?,confirmed_at=?,expires_at=?,revision=revision+1,updated_at=? WHERE id=? AND revision=? AND state!='deleted' AND source_kind='case_snapshot'",
                    ('案件台账 · ' + label, content, case['project_id'], fingerprint, now(), now(), expiry(30), now(), previous['id'], previous['revision'])).rowcount
                if changed:
                    self._event(con, {**previous, 'revision': previous['revision'] + 1}, 'refresh_source', 'system')
            return
        self._insert({'dedupe_key': key, 'scope': 'case', 'project_id': case['project_id'], 'case_id': case['id'],
            'kind': 'fact', 'title': '案件台账 · ' + label, 'content': content, 'state': 'active',
            'source_kind': 'case_snapshot', 'source_id': case['id'], 'source_signature': fingerprint,
            'confirmed_at': now(), 'expires_at': expiry(30)}, 'system')

    def propose(self, task, channel, projects, args, project_id=None, case_id=None):
        scope = args['scope']
        quote = args['quote'].strip()
        text = task['input'].get('text', '')
        if not 4 <= len(quote) <= 1200 or quote not in text:
            raise DomainError('记忆必须引用本次用户消息中的原话，不能根据模型推测新增')
        title = args['title'].strip()
        if not 1 <= len(title) <= 80:
            raise DomainError('请提供简短的记忆名称')
        if args['kind'] not in ('preference', 'note', 'todo'):
            raise DomainError('只能提出偏好、事项约定或待办；模型不能确认业务事实')
        if scope not in ('personal', 'project', 'case') or (scope != 'personal' and project_id not in projects) or (scope == 'case' and not case_id):
            raise DomainError('请明确当前授权范围内的项目或案件')
        if scope == 'personal' and (project_id or case_id):
            raise DomainError('个人偏好不能附带其他项目或案件')
        if scope == 'project' and case_id:
            raise DomainError('案件约定请使用案件范围')
        if scope == 'personal' and args['kind'] != 'preference':
            raise DomainError('项目约定和待办需要指定项目或案件')
        if self.db.one('SELECT COUNT(*) n FROM agent_memories WHERE source_kind=? AND source_id=?', (channel, task['id']))['n'] >= 4:
            raise DomainError('本次最多提出4条记忆，请先在网页核对')
        owner = personal_identity(task, channel)
        stored_scope = ('browser' if channel == 'workspace' else 'member') if scope == 'personal' else scope
        row = self._insert({'dedupe_key': signature([channel, task['id'], stored_scope, project_id, case_id, quote]),
            'scope': stored_scope, 'owner_key': owner if scope == 'personal' else '',
            'connector_id': task.get('connector_id'), 'binding_revision': task.get('binding_revision'),
            'project_ids': dump(projects), 'project_id': project_id, 'case_id': case_id, 'kind': args['kind'],
            'title': title, 'content': quote, 'state': 'candidate', 'source_kind': channel,
            'source_id': task['id'], 'source_quote': quote, 'source_time': task['created_at'],
            'source_signature': self._source_signature(project_id, case_id)}, channel)
        return {'memory_id': row['id'], 'state': row['state'],
            'instruction': '已进入待确认记忆，请在工作台的 AI 记忆中核对后启用；不能当作已核验事实。' if row['state'] != 'deleted' else '该条记忆已删除，本次没有重新加入。'}

    def _select(self, owner, projects, *, member=None, manage=False, case_id=None):
        conditions, values = ["(scope='browser' AND owner_key=?)"], [owner]
        if projects:
            conditions.append("(scope IN ('project','case') AND project_id IN (" + ','.join('?' for _ in projects) + '))')
            values += projects
        if member:
            conditions.append("(scope='member' AND owner_key=?)")
            values.append(member)
        elif manage:
            conditions.append("scope='member'")
        rows = self.db.all("SELECT * FROM agent_memories WHERE state!='deleted' AND (" + ' OR '.join(conditions) + ') ORDER BY updated_at DESC,id DESC LIMIT 1001', values)
        selected = []
        for row in rows:
            if row['scope'] == 'case' and case_id and row['case_id'] != case_id:
                continue
            if row['scope'] == 'member' and not set(json.loads(row['project_ids'])) <= set(projects):
                continue
            selected.append(row)
        return selected

    def public(self, row, reason=''):
        result = {k: row[k] for k in ('id', 'scope', 'project_id', 'case_id', 'kind', 'title', 'content', 'state',
            'source_kind', 'source_id', 'source_quote', 'source_time', 'expires_at', 'revision', 'confirmed_at', 'created_at', 'updated_at')}
        project = self.db.one('SELECT name FROM projects WHERE id=?', (row['project_id'],)) if row['project_id'] else None
        connector = self.db.one('SELECT name FROM connectors WHERE id=?', (row['connector_id'],)) if row['connector_id'] else None
        case = self.db.one('SELECT title,short_code,platform_report_code FROM cases WHERE id=?', (row['case_id'],)) if row['case_id'] else None
        result.update(project_name=(project or {}).get('name'), case_label=(case or {}).get('platform_report_code') or (case or {}).get('short_code') or (case or {}).get('title'),
            group_name=(connector or {}).get('name'), stale_reason=reason,
            scope_label={'browser': '仅此浏览器', 'member': ((connector or {}).get('name') or '原群') + ' · 原发言人',
                'project': (project or {}).get('name') or '项目', 'case': (case or {}).get('platform_report_code') or (case or {}).get('short_code') or '案件'}[row['scope']])
        result['origin'] = json.loads(row['origin_json'])
        return result

    def for_prompt(self, task, channel, projects, query='', case_id=None, explicit=False):
        owner = personal_identity(task, channel)
        rows = self._select(owner if channel == 'workspace' else '', projects, member=owner if channel == 'feishu' else None,
            case_id=case_id if explicit else None)
        parts = re.findall(r'[A-Za-z0-9-]{3,}|[\u4e00-\u9fff]+', query)
        terms = set(p.lower() for p in parts if len(p) < 40)
        terms.update(p[i:i + 2] for p in parts for i in range(min(len(p) - 1, 60)) if re.search(r'[\u4e00-\u9fff]', p))
        ranked = []
        for item in rows:
            row, reason = self._fresh(item)
            if row['state'] != 'active' or reason:
                continue
            public = self.public(row)
            haystack = ' '.join(str(public.get(k) or '') for k in ('title', 'content', 'project_name', 'case_label')).lower()
            score = sum(min(10, len(t)) for t in terms if t in haystack)
            if row['scope'] in ('browser', 'member'):
                score += 60
            elif row['case_id'] == case_id and case_id:
                score += 80
            elif row['scope'] == 'project' and len(projects) == 1:
                score += 30
            elif not score and not explicit:
                continue
            ranked.append((score, row, public))
        ranked.sort(key=lambda x: (x[0], x[1]['updated_at']), reverse=True)
        selected, used = [], 0
        guards = task.setdefault('_memory_guard', {})
        for _, row, public in ranked[:20]:
            content = {k: public[k] for k in ('id', 'kind', 'title', 'content', 'scope_label', 'source_kind', 'source_time', 'confirmed_at', 'expires_at')}
            cost = token_estimate(content)
            if used + cost > MEMORY_BUDGET:
                continue
            selected.append(content)
            used += cost
            guards[row['id']] = row['revision']
        return {'items': selected, 'scope': '仅当前范围内已确认且仍有效的记忆；用于参考，不能替代最新台账、官网核验或权限判断。',
            'estimated_tokens': used, 'omitted_items': max(0, len(ranked) - len(selected))}

    def check_context(self, task):
        self.context.check(task)
        for memory_id, revision in task.get('_memory_guard', {}).items():
            row = self.db.one('SELECT * FROM agent_memories WHERE id=?', (memory_id,))
            if not row:
                raise DomainError('参考记忆已更改，请重新发送问题', 409)
            row, reason = self._fresh(row)
            if row['state'] != 'active' or row['revision'] != revision or reason:
                raise DomainError('参考记忆或来源资料已更改，请重新发送问题', 409)

    def view_scope(self, owner, thread_id=None, project_id=None, case_id=None):
        from .identity import require_project
        from .workspace_scope import check_thread_scope
        if thread_id:
            thread = self.db.one('SELECT * FROM workspace_chat_threads WHERE id=? AND owner_hash=?', (thread_id, owner))
            if not thread:
                raise DomainError('对话不存在或不属于当前浏览器', 404)
            check_thread_scope(self.db, thread)
            if (project_id and project_id != thread['project_id']) or (case_id and case_id != thread['case_id']):
                raise DomainError('记忆范围与当前对话不一致', 409)
            return json.loads(thread['project_ids']), thread['case_id'], thread
        if case_id:
            case = self.domain.case(case_id)
            if project_id and project_id != case['project_id']:
                raise DomainError('案件不属于所选项目')
            project_id = case['project_id']
        if project_id:
            require_project(project_id)
        projects = [project_id] if project_id else [p['id'] for p in self.domain.projects()]
        if any(not self.db.one('SELECT id FROM projects WHERE id=?', (p,)) for p in projects):
            raise DomainError('项目不存在', 404)
        return projects, case_id, None

    def list(self, owner, *, thread_id=None, project_id=None, case_id=None, query=''):
        projects, case_id, thread = self.view_scope(owner, thread_id, project_id, case_id)
        rows = self._select(owner, projects, manage=True, case_id=case_id)
        items = []
        for item in rows:
            row, reason = self._fresh(item)
            value = self.public(row, reason)
            if query and query.casefold() not in dump(value).casefold():
                continue
            items.append(value)
        summary = self.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=? AND owner_key=?', ('workspace:' + thread_id, owner)) if thread else None
        choices = [p for p in self.domain.projects() if p['id'] in projects]
        cases = [{'id': c['id'], 'project_id': c['project_id'], 'label': c.get('platform_report_code') or c.get('short_code') or c['title']}
            for c in self.domain.cases() if c['project_id'] in projects and (not case_id or c['id'] == case_id)]
        return {'items': items[:200], 'total': len(items), 'truncated': len(items) > 200 or len(rows) > 1000,
            'summary': self.context.public(summary), 'projects': [{'id': p['id'], 'name': p['name']} for p in choices], 'cases': cases[:300],
            'summary_options': self.summary_options(owner, projects, case_id, thread_id),
            'scope': '个人偏好仅属于此浏览器；群内偏好仅供原群发言人使用；项目与案件记忆供授权渠道共同参考。'}

    def summary_options(self, owner, projects, case_id, thread_id):
        options = []
        if thread_id:
            row = self.db.one('SELECT * FROM agent_context_summaries WHERE thread_id=? AND owner_key=?', (thread_id, owner))
            if row:
                options.append({'key': row['scope_key'], 'label': '当前网页对话', 'channel': 'workspace'})
        # A multi-project group summary is available for management only when the
        # selected workbench range contains every project in that group.
        if not case_id:
            rows = self.db.all("SELECT s.*,c.name FROM agent_context_summaries s JOIN connectors c ON c.id=s.connector_id JOIN feishu_agent_bindings b ON b.connector_id=s.connector_id WHERE s.channel='feishu' AND c.enabled=1 AND b.enabled=1 AND s.binding_revision=b.revision ORDER BY s.updated_at DESC LIMIT 100")
            for row in rows:
                if not set(json.loads(row['project_ids'])) <= set(projects):
                    continue
                if row['conversation_key']:
                    latest = self.db.one("SELECT input_json FROM feishu_agent_tasks WHERE connector_id=? AND chat_id=? AND conversation_key=? AND binding_revision=? AND kind IN ('chat','images') ORDER BY rowid DESC LIMIT 1",
                        (row['connector_id'], row['chat_id'], row['conversation_key'], row['binding_revision']))
                else:
                    latest = self.db.one("SELECT input_json FROM feishu_agent_tasks WHERE connector_id=? AND sender_id=? AND conversation_key='' AND binding_revision=? AND kind IN ('chat','images') ORDER BY rowid DESC LIMIT 1",
                        (row['connector_id'], row['sender_id'], row['binding_revision']))
                question = json.loads(latest['input_json']).get('text', '') if latest else ''
                kind = '共享话题 · ' if row['conversation_key'].startswith('topic:') else '独立消息 · ' if row['conversation_key'] else ''
                options.append({'key': row['scope_key'], 'label': row['name'] + ' · ' + kind + (question.replace('\n', ' ')[:28] or '群成员对话'), 'channel': 'feishu'})
        return options

    def summary(self, owner, key, **scope):
        projects, case_id, thread = self.view_scope(owner, **scope)
        choices = self.summary_options(owner, projects, case_id, thread['id'] if thread else None)
        if not any(item['key'] == key for item in choices):
            raise DomainError('摘要不存在或不在当前范围', 404)
        return self.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=?', (key,))

    def managed_summary(self, owner, key, data=None, **scope):
        row = self.summary(owner, key, **scope)
        return self.context.public(row) if data is None else self.context.edit(row, data)

    def _accessible(self, memory_id, owner, projects, case_id):
        row = self.db.one('SELECT * FROM agent_memories WHERE id=?', (memory_id,))
        allowed = bool(row and ((row['scope'] == 'browser' and row['owner_key'] == owner)
            or (row['scope'] in ('project', 'case') and row['project_id'] in projects and (row['scope'] != 'case' or not case_id or row['case_id'] == case_id))
            or (row['scope'] == 'member' and set(json.loads(row['project_ids'])) <= set(projects))))
        if not allowed:
            raise DomainError('记忆不存在或不在当前范围', 404)
        return row

    def create(self, owner, data, **scope):
        if not isinstance(data, dict) or set(data) - {'scope', 'project_id', 'case_id', 'kind', 'title', 'content', 'expires_days', 'request_id'}:
            raise DomainError('记忆参数无效')
        projects, view_case, _ = self.view_scope(owner, **scope)
        request_id = data.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{20,80}', request_id):
            raise DomainError('记忆请求标识无效')
        title, content = self._text(data)
        target = data.get('scope')
        project_id, case_id = data.get('project_id') or None, data.get('case_id') or None
        if any(value is not None and (not isinstance(value, str) or len(value) > 100) for value in (project_id, case_id)):
            raise DomainError('项目和案件标识无效')
        if target == 'browser':
            if project_id or case_id or data['kind'] != 'preference':
                raise DomainError('此浏览器范围仅用于个人偏好')
        elif target in ('project', 'case'):
            if target == 'case':
                case = self.domain.case(case_id)
                if project_id and project_id != case['project_id']:
                    raise DomainError('案件与项目不一致')
                project_id = case['project_id']
                if view_case and view_case != case_id:
                    raise DomainError('记忆不在当前案件范围')
            elif case_id:
                raise DomainError('项目记忆不能附带案件')
            if project_id not in projects:
                raise DomainError('记忆不在当前项目范围')
        else:
            raise DomainError('请选择此浏览器、项目或案件范围')
        result = self._insert({'dedupe_key': 'manual:' + owner + ':' + request_id, 'scope': target,
            'owner_key': owner if target == 'browser' else '', 'project_id': project_id, 'case_id': case_id,
            'kind': data['kind'], 'title': title, 'content': content, 'state': 'active', 'source_kind': 'manual',
            'source_quote': content, 'source_signature': self._source_signature(project_id, case_id),
            'confirmed_at': now(), 'expires_at': expiry(data.get('expires_days', 90))}, owner)
        if any(result[k] != v for k, v in {'scope': target, 'project_id': project_id, 'case_id': case_id, 'title': title, 'content': content, 'kind': data['kind']}.items()):
            raise DomainError('该请求标识已用于另一条记忆', 409)
        return self.public(result)

    @staticmethod
    def _text(data):
        title, content = data.get('title'), data.get('content')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            raise DomainError('记忆名称需要1至80字')
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 1200 or '\x00' in content:
            raise DomainError('记忆内容需要1至1200字')
        if data.get('kind') not in ('preference', 'note', 'todo'):
            raise DomainError('请选择偏好、事项约定或待办；手工内容不会作为台账快照')
        return title.strip(), content.strip()

    def update(self, owner, memory_id, data, **scope):
        if not isinstance(data, dict) or set(data) - {'revision', 'title', 'content', 'kind', 'expires_days', 'action'} or type(data.get('revision')) is not int:
            raise DomainError('记忆修改参数无效')
        projects, case_id, _ = self.view_scope(owner, **scope)
        row = self._accessible(memory_id, owner, projects, case_id)
        if row['state'] == 'deleted':
            raise DomainError('该条记忆已删除', 409)
        action = data.get('action', 'edit')
        if action not in ('edit', 'confirm', 'delete'):
            raise DomainError('记忆操作无效')
        updated = {**row, 'revision': row['revision'] + 1, 'updated_at': now()}
        if action == 'delete':
            updated.update(state='deleted', content='', source_quote='', origin_json='{}', title='已删除的记忆', confirmed_at=None)
        else:
            if action == 'edit':
                title, content = self._text(data)
                if row['scope'] in ('browser', 'member') and data['kind'] != 'preference':
                    raise DomainError('个人范围仅用于表达偏好')
                updated.update(title=title, content=content, kind=data['kind'], source_kind='manual', source_quote=content, source_time=now())
                if not json.loads(row['origin_json']):
                    updated['origin_json'] = dump({'kind': row['source_kind'], 'id': row['source_id'], 'quote': row['source_quote'] or row['content'], 'time': row['source_time']})
            updated.update(state='active', confirmed_at=now(), expires_at=expiry(data.get('expires_days', 90)),
                source_signature=self._source_signature(row['project_id'], row['case_id']))
            if row['scope'] == 'member':
                binding = self.db.one('SELECT * FROM feishu_agent_bindings WHERE connector_id=?', (row['connector_id'],))
                if not binding or not binding['enabled'] or not set(json.loads(binding['project_ids'])) <= set(projects):
                    raise DomainError('群未开启或范围已改变，不能启用该偏好', 409)
                updated.update(binding_revision=binding['revision'], project_ids=binding['project_ids'])
            if row['source_kind'] == 'case_snapshot' and action == 'confirm':
                raise DomainError('台账快照请重新查询案件刷新；也可纠正为人工备注')
        with self.db.tx() as con:
            fields = [k for k in updated if k != 'id']
            changed = con.execute('UPDATE agent_memories SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=? AND revision=? AND state!=\'deleted\'',
                tuple(updated[k] for k in fields) + (memory_id, data['revision'])).rowcount
            if not changed:
                raise DomainError('记忆已被其他操作修改，请刷新后再试', 409)
            self._event(con, updated, action, owner)
        return self.public(updated)

    def edit_summary(self, owner, thread_id, data):
        self.view_scope(owner, thread_id)
        row = self.db.one('SELECT * FROM agent_context_summaries WHERE scope_key=? AND owner_key=?', ('workspace:' + thread_id, owner))
        if not row:
            raise DomainError('当前对话尚未生成摘要', 404)
        return self.context.edit(row, data)

    def export(self, owner, **scope):
        result = self.list(owner, **scope)
        lines = ['# IDI AI 记忆', '', '导出时间：' + now(), '', result['scope'], '']
        for row in result['items']:
            lines += ['## ' + row['title'].replace('\n', ' '), '',
                '范围：' + row['scope_label'] + '；状态：' + STATES[row['state']] + '；类别：' + KINDS[row['kind']], '',
                row['content'], '', '来源：' + row['source_kind'] + '；来源时间：' + row['source_time'],
                '有效期至：' + (row['expires_at'] or '长期'), '']
            if row['source_quote']:
                lines += ['原话：' + row['source_quote'], '']
        for option in result['summary_options']:
            summary = self.managed_summary(owner, option['key'], **scope)
            lines += ['## 对话摘要 · ' + option['label'], '', '摘要仅供原会话接续，不能替代核验。', '']
            for section, label in (('focus', '关注事项'), ('agreements', '对话约定'), ('pending', '未决事项')):
                for item in summary['sections'].get(section, []):
                    lines += ['- ' + label + '：' + item['text'] + '（来源：' + '、'.join(item['sources']) + '）']
        if result['truncated']:
            lines += ['', '本次仅导出当前范围前200条，请缩小项目范围后继续导出。']
        return '\n'.join(lines) + '\n'
