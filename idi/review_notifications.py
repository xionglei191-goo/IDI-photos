"""Notify the configured project contact when a saved proposal needs review."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time

from .connectors.feishu_feedback import UncertainDelivery
from .db import audit, dump, now, uid
from .domain import DomainError
from .project_followup import plain
from .review_requirements import pending_checkins
from .automatic_uploads import UPLOAD_INPUTS
from .background import background_work

log = logging.getLogger(__name__)
QUEUE_RECHECK_SECONDS = 60
NOTIFICATION_INPUTS = {**UPLOAD_INPUTS, **{name: None for name in (
    'platform_review_batches', 'followup_contacts', 'feishu_identity_bindings', 'feishu_review_actions')},
    'review_notifications': {'id', 'case_id', 'review_id', 'proposal_key', 'dedupe_key', 'state', 'purpose',
        'recipient_id', 'connector_id', 'chat_id', 'card_token', 'card_view', 'card_dirty', 'card_retry_at',
        'pending_action_id', 'retry_at', 'remote_message_id', 'content'}}


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
        self.cards = None
        self._queue_checks = {}
        self._signal = self.db.runtime.watch('notifications', NOTIFICATION_INPUTS)
        self._poll_token, self._poll_at, self._retry_at = None, 0.0, 0.0

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
                'chat_name': connector['config'].get('chat_name') or connector['name'],
                'app_id': connector['config']['app_id']}

    def current(self, case_id):
        # A proposal performs many related reads. Reuse one consistent SQLite
        # connection, including for the uncached send/callback validation paths.
        with self.platform.material_lock, self.db.read_snapshot():
            return self._current(case_id)

    def _current(self, case_id):
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

    def _queue_observation(self, case_id):
        """Cheap inputs for background polling, never authority to send/submit."""
        with self.db.read_snapshot():
            case = self.automatic.domain.case(case_id)
            case = {k: v for k, v in case.items() if k not in (
                'revision', 'created_at', 'updated_at', 'workflow_verified_at', 'remote_updated_at')}
            upload = self.db.one('SELECT * FROM automatic_upload_cases WHERE case_id=?', (case_id,))
            if not upload:
                raise DomainError('案件已移出自动处理范围', 409)
            review = self.db.one('SELECT id,state,fingerprint,save_only FROM platform_review_batches WHERE id=?',
                                 (upload['review_id'],))
            deliveries = self.db.all(
                'SELECT id,review_id,proposal_key,dedupe_key,state,card_token,card_view,pending_action_id '
                'FROM review_notifications WHERE case_id=? ORDER BY id', (case_id,))
            return digest([case, upload, review, deliveries, self.automatic.policy(),
                           self.automatic.signature(case_id, proposal=True),
                           self.recipient(case['project_id']), self.platform.dictionaries()])

    def _queue_if_changed(self, case_id):
        with self.platform.material_lock:
            observed = self._queue_observation(case_id)
            stamp = time.monotonic()
            previous = self._queue_checks.get(case_id)
            if previous and previous[0] == observed and stamp - previous[1] < QUEUE_RECHECK_SECONDS:
                return
            try:
                self.queue(case_id)
            except DomainError:
                # A blocked proposal is reconsidered when its inputs change or
                # the bounded retry expires; it must not spin every two seconds.
                self._queue_checks[case_id] = (observed, stamp)
                raise
            # Save the observation from BEFORE queue(): a concurrent change or
            # a delivery created/renewed here must be visible on the next tick.
            self._queue_checks[case_id] = (observed, stamp)

    @staticmethod
    def dedupe(key, recipient):
        return digest([key, recipient['connector_id'], recipient['chat_id'], recipient['open_id'], recipient['app_id']])

    def existing(self, key, recipient, plan=None):
        row = self.db.one('SELECT * FROM review_notifications WHERE dedupe_key=?', (self.dedupe(key, recipient),))
        if row:
            return row
        # Pre-card reminders have no app identity and used the original four-part
        # key. Preserve their delivery outcome instead of resending on upgrade.
        legacy = digest([key, recipient['connector_id'], recipient['chat_id'], recipient['open_id']])
        row = self.db.one("SELECT * FROM review_notifications WHERE dedupe_key=? "
                          "AND message_type='text' AND app_id=''", (legacy,))
        if row or not plan:
            return row
        # A missing-check-in reminder follows one case/stage in its original
        # message as evidence arrives. Ordinary completed proposals keep their
        # existing content-based deduplication and legacy delivery outcomes.
        return self.db.one(
            "SELECT n.* FROM review_notifications n JOIN platform_review_batches r ON r.id=n.review_id "
            "WHERE n.case_id=? AND n.stage=? AND n.purpose='checkin_gap' AND n.message_type='interactive' "
            "AND n.connector_id=? AND n.chat_id=? AND n.open_id=? AND n.app_id=? "
            "AND json_extract(r.snapshot,'$.link.report_receiving_id')=? "
            "AND r.state NOT IN ('submitting','submitted','verified','uncertain','saved','save_verified') "
            'ORDER BY n.created_at DESC,n.id DESC LIMIT 1',
            (plan['case']['id'], plan['stage'], recipient['connector_id'], recipient['chat_id'],
             recipient['open_id'], recipient['app_id'], plan['link']['report_receiving_id']))

    def message(self, plan, recipient):
        case = plan['case']
        checkins = pending_checkins(plan)
        lines = [
            f'<at user_id="{recipient["open_id"]}">{plain(recipient["name"])}</at>',
            f'{plain(case["project_name"])} · {plain(plan["label"])}资料' + ('待补齐' if checkins else '待核对'),
            '案件：' + plain(case.get('short_code') or case.get('platform_report_code') or '编号待补'),
        ]
        if case.get('platform_report_code'):
            lines.append('报修编号：' + plain(case['platform_report_code']))
        if case.get('title'):
            lines.append('事项：' + plain(case['title'], 300))
        files = [f'{plain(f["label"])} {len(f["existing"])} 项' for f in plan['file_fields'] if f['existing']]
        if files:
            lines.append('已保存：' + '；'.join(files))
        if checkins:
            lines.append('已核对官网本环节附件，待补：' + '、'.join(plain(item['label']) for item in checkins) + '。')
            lines.append('本环节主要资料已保存，请补齐打卡照片；补齐并核对后才能正式提交。')
        elif plan.get('pending_fields'):
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
            existing = self.existing(key, recipient, plan)
            if (existing and existing['purpose'] == 'checkin_gap' and existing['proposal_key'] != key
                    and existing['state'] == 'sent' and not existing['pending_action_id'] and self.cards):
                with self.db.tx() as con:
                    con.execute('UPDATE review_notifications SET proposal_key=?,dedupe_key=?,official_checked_at=? WHERE id=?',
                                (key, dedupe, plan['link']['verified_at'], existing['id']))
                existing = self.db.one('SELECT * FROM review_notifications WHERE id=?', (existing['id'],))
                return self.cards.renew(existing, row['review_id'])
            replace_queued = (existing and existing['purpose'] == 'checkin_gap'
                              and existing['state'] == 'queued' and existing['proposal_key'] != key)
            if existing and existing['state'] != 'cancelled' and not replace_queued:
                if self.cards and existing['message_type'] == 'interactive' and existing['state'] == 'sent' and not existing['pending_action_id']:
                    view = json.loads(existing['card_view'])
                    if view.get('mode') == 'stale' or (view.get('review_state') == 'save_verified' and existing['review_id'] != row['review_id']):
                        self.cards.renew(existing, row['review_id'])
                return existing
            content = self.message(plan, recipient)
            with self.db.tx() as con:
                if existing:
                    con.execute("UPDATE review_notifications SET state='queued',review_id=?,content=?,dedupe_key=?,"
                                "proposal_key=?,error=NULL,retry_at=0 WHERE id=?",
                                (row['review_id'], content, dedupe, key, existing['id']))
                    delivery_id = existing['id']
                else:
                    delivery_id = uid()
                    con.execute('''INSERT INTO review_notifications(
                        id,dedupe_key,case_id,stage,project_id,review_id,proposal_key,
                        connector_id,open_id,name,chat_id,chat_name,content,created_at,purpose)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (delivery_id, dedupe, case_id, row['stage'], plan['case']['project_id'], row['review_id'], key,
                         recipient['connector_id'], recipient['open_id'], recipient['name'],
                         recipient['chat_id'], recipient['chat_name'], content, now(),
                         'checkin_gap' if pending_checkins(plan) else 'review'))
            delivery = self.db.one('SELECT * FROM review_notifications WHERE id=?', (delivery_id,))
            return self.cards.initialize(delivery, plan) if self.cards else delivery

    def status_for(self, upload):
        if upload['state'] != 'awaiting_confirmation' or not upload.get('review_id'):
            return None
        if not self.enabled():
            return {'state': 'disabled', 'message': '飞书核对提醒已关闭'}
        try:
            record = self.reviews.get(upload['review_id'])
            recipient = self.recipient(record['snapshot']['case']['project_id'])
            row = self.existing(proposal_key(record['snapshot']), recipient, record['snapshot'])
            if row:
                return {**{k: row[k] for k in ('id', 'state', 'error', 'name', 'chat_name', 'sent_at', 'purpose', 'official_checked_at')},
                        'error': row['error'] or row['card_error'] or None}
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

    def _verify_official_checkins(self, delivery):
        """Do a fresh official read immediately before a gap reminder is sent."""
        if delivery['purpose'] != 'checkin_gap':
            return True
        from .project_accounts import case_context
        case_id = delivery['case_id']
        try:
            with case_context(self.platform, case_id):
                self.automatic.workflow.refresh_case(case_id)
                detail = self.platform.link(case_id)['snapshot'].get(delivery['stage']) or {}
                if any(not isinstance(detail.get(key), list)
                       for key in ('buildingFileInfoList', 'houseFileInfoList')):
                    raise DomainError('官网未返回可核实的本环节打卡附件列表', 502)
        except Exception as error:
            # An unavailable website is not evidence of missing attachments.
            with self.db.tx() as con:
                con.execute('UPDATE review_notifications SET error=?,retry_at=? WHERE id=?',
                            ('暂无法核对官网本环节附件，补资料提醒尚未发送；稍后自动重试。',
                             time.time() + 60, delivery['id']))
            if not isinstance(error, DomainError):
                log.exception('Official check-in verification failed for %s', delivery['id'])
            return False
        with self.db.tx() as con:
            con.execute('UPDATE review_notifications SET official_checked_at=?,error=NULL,retry_at=0 WHERE id=?',
                        (now(), delivery['id']))
        try:
            self._matches(delivery)
        except DomainError:
            # The fresh read may have found check-ins absent from the cache, or
            # an already-completed stage. Let the normal workflow rebuild first.
            with self.db.tx() as con:
                con.execute("UPDATE automatic_upload_cases SET signature='',retry_at=? WHERE case_id=?",
                            (time.time() + 1, case_id))
            raise DomainError('官网本环节资料已更新，旧补资料提醒已取消，正在重新核对。', 409)
        return True

    def deliver(self, delivery_id):
        # Share the contact-edit lock, then the material lock (same order as photo deletion).
        with self._lock, self.followup._send_lock, self.platform.material_lock:
            delivery = self.db.one('SELECT * FROM review_notifications WHERE id=?', (delivery_id,))
            if not delivery or delivery['state'] != 'queued' or delivery['retry_at'] > time.time():
                return
            client, sent_id, started = None, None, False
            try:
                self._matches(delivery)
                client = self.followup.client(delivery['connector_id'])
                if not any(p['open_id'] == delivery['open_id'] for p in client.members(delivery['chat_id'])):
                    raise DomainError('项目负责人已不在群中，请重新绑定。')
                # Membership lookup may take time; recheck readiness immediately before sending.
                self._matches(delivery)
                if self.cards and delivery['message_type'] == 'interactive':
                    delivery = self.cards.prepare_send(delivery, client)
                if not self._verify_official_checkins(delivery):
                    return
                self._matches(delivery)
                if self._stop.is_set():
                    return
                with self.db.tx() as con:
                    con.execute("UPDATE review_notifications SET state='sending',error=NULL WHERE id=?", (delivery_id,))
                started = True
                send = client.send_card if delivery['message_type'] == 'interactive' else client.send_feedback
                sent_id = send(delivery['chat_id'], delivery['content'], delivery_id)
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
                con.execute("UPDATE review_notifications SET state='queued',error=NULL,retry_at=0 WHERE id=?", (delivery_id,))
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

    @background_work('notifications.scan')
    def run_once(self):
        if self.cards and not self._stop.is_set():
            self.cards.run_once()
        if self._stop.is_set() or not self._lock.acquire(blocking=False):
            return 0
        try:
            if not self.enabled():
                self._queue_checks.clear()
                if self.db.one("SELECT id FROM review_notifications WHERE state='queued' LIMIT 1"):
                    with self.db.tx() as con:
                        con.execute("UPDATE review_notifications SET state='cancelled',error='飞书核对提醒已暂停' WHERE state='queued'")
                return 0
            observed, stamp = self._signal.token(), time.monotonic()
            due = not self._queue_checks or any(stamp - checked >= QUEUE_RECHECK_SECONDS for _, checked in self._queue_checks.values())
            if observed != self._poll_token or stamp - self._poll_at >= 15 or due:
                self._poll_token, self._poll_at = observed, stamp
                candidates = self.db.all("SELECT case_id FROM automatic_upload_cases WHERE enabled=1 AND state='awaiting_confirmation'")
                case_ids = {row['case_id'] for row in candidates}
                self._queue_checks = {key: value for key, value in self._queue_checks.items() if key in case_ids}
                for row in candidates:
                    try:
                        self._queue_if_changed(row['case_id'])
                    except DomainError:
                        continue
            else:
                self.db.runtime.count('notifications.scan', 'idle_skips')
            deliveries = self.db.all("SELECT id FROM review_notifications WHERE state='queued' AND retry_at<=? ORDER BY created_at LIMIT 10", (time.time(),))
            for row in deliveries:
                if self._stop.is_set():
                    break
                self.deliver(row['id'])
            self._retry_at = self.db.one("SELECT MIN(retry_at) due FROM review_notifications WHERE state='queued'")['due'] or 0
            return len(deliveries)
        finally:
            self._lock.release()

    def recover(self):
        self._queue_checks.clear()
        with self.db.tx() as con:
            con.execute("UPDATE review_notifications SET state='unknown',error='发送时服务中断，请先核对飞书群内记录' WHERE state='sending'")
        if self.cards:
            self.cards.recover()

    def start(self):
        if self._thread:
            return
        self.recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='idi-review-notifications')
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._signal.wake(persist=False)
        if self._thread:
            self._thread.join(timeout=1)
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            observed = self._signal.token()
            try:
                self.run_once()
            except Exception:
                log.exception('Proposal notification queue failed')
            delay = min(15, self._retry_at - time.time() if self._retry_at else 15)
            if self.cards:
                delay = min(delay, self.cards.next_delay())
            self._signal.wait(observed, max(.1, delay), self._stop)
