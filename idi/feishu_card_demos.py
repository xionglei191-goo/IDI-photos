"""Opt-in synthetic Feishu sessions, stored apart from the business database.

Only an explicitly created session can enter this handler. It shares the card
renderer, but has no platform client, business review, or submission queue.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .db import dump, uid
from .domain import DomainError
from .identity import require_admin
from .platform_contract import STAGES, visible
from .review_card_content import build_card, editable_fields, field_options, paragraph, text


DICTIONARIES = {
    'part_engineering': [{'code': 'demo-wall', 'name': '墙面工程'}, {'code': 'demo-roof', 'name': '屋面工程'}],
    'defect_type': [{'code': 'demo-peeling', 'name': '表面起皮'}, {'code': 'demo-leak', 'name': '渗漏'}],
    'estimate_restoration_cost': [{'code': 'demo-small', 'name': '小额维修（示例）'},
                                {'code': 'demo-medium', 'name': '中额维修（示例）'}],
}
BEIJING = timezone(timedelta(hours=8))


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def demo_plan(session):
    plan = {**deepcopy(STAGES['survey']), 'stage': 'survey', 'issues': [], 'field_sources': {},
            'case': {'project_name': '内测演示项目（模拟资料）', 'short_code': 'DEMO-' + session['id'][:8],
                     'platform_report_code': 'SIM-001', 'policy_number': 'SIM-POLICY-001',
                     'building': '1', 'unit': '1', 'room': '201', 'title': '客厅墙面起皮（模拟数据）'},
            'link': {'platform_report_code': 'SIM-001', 'insurer_report_number': 'SIM-CLAIM-001'},
            'personnel': [{'userId': 'demo-person', 'name': '演示维修员'},
                          {'userId': 'demo-person-2', 'name': '演示维修员二'}]}
    plan['values'] = {**session['original'], **session['values']}
    for field in plan['fields']:
        if field.get('name_key'):
            plan['values'][field['name_key']] = next((o['name'] for o in field_options(field, plan, DICTIONARIES)
                if str(o['code']) == plan['values'].get(field['key'])), '')
        if (field.get('required') and field['kind'] not in ('files', 'readonly') and visible(field, plan['values'])
                and not plan['values'].get(field['key'], '').strip()):
            plan['issues'].append({'severity': 'error', 'field': field['key'], 'message': '请填写' + field['label']})
    plan['values']['repairPersonPhone'] = '演示联系方式' if plan['values'].get('repairUserId') else ''
    plan['file_fields'] = [{**f, 'existing': [], 'selected': [{'id': f'demo-{i+1}', 'original_name': f'demo-{i+1}.jpg'}]}
                           for i, f in enumerate(f for f in plan['fields'] if f['kind'] == 'files')]
    return plan


def render_demo(session):
    plan = demo_plan(session)
    mode = session['mode']
    view = {'mode': mode, 'page': 0, 'values': session['values'],
            'changed_fields': [k for k, v in session['values'].items() if session['original'].get(k) != v],
            'notice': {'ready': '可点击“补充修改信息”开始体验。', 'edit': '正在编辑模拟资料，填写后可预览或放弃修改。',
                       'preview': '这是修改后的模拟预览，可继续修改或恢复原资料。',
                       'result': '模拟确认完成。未保存真实案件资料，未提交官网。'}[mode]}
    if mode == 'result':
        view.update(status='模拟确认完成', can_refresh=True)
    images = [{'field_key': f['key'], 'label': f['label'] + ' · 模拟示意图', 'image_key': session['image_keys'][i]}
              for i, f in enumerate(plan['file_fields']) if i < len(session['image_keys'])]
    delivery = {'id': 'demo:' + session['id'], 'card_token': session['token'], 'open_id': session['open_id']}
    card = build_card(delivery, plan, view, DICTIONARIES, images)
    elements = card['body']['elements']
    if elements and elements[0].get('tag') == 'markdown' and '<at ' in elements[0].get('content', ''):
        elements.pop(0)
    elements.insert(0, paragraph('【可操作演示】全部字段和图片均为模拟资料。修改、预览和模拟确认只影响本演示，不会提交官网。'))
    elements[-1] = paragraph('演示有效期至 ' + datetime.fromtimestamp(session['expires_at'], BEIJING).strftime('%m月%d日 %H:%M')
                            + '（北京时间）。可反复修改、预览、取消和重新体验。')
    for item in walk(card):
        for behavior in item.get('behaviors', []):
            if behavior.get('type') != 'callback':
                continue
            value = behavior['value']
            value['demo_session'] = session['id']
            if value['action'] == 'submit':
                value['action'] = 'simulate'
                item['text'] = text('模拟确认（不提交官网）')
                item['confirm'] = {'title': text('完成本次模拟体验'), 'text': text('只更新本演示的显示状态，不保存真实案件资料，不提交官网。')}
            elif value['action'] == 'refresh':
                item['text'] = text('重新体验' if mode == 'result' else '放弃修改，恢复原资料' if mode in ('edit', 'preview') else '重置演示')
    card['header']['title']['content'] = '【可操作演示】' + card['header']['title']['content']
    card['header']['template'] = 'orange'
    return card


class FeishuCardDemos:
    def __init__(self, followup, identities):
        self.followup, self.identities = followup, identities
        self.path = followup.settings.data_dir / 'demos' / 'feishu-cards.sqlite3'

    def connect(self):
        con = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        return con

    def create(self, connector_id, open_id, project_id, image_keys, *, hours=24):
        require_admin()
        source = self.followup.connectors.get(connector_id)
        if source['kind'] != 'feishu' or not source['enabled']:
            raise DomainError('演示群入口不可用', 409)
        config = source['config']
        person, binding = self.identities.resolve(config['app_id'], open_id, project_id)
        if len(image_keys) != 3 or not all(isinstance(k, str) and k.startswith('img_') for k in image_keys):
            raise DomainError('演示需要三张已上传的模拟图片')
        if not 0 < hours <= 168:
            raise DomainError('演示有效期必须在七天内')
        stamp = datetime.now(BEIJING)
        session = {'id': uid(), 'token': uid(), 'connector_id': connector_id, 'app_id': config['app_id'],
                   'chat_id': config['chat_id'], 'open_id': open_id, 'user_id': person.id,
                   'project_id': project_id, 'binding_revision': binding['revision'], 'message_id': '',
                   'mode': 'ready', 'values': {}, 'image_keys': image_keys,
                   'created_at': time.time(), 'expires_at': time.time() + hours * 3600,
                   'original': {'siteSurveyDate': stamp.strftime('%Y-%m-%d 10:00:00'),
                                'defectDesc': '客厅墙面局部起皮。拟清理基层后修补，原因以现场核对为准。',
                                'divisionWorksCode': 'demo-wall', 'defectiveSite': '客厅墙面',
                                'defectTypeCode': 'demo-peeling', 'repairMethod': '2',
                                'planRepairDate': (stamp + timedelta(days=1)).strftime('%Y-%m-%d'),
                                'repairUserId': 'demo-person', 'estimateRestorationCostCode': 'demo-small',
                                'zeroRejectReason': ''}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1)) as con, con:
            con.execute('CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS events(session_id TEXT, event_id TEXT, action TEXT, '
                        'created_at REAL, PRIMARY KEY(session_id,event_id))')
            con.execute('INSERT INTO sessions VALUES(?,?)', (session['id'], dump(session)))
        self.path.chmod(0o600)
        return session

    def get(self, session_id):
        if not self.path.exists():
            raise DomainError('演示已结束，请重新获取演示卡', 409)
        with closing(self.connect()) as con:
            row = con.execute('SELECT body FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            raise DomainError('演示已结束，请重新获取演示卡', 409)
        return json.loads(row['body'])

    def bind_message(self, session_id, message_id):
        require_admin()
        with closing(self.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            session = json.loads(con.execute('SELECT body FROM sessions WHERE id=?', (session_id,)).fetchone()['body'])
            if session['message_id'] and session['message_id'] != message_id:
                raise DomainError('本演示已经绑定另一条消息', 409)
            session['message_id'] = message_id
            con.execute('UPDATE sessions SET body=? WHERE id=?', (dump(session), session_id))

    def receive(self, connector_id, app_id, data):
        header, event = data['header'], data['event']
        action, context, operator = event['action'], event['context'], event['operator']
        value = action['value']
        if not self.path.exists():
            raise DomainError('演示已结束，请重新获取演示卡', 409)
        with closing(self.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT body FROM sessions WHERE id=?', (value['demo_session'],)).fetchone()
            if not row:
                raise DomainError('演示已结束，请重新获取演示卡', 409)
            session = json.loads(row['body'])
            source = self.followup.connectors.get(connector_id)
            original_source = self.followup.connectors.get(session['connector_id'])
            if (header.get('event_type') != 'card.action.trigger' or header.get('app_id') != app_id
                    or app_id != session['app_id'] or source['kind'] != 'feishu' or not source['enabled']
                    or source['config']['app_id'] != app_id or not original_source['enabled']
                    or original_source['config']['app_id'] != app_id or original_source['config']['chat_id'] != session['chat_id']
                    or event.get('host') != 'im_message' or context.get('open_chat_id') != session['chat_id']
                    or not session['message_id'] or context.get('open_message_id') != session['message_id']
                    or operator.get('open_id') != session['open_id'] or value.get('notification_id') != 'demo:' + session['id']
                    or (operator.get('tenant_key') and operator['tenant_key'] != header.get('tenant_key'))):
                raise DomainError('请由指定人员在原内测群的演示消息中操作', 403)
            person, _ = self.identities.resolve(app_id, session['open_id'], session['project_id'], revision=session['binding_revision'])
            if person.id != session['user_id']:
                raise DomainError('演示人员绑定已改变，请重新获取演示卡', 403)
            if session['expires_at'] < time.time():
                raise DomainError('本演示已到期，请重新获取演示卡', 409)
            event_id = header.get('event_id')
            if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
                raise DomainError('演示事件编号无效')
            if con.execute('SELECT 1 FROM events WHERE session_id=? AND event_id=?', (session['id'], event_id)).fetchone():
                return self.response(session, '此演示操作已完成')
            if value.get('card_token') != session['token']:
                return self.response(session, '演示已更新，请使用当前卡片', 'warning')
            operation = value.get('action')
            allowed = {'ready': {'edit', 'refresh', 'simulate'}, 'edit': {'preview', 'refresh'},
                       'preview': {'edit', 'refresh', 'simulate'}, 'result': {'refresh'}}
            if operation not in allowed[session['mode']]:
                raise DomainError('当前演示不支持此操作', 409)
            if operation == 'edit':
                session['mode'] = 'edit'
            elif operation == 'preview':
                plan = demo_plan(session)
                fields = {f['key']: f for f in editable_fields(plan, DICTIONARIES)}
                values = action.get('form_value') or {}
                if not isinstance(values, dict) or set(values) - fields.keys():
                    raise DomainError('演示表单含有未展示或只读字段')
                for key, item in values.items():
                    field = fields[key]
                    if field['kind'] == 'personnel' and isinstance(item, list) and all(isinstance(v, str) for v in item):
                        item = ','.join(item)
                    if item is None:
                        item = ''
                    if not isinstance(item, str) or len(item) > 1000:
                        raise DomainError('演示字段格式不正确或超过一千字')
                    item = '' if item == '__empty__' else item.strip()
                    if field['kind'] in ('select', 'dictionary', 'personnel'):
                        codes = {str(o['code']) for o in field_options(field, plan, DICTIONARIES)}
                        if any(v and v not in codes for v in (item.split(',') if field['kind'] == 'personnel' else [item])):
                            raise DomainError('请选择演示提供的选项')
                    session['values'][key] = item
                session['mode'] = 'preview'
            elif operation == 'refresh':
                session.update(mode='ready', values={})
            elif operation == 'simulate':
                if any(i['severity'] == 'error' for i in demo_plan(session)['issues']):
                    raise DomainError('请先补齐模拟字段后预览')
                session['mode'] = 'result'
            session['token'] = uid()
            response = self.response(session, '模拟确认完成，未提交官网' if operation == 'simulate' else '演示已更新')
            con.execute('UPDATE sessions SET body=? WHERE id=?', (dump(session), session['id']))
            con.execute('INSERT INTO events VALUES(?,?,?,?)', (session['id'], event_id, operation, time.time()))
            return response

    @staticmethod
    def response(session, message, kind='info'):
        return {'toast': {'type': kind, 'content': message}, 'card': {'type': 'raw', 'data': render_demo(session)}}
