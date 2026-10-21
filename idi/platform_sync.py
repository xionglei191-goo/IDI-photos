"""Persistent, bounded official reads. No business writes or photo downloads."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from .db import dump, now, uid
from .domain import DomainError
from .platform_client import PlatformBudgetReached, PlatformReadPaused
from .workflow import remote_stage

CHINA = timezone(timedelta(hours=8))
DEFAULTS = dict(enabled=True, hour=8, minute=0, recent_days=2, request_interval=1.0,
                batch_requests=300, daily_requests=1000, photo_requests=100, photo_interval_hours=24)
log = logging.getLogger(__name__)


def instant(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class PlatformSync:
    def __init__(self, workflow):
        self.workflow, self.db = workflow, workflow.db
        self._stop = threading.Event()
        self._thread = None
        self._dispatch = threading.RLock()
        self.clock = lambda: datetime.now(timezone.utc)

    @property
    def client(self):
        return self.workflow.platform.client

    def policy(self):
        return {**DEFAULTS, **self.db.setting('platform_sync_policy', {})}

    def configure(self, data):
        if set(data) != {'enabled'} or type(data['enabled']) is not bool:
            raise DomainError('请提供自动更新开关')
        self.db.set_setting('platform_sync_policy', {**self.policy(), **data})
        return self.status()

    def status(self):
        latest = self.db.one("SELECT * FROM platform_sync_runs WHERE mode<>'single' ORDER BY started_at DESC,rowid DESC LIMIT 1")
        full = self.db.one("SELECT * FROM platform_sync_runs WHERE mode='full' AND state='completed' ORDER BY finished_at DESC LIMIT 1")
        daily = self.db.one("SELECT * FROM platform_sync_runs WHERE mode='incremental' ORDER BY started_at DESC,rowid DESC LIMIT 1")
        policy, session = self.policy(), self.client.status()
        today = self.clock().astimezone(CHINA).date().isoformat()
        usage = self.db.one('SELECT * FROM platform_sync_usage WHERE account=? AND day=?', (session.get('account', ''), today)) or {}
        for run in (latest, full, daily):
            if run:
                run['details'] = json.loads(run['details'])
                checkpoint = json.loads(run.pop('checkpoint'))
                run['phase'] = checkpoint.get('phase')
                run['date_from'], run['date_to'] = checkpoint.get('date_from'), checkpoint.get('date_to')
        return {'latest': latest, 'last_complete': full, 'last_incremental': daily,
                'lists': self.db.setting('platform_lists'),
                'schedule': {**policy, 'timezone': 'Asia/Shanghai', 'connected': session['connected'],
                             'today_requests': usage.get('requests', 0), 'today_photo_requests': usage.get('photo_requests', 0),
                             'last_success': self.db.setting('platform_incremental_success'),
                             'photo_backlog': self.photo_backlog()}}

    def photo_backlog(self):
        cutoff = (self.clock() - timedelta(hours=self.policy()['photo_interval_hours'])).isoformat()
        return self.db.one('''SELECT COUNT(*) AS n FROM platform_list_entries e
            JOIN platform_links l ON l.case_id=e.case_id JOIN cases c ON c.id=e.case_id
            JOIN platform_repair_activity a ON a.report_receiving_id=e.report_receiving_id
            WHERE e.scope='repair' AND a.active=1
            AND c.workflow_stage NOT IN ('settlement','closed','direct_payment','noncoverage')
            AND (l.verified_at IS NULL OR l.verified_at<?)''', (cutoff,))['n']

    def _create(self, mode, session, refresh_details=True, when=None):
        current = when or self.clock()
        day = current.astimezone(CHINA).date()
        start = day - timedelta(days=self.policy()['recent_days'] - 1)
        last = self.db.setting('platform_incremental_success')
        if last and last.get('account') == session.get('account'):
            start = min(start, datetime.fromisoformat(last['day']).date())
        elif mode == 'incremental':
            baseline = self.db.one("SELECT started_at FROM platform_sync_runs WHERE mode='full' AND state='completed' AND account=? ORDER BY finished_at DESC LIMIT 1", (session.get('account', ''),))
            if baseline:
                start = min(start, instant(baseline['started_at']).astimezone(CHINA).date())
        checkpoint = dict(phase='lists', scans={}, target_day=day.isoformat(), date_from=start.isoformat(),
                          date_to=day.isoformat(), refresh_details=refresh_details, created=0, updated=0, photos_checked=0)
        run_id = uid()
        with self.db.tx() as con:
            con.execute('INSERT INTO platform_sync_runs(id,state,account,started_at,mode,checkpoint) VALUES(?,?,?,?,?,?)',
                        (run_id, 'queued', session.get('account', ''), current.isoformat(), mode, dump(checkpoint)))
        return run_id

    def enqueue(self, mode='full', resume_id=None, when=None):
        with self._dispatch:
            session = self.client.status()
            if not session['connected']:
                raise DomainError('请先连接官网再同步台账', 401)
            # A manual full request must not silently become an automatic partial scan.
            for job in self.db.all("SELECT id,payload FROM jobs WHERE kind='platform_sync' AND state IN ('queued','running')"):
                payload = json.loads(job['payload'])
                if payload.get('mode', 'full') == mode or payload.get('run_id') == resume_id and resume_id:
                    return self.workflow.jobs.get(job['id'])
            if mode == 'full' and not resume_id:
                unfinished = self.db.one("SELECT id FROM platform_sync_runs WHERE mode='full' AND account=? AND state='partial' AND retry_at IS NOT NULL ORDER BY started_at DESC LIMIT 1", (session.get('account', ''),))
                resume_id = unfinished['id'] if unfinished else None
            run_id = resume_id or self._create(mode, session, when=when)
            if resume_id:
                run = self.db.one('SELECT account,mode FROM platform_sync_runs WHERE id=?', (run_id,))
                if not run or run['account'] != session.get('account') or run['mode'] != mode:
                    raise DomainError('同步账号或任务已变化，不能接续此批次', 409)
            return self.workflow.jobs.enqueue('platform_sync', {'mode': mode, 'run_id': run_id, 'refresh_details': mode == 'full'})

    def tick(self, when=None):
        with self._dispatch:
            current, policy = when or self.clock(), self.policy()
            session = self.client.status()
            if not session['connected'] or self.db.one("SELECT id FROM jobs WHERE kind='platform_sync' AND state IN ('queued','running')"):
                return None
            # Explicit manual batches resume even when the daily schedule is paused.
            pending = self.db.one("""SELECT * FROM platform_sync_runs WHERE account=? AND mode IN ('full','incremental')
                AND state IN ('partial','running','queued') AND (retry_at IS NOT NULL OR state IN ('running','queued'))
                ORDER BY CASE mode WHEN 'full' THEN 0 ELSE 1 END,started_at LIMIT 1""", (session.get('account', ''),))
            if pending and (pending['mode'] == 'full' or policy['enabled']):
                if pending['retry_at'] and instant(pending['retry_at']) > current:
                    return None
                return self.enqueue(pending['mode'], pending['id'], when=current)
            local = current.astimezone(CHINA)
            if not policy['enabled'] or (local.hour, local.minute) < (policy['hour'], policy['minute']):
                return None
            day = local.date().isoformat()
            existing = self.db.one("""SELECT id FROM platform_sync_runs WHERE mode='incremental' AND account=?
                AND json_extract(checkpoint,'$.target_day')=? LIMIT 1""", (session.get('account', ''), day))
            if existing:
                return None
            return self.enqueue('incremental', when=current)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='idi-platform-schedule', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception('Official update scheduler failed')
            self._stop.wait(30)

    def _checkpoint(self, run_id, checkpoint):
        with self.db.tx() as con:
            con.execute('UPDATE platform_sync_runs SET checkpoint=? WHERE id=?', (dump(checkpoint), run_id))

    def _session(self, expected):
        if self._stop.is_set():
            raise PlatformReadPaused('服务正在停止，已保留同步进度', 30)
        if self.client.status() != expected:
            raise DomainError('官网连接在同步期间发生变化，请重新连接后继续', 401)

    def _scan(self, run, checkpoint, scope, session):
        """Each completed page is durable; resumed pages first verify the head."""
        state = checkpoint['scans'].setdefault(scope, {'page': 1, 'complete': False})
        if state['complete']:
            return
        filters = {}
        if run['mode'] == 'incremental':
            filters = ({'report_start': checkpoint['date_from'], 'report_end': checkpoint['date_to']}
                       if scope == 'intake' else {'end_status': '0'})
        def query(page):
            self._session(session)
            return self.client.search(page=page, scope=scope, page_size=40, **filters)
        def fingerprint(items):
            return hashlib.sha256(dump(items).encode()).hexdigest()
        def reset():
            with self.db.tx() as con:
                con.execute('DELETE FROM platform_sync_rows WHERE run_id=? AND scope=?', (run['id'], scope))
            state.clear()
            state.update(page=1, complete=False)
            self._checkpoint(run['id'], checkpoint)
        head = None
        if state['page'] > 1:
            head = query(1)
            if head['total'] != state['total'] or fingerprint(head['items']) != state['head']:
                reset()
            else:
                head = None
        while not state['complete']:
            result = head if head is not None else query(state['page'])
            head = None
            if result['total'] > 100000:
                raise DomainError('官网清单超过本次核验上限，未标为同步完成', 409)
            if state['page'] == 1:
                state.update(total=result['total'], head=fingerprint(result['items']), started_at=now())
            if result['total'] != state['total']:
                reset()
                raise PlatformReadPaused('官网清单在分页期间变化，稍后重新核对本清单', 300)
            count = self.db.one('SELECT COUNT(*) AS n FROM platform_sync_rows WHERE run_id=? AND scope=?', (run['id'], scope))['n']
            ids = [r['reportReceivingId'] for r in result['items']]
            duplicate = self.db.one('SELECT report_receiving_id FROM platform_sync_rows WHERE run_id=? AND scope=? AND report_receiving_id IN (SELECT value FROM json_each(?)) LIMIT 1', (run['id'], scope, dump(ids)))
            if len(set(ids)) != len(ids) or duplicate or count + len(ids) > state['total'] or not ids and count != state['total']:
                reset()
                raise DomainError('官网分页重复或数量不完整，未更新清单范围', 409)
            self._session(session)
            state['page'] += 1
            state['complete'] = count + len(ids) == state['total']
            with self.db.tx() as con:
                con.executemany('INSERT INTO platform_sync_rows VALUES(?,?,?,?,?)',
                    [(run['id'], scope, row['reportReceivingId'], count + i, dump(row)) for i, row in enumerate(result['items'])])
                con.execute('UPDATE platform_sync_runs SET checkpoint=? WHERE id=?', (dump(checkpoint), run['id']))

    def _plan(self, run, checkpoint):
        merged = {}
        for scope in ('intake', 'repair'):
            for value in self.db.all('SELECT * FROM platform_sync_rows WHERE run_id=? AND scope=? ORDER BY position', (run['id'], scope)):
                row = json.loads(value['snapshot'])
                previous = merged.get(value['report_receiving_id'])
                if previous and str(previous[1].get('flowStatus')) == '3' and str(row.get('flowStatus')) not in ('2', '9'):
                    continue
                merged[value['report_receiving_id']] = (scope, row, 'full' if run['mode'] == 'full' and checkpoint['refresh_details'] else 'changed')
        if run['mode'] == 'incremental':
            active_ids = {v['report_receiving_id'] for v in self.db.all("SELECT report_receiving_id FROM platform_sync_rows WHERE run_id=? AND scope='repair'", (run['id'],))}
            for value in self.db.all("SELECT e.* FROM platform_list_entries e JOIN platform_repair_activity a ON a.report_receiving_id=e.report_receiving_id WHERE e.scope='repair' AND a.active=1"):
                if value['report_receiving_id'] not in active_ids:
                    merged[value['report_receiving_id']] = ('repair', json.loads(value['snapshot']), 'missing')
        checkpoint['phase'] = 'details'
        with self.db.tx() as con:
            con.executemany('INSERT OR IGNORE INTO platform_sync_items(run_id,report_receiving_id,scope,snapshot,action) VALUES(?,?,?,?,?)',
                [(run['id'], key, scope, dump(row), action) for key, (scope, row, action) in merged.items()])
            con.execute('UPDATE platform_sync_runs SET total=?,checkpoint=? WHERE id=?', (len(merged), dump(checkpoint), run['id']))

    @staticmethod
    def _list_snapshot(row, scope, previous, session, read_at):
        # Omitted list fields must not erase the last observed acceptance state.
        prior_row = previous.get('row', {})
        merged = {**prior_row, **row}
        prior_flow = str(prior_row.get('repairAcceptanceFlowStatus') or '')
        if prior_flow in ('2', '9') and str(row.get('repairAcceptanceFlowStatus')) not in ('0', '1', '2', '9'):
            merged['repairAcceptanceFlowStatus'] = prior_row['repairAcceptanceFlowStatus']
        return {**previous, 'row': merged, 'scope': scope,
                'account': session.get('account', ''), 'fetched_at': read_at}

    @staticmethod
    def _list_only(snapshot):
        return remote_stage(snapshot) in ('settlement', 'closed')

    def _item(self, item, session, run, checkpoint):
        self._session(session)
        record_id, scope, row = item['report_receiving_id'], item['scope'], json.loads(item['snapshot'])
        link = self.db.one('SELECT * FROM platform_links WHERE report_receiving_id=?', (record_id,))
        previous = json.loads(link['snapshot']) if link else {}
        if previous.get('account') and previous['account'] != session.get('account'):
            raise DomainError('案件快照属于另一个官网账号，需人工核对后更新', 409)
        remove_repair = False
        read_at = checkpoint.get('scans', {}).get(scope, {}).get('started_at') or now()
        if item['action'] == 'missing':
            row = self.client.lookup_row(record_id, 'repair')
            if row is None:
                row, scope = self.client.lookup_row(record_id, 'intake'), 'intake'
                remove_repair = row is not None
            if row is None:
                raise DomainError('案件暂未在官网两张清单中找到，保留原记录等待核对', 409)
            if scope == 'repair' and str(row.get('endStatus')) == '0':
                raise PlatformReadPaused('未归档维修清单与单案查询不一致，稍后核对', 300)
            read_at = now()
        elif scope == 'intake' and previous.get('scope') == 'repair':
            repair_row = self.client.lookup_row(record_id, 'repair')
            if repair_row:
                row, scope = repair_row, 'repair'
            read_at = now()
        list_snapshot = self._list_snapshot(row, scope, previous, session, read_at)
        list_only = run['mode'] == 'incremental' and self._list_only(list_snapshot)
        changed = (previous.get('row') != (list_snapshot['row'] if list_only else row)
                   or previous.get('scope') != scope or (not previous.get('claim') and not list_only))
        if changed or item['action'] in ('full', 'missing'):
            scan_time = checkpoint.get('scans', {}).get(scope, {}).get('started_at')
            if item['action'] != 'missing' and scan_time and self.clock() - instant(scan_time) > timedelta(minutes=15):
                fresh = self.client.lookup_row(record_id, scope)
                if fresh is None:
                    raise DomainError('本案在分页后已变化，需重新核对官网清单', 409)
                row = fresh
                list_snapshot = self._list_snapshot(row, scope, previous, session, now())
                list_only = run['mode'] == 'incremental' and self._list_only(list_snapshot)
            snapshot = list_snapshot if list_only else self.client.snapshot_from_row(row, scope)
            self._session(session)
            result = self.workflow.apply_snapshot(snapshot, details_read=not list_only)
            checkpoint['created' if result['created'] else 'updated'] += 1
            case_id = result['case_id']
        else:
            case_id = link['case_id']
        with self.db.tx() as con:
            con.execute("UPDATE platform_sync_items SET state='done',scope=?,snapshot=?,case_id=?,error=NULL,action=? WHERE run_id=? AND report_receiving_id=?",
                (scope, dump(row), case_id, 'missing_intake' if remove_repair else item['action'], run['id'], record_id))
            con.execute('UPDATE platform_sync_runs SET checkpoint=? WHERE id=?', (dump(checkpoint), run['id']))

    @staticmethod
    def _upsert(con, scope, row, case_id, run_id, read_at, position=None):
        if position is None:
            old = con.execute('SELECT position FROM platform_list_entries WHERE scope=? AND report_receiving_id=?', (scope, row['reportReceivingId'])).fetchone()
            position = old['position'] if old else con.execute('SELECT COALESCE(MAX(position),-1)+1 FROM platform_list_entries WHERE scope=?', (scope,)).fetchone()[0]
        con.execute('''INSERT INTO platform_list_entries VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,report_receiving_id) DO UPDATE SET
            case_id=excluded.case_id,position=excluded.position,snapshot=excluded.snapshot,sync_run_id=excluded.sync_run_id,synced_at=excluded.synced_at''',
            (scope, row['reportReceivingId'], case_id, position, dump(row), run_id, read_at))

    def _publish(self, run, checkpoint):
        rows = self.db.all('SELECT * FROM platform_sync_rows WHERE run_id=? ORDER BY scope,position', (run['id'],))
        items = {v['report_receiving_id']: v for v in self.db.all('SELECT * FROM platform_sync_items WHERE run_id=?', (run['id'],))}
        if any(v['state'] != 'done' for v in items.values()):
            raise DomainError('仍有案件未核对，本批清单未发布', 409)
        stamp = now()
        with self.db.tx() as con:
            if run['mode'] == 'full':
                list_read_at = min((v.get('started_at', stamp) for v in checkpoint['scans'].values()), default=stamp)
                self.workflow.publish_lists(con, {scope: [json.loads(v['snapshot']) for v in rows if v['scope'] == scope] for scope in ('intake', 'repair')}, run['id'], list_read_at)
                con.execute("UPDATE platform_repair_activity SET active=0,checked_at=?,source='full_absence' WHERE report_receiving_id NOT IN (SELECT report_receiving_id FROM platform_sync_rows WHERE run_id=? AND scope='repair')", (stamp, run['id']))
                for value in rows:
                    if value['scope'] == 'repair':
                        row = json.loads(value['snapshot'])
                        con.execute('INSERT OR IGNORE INTO platform_repair_activity VALUES(?,?,1,?,?)', (value['report_receiving_id'], items[value['report_receiving_id']]['case_id'], stamp, 'baseline'))
                        if row.get('endStatus') is not None:
                            con.execute("UPDATE platform_repair_activity SET active=?,checked_at=?,source='full_flag' WHERE report_receiving_id=?", (int(str(row['endStatus']) == '0'), stamp, value['report_receiving_id']))
            else:
                for value in rows:
                    item = items[value['report_receiving_id']]
                    self._upsert(con, value['scope'], json.loads(value['snapshot']), item['case_id'], run['id'], stamp)
                    if value['scope'] == 'repair':
                        con.execute("INSERT INTO platform_repair_activity VALUES(?,?,1,?,'filtered_list') ON CONFLICT(report_receiving_id) DO UPDATE SET case_id=excluded.case_id,active=1,checked_at=excluded.checked_at,source=excluded.source", (value['report_receiving_id'], item['case_id'], stamp))
                for item in items.values():
                    if item['action'] in ('missing', 'missing_intake'):
                        con.execute("UPDATE platform_repair_activity SET active=0,checked_at=?,source='confirmed_absence' WHERE report_receiving_id=?", (stamp, item['report_receiving_id']))
                    if item['action'] == 'missing_intake':
                        con.execute("DELETE FROM platform_list_entries WHERE scope='repair' AND report_receiving_id=?", (item['report_receiving_id'],))
                    # Targeted confirmation may find a newly archived repair record.
                    if item['action'] in ('missing', 'missing_intake') or item['scope'] == 'repair':
                        self._upsert(con, item['scope'], json.loads(item['snapshot']), item['case_id'], run['id'], stamp)
                previous = con.execute("SELECT value FROM settings WHERE key='platform_lists'").fetchone()
                value = json.loads(previous['value']) if previous else {}
                counts = {scope: con.execute('SELECT COUNT(*) FROM platform_list_entries WHERE scope=?', (scope,)).fetchone()[0] for scope in ('intake', 'repair')}
                value.update(coverage='incremental', incremental_at=stamp, counts=counts, recent_range=[checkpoint['date_from'], checkpoint['date_to']])
                con.execute("INSERT INTO settings VALUES('platform_lists',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dump(value),))
                con.execute("INSERT INTO settings VALUES('platform_incremental_success',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (dump({'account': run['account'], 'day': checkpoint['date_to'], 'completed_at': stamp}),))
            checkpoint['phase'] = 'photos' if run['mode'] == 'incremental' else 'done'
            con.execute('UPDATE platform_sync_runs SET checkpoint=? WHERE id=?', (dump(checkpoint), run['id']))

    def _photos(self, run, checkpoint, session, outer_budget, account_request):
        policy = self.policy()
        day = self.clock().astimezone(CHINA).date().isoformat()
        usage = self.db.one('SELECT * FROM platform_sync_usage WHERE account=? AND day=?', (run['account'], day)) or {}
        available = min(policy['photo_requests'] - usage.get('photo_requests', 0), outer_budget['limit'] - outer_budget['used'])
        cutoff = (self.clock() - timedelta(hours=policy['photo_interval_hours'])).isoformat()
        candidates = self.db.all('''SELECT e.report_receiving_id,l.verified_at FROM platform_list_entries e
            JOIN platform_links l ON l.case_id=e.case_id JOIN cases c ON c.id=e.case_id
            JOIN platform_repair_activity a ON a.report_receiving_id=e.report_receiving_id
            WHERE e.scope='repair' AND a.active=1
            AND c.workflow_stage NOT IN ('settlement','closed','direct_payment','noncoverage')
            AND (l.verified_at IS NULL OR l.verified_at<?)
            ORDER BY CASE WHEN EXISTS(SELECT 1 FROM photos p WHERE p.case_id=e.case_id AND p.updated_at>COALESCE(l.verified_at,'')) THEN 0 ELSE 1 END,
                     COALESCE(l.verified_at,''),e.report_receiving_id LIMIT ?''', (cutoff, max(0, available // 5)))
        with self.client.request_budget(max(0, available), account_request) as budget:
            for candidate in candidates:
                if budget['limit'] - budget['used'] < 5:
                    break
                self._session(session)
                link = self.db.one('SELECT l.*,c.workflow_stage FROM platform_links l JOIN cases c ON c.id=l.case_id WHERE report_receiving_id=?', (candidate['report_receiving_id'],))
                if not link or link['workflow_stage'] in ('settlement', 'closed', 'direct_payment', 'noncoverage'):
                    continue
                row = self.client.lookup_row(candidate['report_receiving_id'], 'repair')
                if row is None:
                    raise DomainError('此记录未在当前账号的对应列表中找到，请重新查询并选择案件', 404)
                previous = json.loads(link['snapshot'])
                if previous.get('account') and previous['account'] != session.get('account'):
                    raise DomainError('案件快照属于另一个官网账号，需人工核对后更新', 409)
                read_at = now()
                snapshot = self._list_snapshot(row, 'repair', previous, session, read_at)
                list_only = self._list_only(snapshot)
                if not list_only:
                    snapshot = self.client.snapshot_from_row(row, 'repair')
                self._session(session)
                result = self.workflow.apply_snapshot(snapshot, details_read=not list_only)
                with self.db.tx() as con:
                    self._upsert(con, 'repair', row, result['case_id'], run['id'], read_at)
                if not list_only:
                    checkpoint['photos_checked'] += 1
                self._checkpoint(run['id'], checkpoint)
        checkpoint['photo_pending'] = self.photo_backlog()
        checkpoint['phase'] = 'done'
        self._checkpoint(run['id'], checkpoint)

    def run(self, payload, progress):
        if not self.workflow._sync_lock.acquire(blocking=False):
            raise DomainError('官网台账正在同步，请等待本次完成', 409)
        try:
            return self._run(payload, progress)
        finally:
            self.workflow._sync_lock.release()

    def _run(self, payload, progress):
        session = self.client.status()
        if not session['connected']:
            raise DomainError('请先连接官网再同步台账', 401)
        mode = payload.get('mode', 'full')
        if mode not in ('full', 'incremental'):
            raise DomainError('官网同步方式无效')
        run_id = payload.get('run_id') or self._create(mode, session, payload.get('refresh_details', True))
        run = self.db.one('SELECT * FROM platform_sync_runs WHERE id=?', (run_id,))
        if not run or run['account'] != session.get('account') or run['mode'] != mode:
            raise DomainError('同步账号或方式不匹配，未继续读取官网', 409)
        if run['state'] == 'completed':
            return json.loads(run['details'])
        if run['state'] in ('cancelled', 'superseded'):
            return {'state': 'partial', 'error': '此批次已取消或已由完整对账覆盖', 'remote_written': False}
        checkpoint, policy = json.loads(run['checkpoint']), self.policy()
        day = self.clock().astimezone(CHINA).date().isoformat()
        usage = self.db.one('SELECT * FROM platform_sync_usage WHERE account=? AND day=?', (run['account'], day)) or {}
        limit = policy['batch_requests']
        if mode == 'incremental':
            limit = min(limit, max(0, policy['daily_requests'] - usage.get('requests', 0)))
        # Mock transports keep their zero-delay setting; production uses the policy.
        if self.client.request_interval:
            self.client.request_interval = policy['request_interval']
        with self.db.tx() as con:
            con.execute("UPDATE platform_sync_runs SET state='running',retry_at=NULL,error=NULL WHERE id=?", (run_id,))
            con.execute("UPDATE platform_sync_items SET state='pending',error=NULL WHERE run_id=? AND state='error'", (run_id,))
        def account_request():
            self._session(session)
            current_day = self.clock().astimezone(CHINA).date().isoformat()
            with self.db.tx() as con:
                con.execute('UPDATE platform_sync_runs SET request_count=request_count+1 WHERE id=?', (run_id,))
                con.execute('''INSERT INTO platform_sync_usage VALUES(?,?,1,?) ON CONFLICT(account,day)
                    DO UPDATE SET requests=requests+1,photo_requests=photo_requests+excluded.photo_requests''',
                    (run['account'], current_day, int(checkpoint['phase'] == 'photos')))
        state, error, retry_at = 'completed', None, None
        try:
            with self.client.request_budget(limit, account_request) as budget:
                if checkpoint['phase'] == 'lists':
                    for scope in ('intake', 'repair'):
                        self._scan(run, checkpoint, scope, session)
                    if mode == 'incremental' and not checkpoint.get('repair_filter_verified'):
                        # Repair rows may omit endStatus: check the opposite filter
                        # instead of inventing an archive flag on returned records.
                        opposite = self.client.search(scope='repair', page_size=40, end_status='1')
                        active = {v['report_receiving_id'] for v in self.db.all("SELECT report_receiving_id FROM platform_sync_rows WHERE run_id=? AND scope='repair'", (run_id,))}
                        if active.intersection(v['reportReceivingId'] for v in opposite['items']):
                            raise DomainError('官网已归档与未归档筛选结果重叠，未发布本次增量清单，请核对官网筛选', 409)
                        checkpoint['repair_filter_verified'] = True
                        self._checkpoint(run_id, checkpoint)
                    self._plan(run, checkpoint)
                if checkpoint['phase'] == 'details':
                    items = self.db.all('SELECT * FROM platform_sync_items WHERE run_id=? ORDER BY rowid', (run_id,))
                    progress(sum(v['state'] == 'done' for v in items), len(items))
                    consecutive = 0
                    for index, item in enumerate(items, 1):
                        if item['state'] == 'done':
                            continue
                        try:
                            self._item(item, session, run, checkpoint)
                            consecutive = 0
                        except (PlatformReadPaused, PlatformBudgetReached):
                            raise
                        except Exception as exc:
                            if isinstance(exc, DomainError) and exc.status in (401, 403):
                                raise
                            with self.db.tx() as con:
                                con.execute("UPDATE platform_sync_items SET state='error',error=? WHERE run_id=? AND report_receiving_id=?", (str(exc)[:350], run_id, item['report_receiving_id']))
                            consecutive += 1
                            if consecutive >= 3:
                                raise DomainError('连续三个案件核验失败，已停止后续请求，请核对错误', 409)
                        progress(index, len(items))
                    self._session(session)
                    self._publish(run, checkpoint)
                if checkpoint['phase'] == 'photos':
                    self._photos(run, checkpoint, session, budget, account_request)
        except PlatformBudgetReached as exc:
            state, error = 'partial', str(exc)
            today_used = self.db.one('SELECT requests FROM platform_sync_usage WHERE account=? AND day=?', (run['account'], day)) or {'requests': 0}
            resume = self.clock() + timedelta(seconds=60)
            if mode == 'incremental' and today_used['requests'] >= policy['daily_requests']:
                resume = (self.clock().astimezone(CHINA) + timedelta(days=1)).replace(hour=policy['hour'], minute=policy['minute'], second=0, microsecond=0)
            retry_at = resume.astimezone(timezone.utc).isoformat()
        except PlatformReadPaused as exc:
            state, error, retry_at = 'partial', str(exc), (self.clock() + timedelta(seconds=exc.retry_after)).isoformat()
        except Exception as exc:
            state, error = 'partial', str(exc)[:1000]
            if isinstance(exc, DomainError) and exc.status in (401, 403):
                retry_at = (self.clock() + timedelta(seconds=60)).isoformat()
        errors = self.db.all("SELECT report_receiving_id AS record_id,error FROM platform_sync_items WHERE run_id=? AND state='error'", (run_id,))
        counts = {scope: info.get('total', 0) for scope, info in checkpoint['scans'].items()}
        total = self.db.one('SELECT total,request_count FROM platform_sync_runs WHERE id=?', (run_id,))
        result = {'state': state, 'mode': mode, 'run_id': run_id, 'total': total['total'], 'created': checkpoint['created'], 'updated': checkpoint['updated'],
                  'errors': errors, 'error': error, 'retry_at': retry_at, 'request_count': total['request_count'],
                  'scopes': [{'scope': scope, 'archived': 'all' if mode == 'full' or scope == 'intake' else '0', 'count': n} for scope, n in counts.items()],
                  'list_counts': counts, 'date_from': checkpoint['date_from'], 'date_to': checkpoint['date_to'],
                  'photos_checked': checkpoint['photos_checked'], 'photo_pending': checkpoint.get('photo_pending', self.photo_backlog()), 'remote_written': False}
        with self.db.tx() as con:
            con.execute('''UPDATE platform_sync_runs SET state=?,finished_at=?,created_count=?,updated_count=?,error=?,details=?,checkpoint=?,retry_at=? WHERE id=?''',
                (state, now(), checkpoint['created'], checkpoint['updated'], error, dump(result), dump(checkpoint), retry_at, run_id))
            if state == 'completed':
                con.execute('DELETE FROM platform_sync_rows WHERE run_id=?', (run_id,))
                con.execute('DELETE FROM platform_sync_items WHERE run_id=?', (run_id,))
                if mode == 'full':
                    older = [v['id'] for v in con.execute("SELECT id FROM platform_sync_runs WHERE id<>? AND account=? AND started_at<=? AND state IN ('partial','queued','running')", (run_id, run['account'], run['started_at']))]
                    for old_id in older:
                        con.execute("UPDATE platform_sync_runs SET state='superseded',retry_at=NULL,error='已由更新的完整对账覆盖' WHERE id=?", (old_id,))
                        con.execute("UPDATE jobs SET state='failed',error='此批次已由完整对账覆盖' WHERE kind='platform_sync' AND state='queued' AND json_extract(payload,'$.run_id')=?", (old_id,))
                        con.execute('DELETE FROM platform_sync_rows WHERE run_id=?', (old_id,))
                        con.execute('DELETE FROM platform_sync_items WHERE run_id=?', (old_id,))
        return result

    def record_single(self, snapshot, case_id):
        """Keep list membership current after an explicit, verified single-case read."""
        scope, row = snapshot.get('scope'), snapshot.get('row', {})
        if scope not in ('intake', 'repair') or not row.get('reportReceivingId'):
            return
        stamp, run_id = snapshot.get('fetched_at') or now(), uid()
        with self.db.tx() as con:
            con.execute('INSERT INTO platform_sync_runs(id,state,account,started_at,finished_at,total,mode) VALUES(?,?,?,?,?,1,?)',
                (run_id, 'completed', snapshot.get('account', ''), stamp, stamp, 'single'))
            self._upsert(con, scope, row, case_id, run_id, stamp)
            if scope == 'repair':
                con.execute("INSERT OR IGNORE INTO platform_repair_activity VALUES(?,?,1,?,'single')", (row['reportReceivingId'], case_id, stamp))
            previous = con.execute("SELECT value FROM settings WHERE key='platform_lists'").fetchone()
            value = json.loads(previous['value']) if previous else {}
            value.update(coverage='incremental', single_at=stamp,
                counts={s: con.execute('SELECT COUNT(*) FROM platform_list_entries WHERE scope=?', (s,)).fetchone()[0] for s in ('intake', 'repair')})
            con.execute("INSERT INTO settings VALUES('platform_lists',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dump(value),))
