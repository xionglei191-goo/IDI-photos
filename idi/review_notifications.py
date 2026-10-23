"""Notify the configured project contact when a saved proposal needs review."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading

from .connectors.feishu_feedback import UncertainDelivery
from .db import audit, dump, now, uid
from .domain import DomainError
from .project_followup import plain

log = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def proposal_key(plan):
    """Exclude revision counters: rereading unchanged materials must stay quiet."""
    case = plan['case']
    files = {f['key']: {
        'existing': sorted((str(p['id']), p.get('fileUrl', ''), p.get('fileName', '')) for p in f['existing']),
        'selected': sorted((p['id'], p['blob_hash'], p['revision']) for p in f['selected']),
    } for f in plan['file_fields']}
    return digest({'case': {k: case.get(k) for k in ('id', 'project_id', 'short_code', 'platform_report_code')},
                   'stage': plan['stage'], 'target': plan['link']['report_receiving_id'],
                   'business': plan['business_fields'], 'files': files})


class ReviewNotifications:
    def __init__(self, automatic, followup):
        self.automatic, self.followup = automatic, followup
        self.db, self.platform, self.reviews = automatic.db, automatic.platform, automatic.reviews
        self._lock, self._stop, self._thread = threading.RLock(), threading.Event(), None

    def enabled(self):
        policy = self.automatic.policy()
        return bool(policy['enabled'] and policy['notify_review_ready'])

    def recipient(self, project_id):
        contact = self.db.one('SELECT * FROM followup_contacts WHERE project_id=?', (project_id,))
        if not contact:
            raise DomainError('项目尚未绑定飞书负责人，请在“项目跟进”中绑定后自动提醒。', 409)
        if not re.fullmatch(r'ou_[A-Za-z0-9]+', contact['open_id']):
            raise DomainError('项目负责人账号无效，请重新绑定。', 409)
        connector = self.followup.connectors.get(contact['connector_id'])
        if connector['kind'] != 'feishu' or not connector['enabled']:
            raise DomainError('负责人绑定的飞书群入口已暂停，请启用后继续提醒。', 409)
        return {**contact, 'chat_id': connector['config']['chat_id'],
                'chat_name': connector['config'].get('chat_name') or connector['name']}

    def current(self, case_id):
        row = self.db.one('SELECT * FROM automatic_upload_cases WHERE case_id=?', (case_id,))
        if not self.enabled() or not row or not row['enabled'] or row['state'] != 'awaiting_confirmation' or not row['review_id']:
            raise DomainError('案件当前不再等待核对拟提交信息，提醒已取消。', 409)
        record = self.reviews.get(row['review_id'])
        if record['save_only'] or record['active'] or record['state'] not in ('prepared', 'reviewed'):
            raise DomainError('拟提交记录已变更或正在办理，提醒已取消。', 409)
        if row['review_guard'] != self.automatic.signature(case_id, proposal=True):
            raise DomainError('案件资料已变化，等待重新生成拟提交信息。', 409)
        plan, fingerprint = self.reviews.snapshot(case_id, row['stage'], review_only=bool(record['snapshot'].get('review_only')))
        if record['fingerprint'] != fingerprint or plan['case']['workflow_stage'] != row['stage']:
            raise DomainError('当前环节或拟提交内容已变化，提醒已取消。', 409)
        return row, plan

    @staticmethod
    def dedupe(key, recipient):
        return digest([key, recipient['connector_id'], recipient['chat_id'], recipient['open_id']])

    def message(self, plan, recipient):
        case = plan['case']
        lines = [
            f'<at user_id="{recipient["open_id"]}">{plain(recipient["name"])}</at>',
            f'{plain(case["project_name"])} · {plain(plan["label"])}资料待核对',
            '案件：' + plain(case.get('short_code') or case.get('platform_report_code') or '编号待补'),
        ]
        if case.get('platform_report_code'):
            lines.append('报修编号：' + plain(case['platform_report_code']))
        if case.get('title'):
            lines.append('事项：' + plain(case['title'], 300))
        files = [f'{plain(f["label"])} {len(f["existing"])} 项' for f in plan['file_fields'] if f['existing']]
        if files:
            lines.append('已保存：' + '；'.join(files))
        if plan.get('pending_fields'):
            lines.append('待管理员确认：' + '、'.join(plain(f['label']) for f in plan['pending_fields']) + '。')
            lines.append('本环节已收照片已合并，已自动填写可确定内容。请在 IDI 工作台的“拟提交”中点击“核对拟提交信息”，确认照片是否传齐、补齐待确认字段并审核；确认后系统自动正式提交。')
        else:
            lines.append('本环节资料已收集并保存，请在 IDI 工作台的“拟提交”中点击“核对拟提交信息”，核对字段和照片后确认提交。')
        settings = self.followup.settings
        urls = list(settings.public_origins) or settings.lan_urls
        if urls:
            lines.append('工作台：' + urls[0])
        return dump({'text': '\n'.join(lines)})

    def queue(self, case_id):
        with self._lock, self.platform.material_lock:
            row, plan = self.current(case_id)
            recipient = self.recipient(plan['case']['project_id'])
            key = proposal_key(plan)
            dedupe = self.dedupe(key, recipient)
            existing = self.db.one('SELECT * FROM review_notifications WHERE dedupe_key=?', (dedupe,))
            if existing and existing['state'] != 'cancelled':
                return existing
            content = self.message(plan, recipient)
            with self.db.tx() as con:
                if existing:
                    con.execute("UPDATE review_notifications SET state='queued',review_id=?,content=?,error=NULL WHERE id=?",
                                (row['review_id'], content, existing['id']))
                    delivery_id = existing['id']
                else:
                    delivery_id = uid()
                    con.execute('''INSERT INTO review_notifications(
                        id,dedupe_key,case_id,stage,project_id,review_id,proposal_key,
                        connector_id,open_id,name,chat_id,chat_name,content,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (delivery_id, dedupe, case_id, row['stage'], plan['case']['project_id'], row['review_id'], key,
                         recipient['connector_id'], recipient['open_id'], recipient['name'],
                         recipient['chat_id'], recipient['chat_name'], content, now()))
            return self.db.one('SELECT * FROM review_notifications WHERE id=?', (delivery_id,))

    def status_for(self, upload):
        if upload['state'] != 'awaiting_confirmation' or not upload.get('review_id'):
            return None
        if not self.enabled():
            return {'state': 'disabled', 'message': '飞书核对提醒已关闭'}
        try:
            record = self.reviews.get(upload['review_id'])
            recipient = self.recipient(record['snapshot']['case']['project_id'])
            key = self.dedupe(proposal_key(record['snapshot']), recipient)
            row = self.db.one('SELECT * FROM review_notifications WHERE dedupe_key=?', (key,))
            if row:
                return {k: row[k] for k in ('id', 'state', 'error', 'name', 'chat_name', 'sent_at')}
            return {'state': 'queued', 'name': recipient['name'], 'chat_name': recipient['chat_name']}
        except DomainError as error:
            return {'state': 'blocked', 'message': str(error)}

    def _matches(self, delivery):
        row, plan = self.current(delivery['case_id'])
        recipient = self.recipient(plan['case']['project_id'])
        if (proposal_key(plan) != delivery['proposal_key']
                or any(recipient[k] != delivery[k] for k in ('connector_id', 'chat_id', 'open_id'))):
            raise DomainError('拟提交资料、群或项目负责人已变更，旧提醒已取消。', 409)
        return row, plan

    def deliver(self, delivery_id):
        # Share the contact-edit lock, then the material lock (same order as photo deletion).
        with self._lock, self.followup._send_lock, self.platform.material_lock:
            delivery = self.db.one('SELECT * FROM review_notifications WHERE id=?', (delivery_id,))
            if not delivery or delivery['state'] != 'queued':
                return
            client, sent_id, started = None, None, False
            try:
                self._matches(delivery)
                client = self.followup.client(delivery['connector_id'])
                if not any(p['open_id'] == delivery['open_id'] for p in client.members(delivery['chat_id'])):
                    raise DomainError('项目负责人已不在群中，请重新绑定。')
                # Membership lookup may take time; recheck readiness immediately before sending.
                self._matches(delivery)
                if self._stop.is_set():
                    return
                with self.db.tx() as con:
                    con.execute("UPDATE review_notifications SET state='sending',error=NULL WHERE id=?", (delivery_id,))
                started = True
                sent_id = client.send_feedback(delivery['chat_id'], delivery['content'], delivery_id)
                with self.db.tx() as con:
                    con.execute("UPDATE review_notifications SET state='sent',remote_message_id=?,sent_at=? WHERE id=?",
                                (sent_id, now(), delivery_id))
                    audit(con, 'review_notification', delivery_id, 'sent',
                          after={'case_id': delivery['case_id'], 'review_id': delivery['review_id'], 'remote_message_id': sent_id})
            except Exception as error:
                uncertain = sent_id is not None or isinstance(error, UncertainDelivery) or (started and not isinstance(error, DomainError))
                state = 'unknown' if uncertain else 'cancelled' if getattr(error, 'status', None) == 409 else 'failed'
                message = str(error) if isinstance(error, DomainError) else '飞书提醒处理失败，请检查连接后重试。'
                with self.db.tx() as con:
                    con.execute('UPDATE review_notifications SET state=?,error=? WHERE id=?', (state, message[:700], delivery_id))
                if not isinstance(error, DomainError):
                    log.exception('Proposal notification failed for %s', delivery_id)
            finally:
                if client:
                    client.close()

    def retry(self, delivery_id):
        with self._lock:
            row = self.db.one('SELECT * FROM review_notifications WHERE id=?', (delivery_id,))
            if not row or row['state'] != 'failed':
                raise DomainError('仅明确失败的提醒可以重试；结果不确定时请先核对群内记录。', 409)
            self._matches(row)
            with self.db.tx() as con:
                con.execute("UPDATE review_notifications SET state='queued',error=NULL WHERE id=?", (delivery_id,))
        return {'state': 'queued'}

    def confirm(self, delivery_id, received):
        if type(received) is not bool:
            raise DomainError('请先核对群内是否收到这条提醒。')
        with self._lock:
            row = self.db.one('SELECT * FROM review_notifications WHERE id=?', (delivery_id,))
            if not row or row['state'] != 'unknown':
                raise DomainError('只有发送结果不确定的提醒需要核对。', 409)
            if not received:
                self._matches(row)
            with self.db.tx() as con:
                state = 'sent' if received else 'queued'
                con.execute('UPDATE review_notifications SET state=?,sent_at=?,error=? WHERE id=?',
                            (state, now() if received else None, '人工核对群内已收到' if received else None, delivery_id))
                audit(con, 'review_notification', delivery_id, 'confirm_delivery', after={'received': received})
        return {'state': state}

    def run_once(self):
        if self._stop.is_set() or not self._lock.acquire(blocking=False):
            return 0
        try:
            if not self.enabled():
                if self.db.one("SELECT id FROM review_notifications WHERE state='queued' LIMIT 1"):
                    with self.db.tx() as con:
                        con.execute("UPDATE review_notifications SET state='cancelled',error='飞书核对提醒已暂停' WHERE state='queued'")
                return 0
            for row in self.db.all("SELECT case_id FROM automatic_upload_cases WHERE enabled=1 AND state='awaiting_confirmation'"):
                try:
                    self.queue(row['case_id'])
                except DomainError:
                    # Missing bindings/readiness are visible beside the proposal and retried after changes.
                    continue
            deliveries = self.db.all("SELECT id FROM review_notifications WHERE state='queued' ORDER BY created_at LIMIT 10")
            for row in deliveries:
                if self._stop.is_set():
                    break
                self.deliver(row['id'])
            return len(deliveries)
        finally:
            self._lock.release()

    def recover(self):
        with self.db.tx() as con:
            con.execute("UPDATE review_notifications SET state='unknown',error='发送时服务中断，请先核对飞书群内记录' WHERE state='sending'")

    def start(self):
        if self._thread:
            return
        self.recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='idi-review-notifications')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception('Proposal notification queue failed')
            self._stop.wait(2)
