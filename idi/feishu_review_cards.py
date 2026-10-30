"""Human card actions: durable SDK intake, main-process execution and receipts.

The SDK child never logs into the official website or executes an action. Only
the main application owns official sessions and drains this queue. No Agent
tool can call this entry point.
"""
from __future__ import annotations

import io
import json
import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from PIL import Image, ImageOps

from .db import audit, dump, now, uid
from .domain import DomainError
from .feishu_card_demos import FeishuCardDemos
from .identity import acting_as
from .review_card_content import PAGE_SIZE, attachments, build_card, editable_fields

log = logging.getLogger(__name__)
EXECUTED = {'submitting', 'submitted', 'verified', 'uncertain', 'saved', 'save_verified'}
MANAGE = {'edit', 'preview', 'submit', 'readback'}


class FeishuReviewCards:
    def __init__(self, notifications, identities):
        self.notifications, self.identities = notifications, identities
        self.db, self.reviews, self.platform = notifications.db, notifications.reviews, notifications.platform
        self.followup = notifications.followup
        self.demos = FeishuCardDemos(self.followup, identities)
        self._lock = threading.Lock()

    def delivery(self, notification_id):
        row = self.db.one('SELECT * FROM review_notifications WHERE id=?', (notification_id,))
        if not row or row['message_type'] != 'interactive':
            raise DomainError('卡片已失效，请打开当前拟提交信息', 409)
        return row

    def url(self, delivery):
        settings = self.followup.settings
        origins = list(settings.public_origins) or settings.lan_urls
        return origins[0].rstrip('/') + '/?' + urlencode({'review': delivery['review_id']}) if origins else ''

    def _scope(self, delivery, *, readback=False):
        connector = self.followup.connectors.get(delivery['connector_id'])
        if (connector['kind'] != 'feishu' or not connector['enabled']
                or connector['config']['app_id'] != delivery['app_id']
                or connector['config']['chat_id'] != delivery['chat_id']):
            raise DomainError('卡片所属飞书入口已停用或改变', 409)
        recipient = self.notifications.recipient(delivery['project_id'])
        if any(recipient[k] != delivery[k] for k in ('connector_id', 'chat_id', 'open_id')):
            raise DomainError('项目负责人或群已改变，请使用新卡片', 409)
        if not readback and not self.notifications.enabled():
            raise DomainError('飞书核对提醒已暂停，请到 IDI 工作台核对', 409)
        return connector

    def _fresh(self, delivery, view):
        self._scope(delivery)
        row, _ = self.notifications._matches(delivery)
        if row['review_id'] != delivery['review_id']:
            raise DomainError('核对记录已更新，请刷新卡片后重新预览', 409)
        record = self.reviews.get(delivery['review_id'])
        if (record['fingerprint'] != view.get('fingerprint') or record['active']
                or record['state'] not in ('prepared', 'reviewed')):
            raise DomainError('资料已变化或正在办理，请使用最新核对记录', 409)
        return record

    def initialize(self, delivery, plan):
        record = self.reviews.get(delivery['review_id'])
        connector = self.followup.connectors.get(delivery['connector_id'])
        delivery = {**delivery, 'app_id': connector['config']['app_id'], 'card_token': uid()}
        view = {'mode': 'ready', 'fingerprint': record['fingerprint'], 'page': 0,
                'expires_at': time.time() + 7 * 86400}
        content = dump(build_card(delivery, plan, view, self.platform.dictionaries()['items'], url=self.url(delivery)))
        with self.db.tx() as con:
            con.execute("UPDATE review_notifications SET message_type='interactive',app_id=?,card_token=?,card_view=?,content=? WHERE id=?",
                        (delivery['app_id'], delivery['card_token'], dump(view), content, delivery['id']))
        return self.delivery(delivery['id'])

    def renew(self, delivery, review_id):
        """Resume an unchanged proposal in its original message, without a resend."""
        self._scope(delivery)
        record = self.reviews.get(review_id)
        delivery = {**delivery, 'review_id': review_id}
        return self._replace(delivery, {'mode': 'ready', 'fingerprint': record['fingerprint'], 'page': 0,
                                       'expires_at': time.time() + 7 * 86400})

    def _images(self, delivery, plan, page, client):
        result = []
        for field, source, item in attachments(plan)[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            name = item.get('original_name') or item.get('oldFileName') or item.get('fileName') or '附件'
            entry = {'label': field['label'] + ' · ' + name, 'field_key': field['key']}
            try:
                if source == 'existing':
                    blob, _ = self.platform.media.ensure(plan, field['key'], str(item['id']))
                else:
                    photo = self.db.one('SELECT case_id,blob_hash,revision FROM photos WHERE id=?', (item['id'],))
                    if (not photo or photo['case_id'] != plan['case']['id']
                            or photo['blob_hash'] != item['blob_hash'] or photo['revision'] != item['revision']):
                        raise DomainError('照片已变化，请重新核对', 409)
                    blob = self.db.one('SELECT * FROM blobs WHERE hash=?', (photo['blob_hash'],))
                if not blob or not self.platform.storage.verify(blob):
                    raise DomainError('原件校验失败，请打开完整资料核对', 409)
                if blob['media_type'] == 'image':
                    cached = self.db.one('SELECT image_key FROM feishu_card_images WHERE app_id=? AND blob_hash=?',
                                         (delivery['app_id'], blob['hash']))
                    if cached:
                        entry['image_key'] = cached['image_key']
                    else:
                        with Image.open(self.platform.storage.resolve(blob['stored_path'])) as original:
                            preview = ImageOps.exif_transpose(original).convert('RGB')
                            preview.thumbnail((1600, 1600))
                            output = io.BytesIO()
                            preview.save(output, 'JPEG', quality=88)
                        entry['image_key'] = client.upload_card_image(output.getvalue())
                        with self.db.tx() as con:
                            con.execute('INSERT INTO feishu_card_images VALUES(?,?,?,?) '
                                        'ON CONFLICT(app_id,blob_hash) DO UPDATE SET image_key=excluded.image_key,created_at=excluded.created_at',
                                        (delivery['app_id'], blob['hash'], entry['image_key'], now()))
            except Exception as error:
                entry['error'] = str(error) if isinstance(error, DomainError) else '图片预览暂不可用，请打开完整资料核对'
            result.append(entry)
        return result

    def _content(self, delivery, view, client=None):
        plan = view.get('plan') or self.reviews.get(delivery['review_id'])['snapshot']
        images = self._images(delivery, plan, view.get('page', 0), client) if (
            client and view['mode'] in ('ready', 'edit', 'preview', 'result')) else []
        # Recompute on each render: a recovered image preview can re-enable a
        # fully displayed card, while oversized cards explicitly disable it.
        view['direct_allowed'] = (not any(item.get('error') for item in images)
                                  and not any(i['severity'] == 'error' for i in plan['issues']))
        view['media_ready'] = bool(client) or view['mode'] not in ('ready', 'edit', 'preview', 'result')
        content = build_card(delivery, plan, view, self.platform.dictionaries()['items'], images, url=self.url(delivery))
        return dump(content)

    def prepare_send(self, delivery, client):
        row, plan = self.notifications._matches(delivery)
        record = self.reviews.get(row['review_id'])
        delivery = {**delivery, 'review_id': row['review_id']}
        view = {'mode': 'ready', 'fingerprint': record['fingerprint'], 'page': 0,
                'expires_at': time.time() + 7 * 86400}
        content = self._content(delivery, view, client)
        self._fresh(delivery, view)
        with self.db.tx() as con:
            con.execute('UPDATE review_notifications SET review_id=?,card_view=?,content=? WHERE id=?',
                        (delivery['review_id'], dump(view), content, delivery['id']))
        return self.delivery(delivery['id'])

    def _replace(self, delivery, view, client=None):
        delivery = {**delivery, 'card_token': uid()}
        content = self._content(delivery, view, client)
        with self.db.tx() as con:
            con.execute('UPDATE review_notifications SET review_id=?,card_token=?,card_view=?,content=?,'
                        "card_dirty=1,card_error='',card_retry_at=0,card_checked_at=? WHERE id=?",
                        (delivery['review_id'], delivery['card_token'], dump(view), content, time.time(), delivery['id']))
        return self.delivery(delivery['id'])

    @staticmethod
    def toast(message, kind='info'):
        return {'toast': {'type': kind, 'content': message}}

    def receive_callback(self, connector_id, app_id, data):
        """Called only by the authenticated SDK connection; bounded DB work."""
        try:
            if data.get('event', {}).get('action', {}).get('value', {}).get('demo_session'):
                return self.demos.receive(connector_id, app_id, data)
            return self._receive(connector_id, app_id, data)
        except DomainError as error:
            return self.toast(str(error), 'error')
        except sqlite3.OperationalError:
            return self.toast('系统正在处理资料，请稍后再点击；本次尚未受理', 'warning')
        except (KeyError, TypeError, ValueError, AttributeError):
            return self.toast('卡片回调格式无效，请刷新卡片', 'error')

    def _receive(self, connector_id, app_id, data):
        header, event = data['header'], data['event']
        if header.get('event_type') != 'card.action.trigger' or header.get('app_id') != app_id:
            raise DomainError('卡片应用标识不匹配', 403)
        source = self.followup.connectors.get(connector_id)
        if source['kind'] != 'feishu' or not source['enabled'] or source['config']['app_id'] != app_id:
            raise DomainError('接收卡片的飞书应用连接已失效', 403)
        event_id = header.get('event_id')
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
            raise DomainError('卡片事件编号无效')
        operator, context, action = event['operator'], event['context'], event['action']
        value = action['value']
        if operator.get('tenant_key') and operator['tenant_key'] != header.get('tenant_key'):
            raise DomainError('卡片人员所属组织不匹配', 403)
        delivery = self.delivery(value['notification_id'])
        operation = value.get('action')
        # Multiple configured groups may share one application. Feishu can
        # deliver its callback to any of that application's SDK connections.
        if (delivery['app_id'] != app_id
                or delivery['state'] != 'sent' or context.get('open_chat_id') != delivery['chat_id']
                or context.get('open_message_id') != delivery['remote_message_id']
                or event.get('host') != 'im_message'):
            raise DomainError('只能在本项目原始群消息中操作此卡片', 403)
        self._scope(delivery, readback=operation == 'readback')
        person, binding = self.identities.resolve(app_id, operator['open_id'], delivery['project_id'],
                                                  manage=operation in MANAGE)
        old = self.db.one('SELECT state FROM feishu_review_actions WHERE app_id=? AND event_id=?', (app_id, event_id))
        if old:
            return self.toast('此操作已经受理，请查看卡片结果')
        view = json.loads(delivery['card_view'])
        if value.get('card_token') != delivery['card_token']:
            raise DomainError('卡片内容已更新，请等待最新卡片后再操作', 409)
        allowed = {'ready': {'edit', 'submit', 'photos', 'refresh'},
                   'preview': {'edit', 'submit', 'photos', 'refresh'},
                   'edit': {'preview', 'refresh'}, 'stale': {'refresh'},
                   'result': {'readback'} if view.get('can_readback') else {'refresh'} if view.get('can_refresh') else set()}
        if operation not in allowed.get(view.get('mode'), set()):
            raise DomainError('当前卡片不支持此操作，请查看最新状态', 409)
        if operation != 'refresh' and view.get('expires_at', time.time() + 1) < time.time():
            raise DomainError('本次预览已过期，请刷新卡片后重新核对', 409)
        if operation in ('preview', 'submit') and view.get('owner_user_id') and (
                view['owner_user_id'] != person.id or view.get('binding_revision') != binding['revision']):
            raise DomainError('请由本次修改信息的人员确认；您可先打开补充修改信息重新核对', 403)
        if operation == 'submit' and (value.get('acknowledged') is not True or not view.get('direct_allowed', True)):
            raise DomainError('请先核对完整资料；此卡片暂不能直接提交', 409)
        payload = {}
        if operation == 'preview':
            plan = view.get('plan') or json.loads(self.db.one(
                'SELECT snapshot FROM platform_review_batches WHERE id=?', (delivery['review_id'],))['snapshot'])
            fields = {f['key']: f for f in editable_fields(plan, self.platform.dictionaries()['items'])}
            values = action.get('form_value') or {}
            if not isinstance(values, dict) or set(values) - fields.keys():
                raise DomainError('表单含有未展示或只读字段，请重新打开卡片')
            clean = dict(view.get('values', {}))
            for key, item in values.items():
                if fields[key]['kind'] == 'personnel' and isinstance(item, list) and all(isinstance(v, str) for v in item):
                    item = ','.join(item)
                if item is None:
                    item = ''
                if not isinstance(item, str) or len(item) > 1000:
                    raise DomainError('字段内容过长或格式不正确，请打开完整资料修改')
                clean[key] = '' if item == '__empty__' else item.strip()
            payload['values'] = clean
        if operation == 'photos':
            page = value.get('page')
            if type(page) is not int or not 0 <= page <= 1000:
                raise DomainError('照片页码无效')
            payload['page'] = page
        # A separate short-timeout writer keeps the callback below Feishu's 3s
        # deadline even while an official write holds the normal SQLite lock.
        con = self.db.connect()
        try:
            con.execute('PRAGMA busy_timeout=1000')
            con.execute('BEGIN IMMEDIATE')
            latest = con.execute('SELECT card_token,pending_action_id FROM review_notifications WHERE id=?',
                                 (delivery['id'],)).fetchone()
            if latest['card_token'] != delivery['card_token'] or latest['pending_action_id']:
                raise DomainError('此卡片正在处理，请等待结果，勿重复点击', 409)
            action_id = uid()
            con.execute('INSERT INTO feishu_review_actions(id,event_id,app_id,notification_id,card_token,'
                        'user_id,open_id,binding_revision,action,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (action_id, event_id, app_id, delivery['id'], delivery['card_token'], person.id,
                         operator['open_id'], binding['revision'], operation, dump(payload), now()))
            con.execute('UPDATE review_notifications SET pending_action_id=? WHERE id=?', (action_id, delivery['id']))
            audit(con, 'feishu_review', action_id, 'accepted',
                  after={'notification_id': delivery['id'], 'action': operation, 'review_id': delivery['review_id']}, actor=person.id)
            con.commit()
        finally:
            if con.in_transaction:
                con.rollback()
            con.close()
        return self.toast('已收到确认，正在提交并回读官网结果' if operation == 'submit' else '已收到，正在更新卡片')

    def _permission(self, job, client=None):
        delivery = self.delivery(job['notification_id'])
        self._scope(delivery, readback=job['action'] == 'readback')
        person, _ = self.identities.resolve(delivery['app_id'], job['open_id'], delivery['project_id'],
                                            manage=job['action'] in MANAGE, revision=job['binding_revision'])
        if person.id != job['user_id']:
            raise DomainError('操作人员绑定已改变', 403)
        if client and not any(m['open_id'] == job['open_id'] for m in client.members(delivery['chat_id'])):
            raise DomainError('操作人员已不在原飞书群中', 403)
        return person

    def _result_view(self, record, *, person=None, error=''):
        state = record['state']
        labels = {'verified': '已提交并回读核实', 'submitted': '官网已接收 · 等待回读核实',
                  'submitting': '办理中断 · 请先回读核实', 'uncertain': '提交结果待核实',
                  'failed': '操作未完成', 'stale': '资料已变化 · 本次未提交',
                  'saved': '官网已保存 · 尚未正式提交', 'save_verified': '草稿已保存 · 尚未正式提交'}
        result = record.get('result', {})
        notice = error or result.get('error') or ''
        if state == 'verified':
            notice = '官网已接收本次正式提交，回读核对一致。'
        if person:
            notice = f'确认人员：{person.display_name}\n' + notice
        if record.get('confirmed_at'):
            confirmed_at = datetime.fromisoformat(record['confirmed_at']).astimezone(timezone(timedelta(hours=8)))
            notice += '\n确认时间：' + confirmed_at.strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）'
        return {'mode': 'result', 'status': labels.get(state, '本次操作未执行'),
                'verified': state == 'verified', 'can_readback': state in EXECUTED and state not in ('verified', 'save_verified'),
                'can_refresh': state not in EXECUTED, 'notice': notice, 'plan': record['snapshot'],
                'review_state': state, 'result_key': dump(result), 'page': 0,
                'actor_name': person.display_name if person else ''}

    def process_one(self):
        with self.db.tx() as con:
            job = con.execute("SELECT * FROM feishu_review_actions WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not job:
                return False
            job = dict(job)
            con.execute("UPDATE feishu_review_actions SET state='running' WHERE id=? AND state='queued'", (job['id'],))
        client, error, state = None, '', 'done'
        try:
            delivery = self.delivery(job['notification_id'])
            client = self.followup.client(delivery['connector_id'])
            person = self._permission(job, client)
            with acting_as(person), self.followup._send_lock, self.platform.material_lock:
                delivery = self.delivery(job['notification_id'])
                view = json.loads(delivery['card_view'])
                if delivery['card_token'] != job['card_token'] or delivery['pending_action_id'] != job['id']:
                    raise DomainError('卡片内容已更新，本次操作未执行', 409)
                action, payload = job['action'], json.loads(job['payload'])
                if action == 'readback':
                    record = self.reviews.reconcile(delivery['review_id'])
                    self._replace(delivery, self._result_view(record, person=person), client)
                elif action == 'refresh':
                    row, plan = self.notifications._matches(delivery)
                    record = self.reviews.get(row['review_id'])
                    delivery['review_id'] = record['id']
                    fresh = {'mode': 'ready', 'fingerprint': record['fingerprint'], 'page': 0,
                             'expires_at': time.time() + 7 * 86400}
                    self._replace(delivery, fresh, client)
                else:
                    record = self._fresh(delivery, view)
                    if view.get('expires_at', 0) < time.time():
                        raise DomainError('预览已过期，请重新核对', 409)
                    if action == 'edit':
                        view.update(mode='edit', owner_user_id=person.id, owner_open_id=job['open_id'],
                                    binding_revision=job['binding_revision'], expires_at=time.time() + 1800,
                                    notice='当前核对人员：' + person.display_name)
                        self._replace(delivery, view, client)
                    elif action == 'preview':
                        plan = self.reviews.preview_edits(record['id'], view['fingerprint'], payload['values'])
                        view.update(mode='preview', plan=plan, values=payload['values'], page=0,
                                    changed_fields=[key for key, value in payload['values'].items()
                                                    if str(record['snapshot']['values'].get(key, '') or '') != value],
                                    expires_at=time.time() + 1800,
                                    notice='本次修改尚未保存。请核对下列内容后确认正式提交。')
                        self._replace(delivery, view, client)
                    elif action == 'photos':
                        plan = view.get('plan') or record['snapshot']
                        if payload['page'] >= max(1, math.ceil(len(attachments(plan)) / PAGE_SIZE)):
                            raise DomainError('照片页码已失效，请刷新卡片')
                        view['page'] = payload['page']
                        self._replace(delivery, view, client)
                    elif action == 'submit':
                        if view.get('owner_user_id') and view['owner_user_id'] != person.id:
                            raise DomainError('请由本次核对人员本人确认提交', 403)
                        if not view.get('direct_allowed', True):
                            raise DomainError('请打开完整资料核对后提交', 409)
                        values = view.get('values')
                        plan = self.reviews.preview_edits(record['id'], view['fingerprint'], values) if values is not None else record['snapshot']
                        if any(i['severity'] == 'error' for i in plan['issues']):
                            raise DomainError('业务字段或附件仍有待补项，请先补齐后预览', 409)
                        processing = {**view, 'mode': 'processing', 'notice': '正在提交并回读官网，重复点击不会再次提交。'}
                        delivery = self._replace(delivery, processing)
                        self.publish(delivery, client)
                        self._permission(job, client)
                        record = self.reviews.confirm(record['id'], view['fingerprint'], action='submit', values=values,
                                                      check_permission=lambda: self._permission(job, client))
                        self._replace(delivery, self._result_view(record, person=person), client)
        except Exception as failure:
            state = 'rejected'
            error = str(failure) if isinstance(failure, DomainError) else '操作处理失败，请在完整资料中查看当前状态'
            try:
                delivery = self.delivery(job['notification_id'])
                record = self.reviews.get(delivery['review_id'])
                view = self._result_view(record, error=error)
                self._replace(delivery, view)
            except Exception:
                log.exception('Failed to refresh card after rejected action %s', job['id'])
            if not isinstance(failure, DomainError):
                log.exception('Card action failed %s', job['id'])
        finally:
            with self.db.tx() as con:
                con.execute('UPDATE feishu_review_actions SET state=?,error=?,finished_at=? WHERE id=?',
                            (state, error[:700], now(), job['id']))
                con.execute('UPDATE review_notifications SET pending_action_id=NULL WHERE id=? AND pending_action_id=?',
                            (job['notification_id'], job['id']))
                audit(con, 'feishu_review', job['id'], state, after={'action': job['action'], 'error': error[:700]}, actor=job['user_id'])
            if client:
                client.close()
        return True

    def publish(self, delivery, client):
        if not delivery['card_dirty'] or not delivery['remote_message_id']:
            return
        try:
            view = json.loads(delivery['card_view'])
            if not view.get('media_ready', True):
                content = self._content(delivery, view, client)
                if view['mode'] in ('ready', 'edit', 'preview'):
                    self._fresh(delivery, view)
                with self.db.tx() as con:
                    con.execute('UPDATE review_notifications SET content=?,card_view=? WHERE id=? AND card_token=?',
                                (content, dump(view), delivery['id'], delivery['card_token']))
                delivery = {**delivery, 'content': content}
            client.update_card(delivery['remote_message_id'], delivery['content'])
            with self.db.tx() as con:
                con.execute("UPDATE review_notifications SET card_dirty=0,card_error='',card_retry_at=0 WHERE id=? AND card_token=?",
                            (delivery['id'], delivery['card_token']))
        except Exception as error:
            message = str(error) if isinstance(error, DomainError) else '卡片结果暂未更新，可在 IDI 查看办理回执'
            with self.db.tx() as con:
                con.execute('UPDATE review_notifications SET card_error=?,card_retry_at=? WHERE id=? AND card_token=?',
                            (message[:700], time.time() + 30, delivery['id'], delivery['card_token']))

    def synchronize(self):
        deliveries = self.db.all(
            "SELECT * FROM review_notifications WHERE message_type='interactive' AND state='sent' "
            "AND COALESCE(json_extract(card_view,'$.verified'),0)=0 AND json_extract(card_view,'$.mode')<>'stale' "
            'AND pending_action_id IS NULL AND card_checked_at<? ORDER BY card_checked_at LIMIT 10', (time.time() - 15,))
        for delivery in deliveries:
            view = json.loads(delivery['card_view'])
            try:
                record = self.reviews.get(delivery['review_id'])
                if record['state'] in EXECUTED:
                    if view.get('review_state') != record['state'] or view.get('result_key') != dump(record['result']):
                        self._replace(delivery, self._result_view(record))
                elif view['mode'] != 'stale':
                    row, _ = self.notifications._matches(delivery)
                    self._scope(delivery)
                    if row['review_id'] != delivery['review_id']:
                        record = self.reviews.get(row['review_id'])
                        delivery['review_id'] = record['id']
                        self._replace(delivery, {'mode': 'ready', 'fingerprint': record['fingerprint'], 'page': 0,
                                                 'expires_at': time.time() + 7 * 86400})
                    elif view.get('expires_at', time.time() + 1) < time.time():
                        self._replace(delivery, {'mode': 'result', 'status': '预览已过期', 'can_refresh': True,
                                                 'notice': '请刷新卡片后重新核对，尚未执行提交。'})
            except DomainError:
                if view['mode'] not in ('stale',) and not view.get('verified'):
                    self._replace(delivery, {'mode': 'stale', 'notice': '资料、办理状态或授权已变化，此卡片不能再提交。请使用最新拟提交记录。'})
            finally:
                with self.db.tx() as con:
                    con.execute('UPDATE review_notifications SET card_checked_at=? WHERE id=?', (time.time(), delivery['id']))

    def run_once(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            # One read-only snapshot on idle ticks; do not acquire a SQLite
            # writer or assemble a proposal when there is no pending card work.
            with self.db.read_snapshot():
                pending = self.db.one("SELECT id FROM feishu_review_actions WHERE state='queued' LIMIT 1")
                due = self.db.one(
                    "SELECT id FROM review_notifications WHERE message_type='interactive' AND state='sent' "
                    "AND COALESCE(json_extract(card_view,'$.verified'),0)=0 AND json_extract(card_view,'$.mode')<>'stale' "
                    'AND pending_action_id IS NULL AND card_checked_at<? LIMIT 1', (time.time() - 15,))
                dirty = self.db.one("SELECT id FROM review_notifications WHERE message_type='interactive' AND state='sent' "
                                    'AND card_dirty=1 AND card_retry_at<=? LIMIT 1', (time.time(),))
            if pending:
                for _ in range(3):
                    if self.notifications._stop.is_set() or not self.process_one():
                        break
            if due:
                self.synchronize()
            deliveries = self.db.all(
                    "SELECT * FROM review_notifications WHERE message_type='interactive' AND state='sent' "
                    'AND card_dirty=1 AND card_retry_at<=? ORDER BY created_at LIMIT 10', (time.time(),)) if pending or due or dirty else []
            for delivery in deliveries:
                client = None
                try:
                    client = self.followup.client(delivery['connector_id'])
                    self.publish(delivery, client)
                except DomainError:
                    with self.db.tx() as con:
                        con.execute('UPDATE review_notifications SET card_retry_at=? WHERE id=?', (time.time() + 60, delivery['id']))
                finally:
                    if client:
                        client.close()
        finally:
            self._lock.release()

    def recover(self):
        # A running job may have reached the official website. Never replay it.
        jobs = self.db.all("SELECT * FROM feishu_review_actions WHERE state='running'")
        with self.db.tx() as con:
            con.execute("UPDATE feishu_review_actions SET state='interrupted',error='服务中断，请先核对结果，未自动重放',finished_at=? WHERE state='running'", (now(),))
            con.execute("UPDATE review_notifications SET pending_action_id=NULL WHERE pending_action_id IN "
                        "(SELECT id FROM feishu_review_actions WHERE state IN ('done','rejected','interrupted'))")
        for job in jobs:
            delivery = self.delivery(job['notification_id'])
            record = self.reviews.get(delivery['review_id'])
            self._replace(delivery, self._result_view(record, error='服务中断，未自动重复执行。请先核对本次操作结果。'))
