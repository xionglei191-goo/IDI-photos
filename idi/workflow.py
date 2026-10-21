from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime

from .case_codes import issue_code, unique_prefix
from .db import audit, dump, now, rowdict, uid
from .domain import CASE_PLATFORM_COLUMNS, DomainError, PHOTO_GROUPS, WORKFLOW_STAGES, case_filters, clean_date, decode_case
from .platform_client import unsubmitted_report
from .platform_photos import attachment_removal_history, online_photo_counts
from .stage_access import FORM_LABELS, STAGE_FORM, stage_access

INTAKE = {'pending_intake', 'insurer_intake', 'insurer_review', 'historical_unverified'}
ARCHIVED = {'closed', 'direct_payment', 'noncoverage'}
STATUS_LEGACY = {'pending_intake': 'pending', 'insurer_intake': 'pending', 'insurer_review': 'pending', 'historical_unverified': 'pending', 'survey': 'survey', 'repair': 'working',
                 'acceptance': 'completed', 'settlement': 'accepted', 'closed': 'closed', 'direct_payment': 'closed', 'noncoverage': 'closed'}


def remote_stage(snapshot):
    """Interpret the observed acceptance branch, preserving incomplete old rows."""
    row = {**snapshot.get('claim', {}), **snapshot.get('row', {})}
    flow, passed, result = (str(row[k]) if row.get(k) is not None else '' for k in ('flowStatus', 'passStatus', 'acceptanceResult'))
    try:
        report_date = datetime.strptime(str(row.get('reportDate', ''))[:10], '%Y-%m-%d').date()
    except ValueError:
        report_date = None
    # The cutoff describes an observed data cohort, not a confirmed test era.
    # An actual repair-list member or an acceptance result takes precedence.
    if report_date and report_date < datetime(2026, 9, 24).date() and not result.strip() and snapshot.get('scope') != 'repair':
        return 'historical_unverified'
    if flow == '3':
        return 'insurer_review'
    if flow == '0':
        return 'insurer_intake'
    if flow == '1':
        return 'pending_intake'
    if passed == '1' and result in ('2', '3'):
        return 'direct_payment' if result == '2' else 'noncoverage'
    if str(row.get('repairMethod') or '') == '4':
        return 'noncoverage'
    if unsubmitted_report(row, snapshot.get('scope')):
        return 'pending_intake'
    if snapshot.get('scope') == 'repair' or flow == '2' or (flow == '9' and result == '1'):
        # Intake flowStatus=9 ends the intake subprocess. It does not close a
        # case that is present in the official survey/repair list.
        repair_flow = str(row.get('repairAcceptanceFlowStatus') or '')
        if repair_flow == '9':
            return 'closed'
        if repair_flow in ('1', '2'):
            return {'1': 'acceptance', '2': 'settlement'}[repair_flow]
        if str(row.get('repairMethod') or '') == '3' and str(row.get('siteSurveyPassStatus') or '') != '2':
            return 'survey'
        if str(row.get('siteSurveyState') or '') != '1':
            return 'survey'
        return 'repair'
    if flow == '9':
        return 'closed'
    return None


def address_fields(value):
    value = str(value or '').strip()
    match = re.search(r'([A-Za-z0-9]+)(?:栋|幢|号楼)(?:[-\s]*([A-Za-z0-9]+)单元)?[-\s]*(\d{2,5})(?:房|室|号|户)?', value)
    if not match:
        match = re.search(r'(?<![A-Za-z0-9])([A-Za-z0-9]+)-(?:([A-Za-z0-9]+)-)?(\d{2,5})(?![A-Za-z0-9])', value)
    if match:
        return {'building': match[1], 'unit': match[2] or '', 'room': match[3], 'location_detail': value}
    return {'location_detail': value}


class Workflow:
    def __init__(self, db, domain, platform, jobs=None):
        self.db, self.domain, self.platform, self.jobs = db, domain, platform, jobs
        self._sync_lock = threading.Lock()
        from .platform_sync import PlatformSync
        self.synchronizer = PlatformSync(self)

    def sync_status(self):
        from .identity import current
        value = self.synchronizer.status()
        if current() and not current().admin:
            return {'lists': bool(value.get('lists')), 'state': value.get('state', '')}
        return value

    def sync(self, payload, progress):
        return self.synchronizer.run(payload, progress)

    def publish_lists(self, con, lists, run_id, read_at):
        """Publish only a fully read, fully linked pair of official lists."""
        if set(lists) != {'intake', 'repair'}:
            raise DomainError('官网清单不完整，本次未更新看板范围', 409)
        entries = []
        for scope, rows in lists.items():
            seen = set()
            for index, row in enumerate(rows):
                record_id = row['reportReceivingId']
                if record_id in seen:
                    raise DomainError('官网清单含重复记录，本次未更新看板范围', 409)
                seen.add(record_id)
                linked = con.execute('SELECT case_id FROM platform_links WHERE report_receiving_id=? AND verified_at IS NOT NULL', (record_id,)).fetchone()
                if not linked:
                    raise DomainError('官网清单仍有案件未完成回读，本次未更新看板范围', 409)
                entries.append((scope, record_id, linked['case_id'], index, dump(row), run_id, read_at))
        con.execute('DELETE FROM platform_list_entries')
        con.executemany('INSERT INTO platform_list_entries VALUES(?,?,?,?,?,?,?)', entries)
        con.execute("INSERT INTO settings VALUES('platform_lists',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (dump({'run_id': run_id, 'read_at': read_at, 'coverage': 'full', 'counts': {k: len(v) for k, v in lists.items()}}),))

    def apply_snapshot(self, snapshot, case_id=None, *, details_read=True):
        row = {**snapshot.get('claim', {}), **snapshot.get('row', {})}
        record_id = str(row.get('reportReceivingId') or '')
        code = str(row.get('reportCode') or '').strip()
        project_name = str(row.get('communityName') or row.get('engName') or '').strip()
        if not record_id or not code or not project_name:
            raise DomainError('官网案件缺少报修编号或项目名称，未自动建立案件', 409)
        stage = remote_stage(snapshot)
        if stage is None:
            raise DomainError('官网流程状态尚未识别，保留原案件进度等待核对', 409)
        stamp = now()
        read_at = snapshot.get('fetched_at') or stamp
        with self.db.tx() as con:
            current_link = con.execute('SELECT case_id,verified_at,snapshot FROM platform_links WHERE report_receiving_id=?', (record_id,)).fetchone()
            current_snapshot = json.loads(current_link['snapshot']) if current_link else {}
            latest_read = max((current_link['verified_at'] or '') if current_link else '', current_snapshot.get('list_verified_at') or '')
            if latest_read > read_at:
                current = con.execute('SELECT workflow_stage FROM cases WHERE id=?', (current_link['case_id'],)).fetchone()
                return {'case_id': current_link['case_id'], 'created': False, 'stage': current['workflow_stage'], 'stale_ignored': True}
            linked = con.execute('SELECT case_id FROM platform_links WHERE report_receiving_id=?', (record_id,)).fetchone()
            by_code = con.execute('SELECT id FROM cases WHERE platform_report_code=?', (code,)).fetchone()
            matches = {v for v in (case_id, linked['case_id'] if linked else None, by_code['id'] if by_code else None) if v}
            if len(matches) > 1:
                raise DomainError('官网记录与报修编号分别关联了不同案件，需先核对对应关系', 409)
            case_id = next(iter(matches), None)
            previous = rowdict(con.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()) if case_id else None
            created = previous is None
            location = address_fields(row.get('communityDesc'))
            title = str(row.get('accidentDesc') or row.get('issueDescription') or '报修内容待核对').strip()[:2000]
            insurer = str(row.get('insuraceCompany') or '')
            if not previous:
                project = con.execute('SELECT id FROM projects WHERE name=?', (project_name,)).fetchone()
                if project:
                    project_id = project['id']
                else:
                    project_id = uid()
                    con.execute('INSERT INTO projects(id,name,aliases,insurer,created_at,updated_at,short_prefix) VALUES(?,?,?,?,?,?,?)',
                                (project_id, project_name, '[]', insurer, stamp, stamp, unique_prefix(con, project_name, project_id)))
                case_id = uid()
                values = {'id': case_id, 'project_id': project_id, 'platform_report_code': code,
                          'policy_number': str(row.get('policyNum') or ''), 'report_number': str(row.get('reportNumber') or '') or None,
                          'title': title, 'insurer': insurer, 'contact_name': str(row.get('reportContact') or ''),
                          'contact_phone': str(row.get('reportContactPhone') or ''), **location,
                          'workflow_stage': stage, 'workflow_source': 'platform', 'workflow_verified_at': read_at,
                          'remote_status': dump({k: row.get(k) for k in ('flowStatus', 'passStatus', 'siteSurveyState', 'repairAcceptanceFlowStatus', 'interceptFlag')}),
                          'remote_updated_at': read_at, 'status': STATUS_LEGACY[stage], 'created_at': stamp, 'updated_at': stamp}
                values['short_code'] = issue_code(con, values)
                con.execute('INSERT INTO cases(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            else:
                # Keep local edits and all local drafts; fill missing identity fields only.
                values = {'platform_report_code': code, 'workflow_stage': stage, 'workflow_source': 'platform',
                          'workflow_verified_at': read_at, 'remote_updated_at': read_at,
                          'remote_status': dump({k: row.get(k) for k in ('flowStatus', 'passStatus', 'siteSurveyState', 'repairAcceptanceFlowStatus', 'interceptFlag')}),
                          'status': STATUS_LEGACY[stage], 'updated_at': stamp, 'revision': previous['revision'] + 1}
                fills = {'policy_number': str(row.get('policyNum') or ''), 'report_number': str(row.get('reportNumber') or '') or None,
                         'insurer': insurer, **location}
                for key, value in fills.items():
                    if not previous.get(key) and value:
                        values[key] = value
                if not previous.get('short_code'):
                    values['short_code'] = issue_code(con, {**previous, **values})
                con.execute('UPDATE cases SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), case_id))
            link = con.execute('SELECT * FROM platform_links WHERE case_id=?', (case_id,)).fetchone()
            if link and link['report_receiving_id'] and link['report_receiving_id'] != record_id:
                raise DomainError('本案已关联另一个官网记录，未覆盖', 409)
            if not details_read:
                # List status is fresh, but cached form fields and attachments were
                # not fetched. Keep their original verification time and contents.
                cached = json.loads(link['snapshot']) if link else {}
                snapshot = {**cached, 'row': snapshot['row'], 'scope': snapshot['scope'],
                            'account': snapshot.get('account', ''), 'list_verified_at': read_at}
            elif link:
                previous_snapshot = json.loads(link['snapshot'])
                removed = attachment_removal_history(previous_snapshot, snapshot, read_at)
                snapshot = {k: v for k, v in snapshot.items() if k != 'removed_attachments'}
                if removed:
                    snapshot['removed_attachments'] = removed
            verified_at = read_at if details_read else (link['verified_at'] if link else None)
            semantic = lambda value: {k: v for k, v in value.items() if k not in ('fetched_at', 'list_verified_at', 'account')}
            link_changed = not link or semantic(json.loads(link['snapshot'])) != semantic(snapshot)
            con.execute('''INSERT INTO platform_links VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET
                report_receiving_id=excluded.report_receiving_id,platform_report_code=excluded.platform_report_code,
                insurer_report_number=excluded.insurer_report_number,snapshot=excluded.snapshot,verified_at=excluded.verified_at,
                revision=excluded.revision,updated_at=excluded.updated_at''',
                        (case_id, record_id, code, str(row.get('reportNumber') or ''), dump(snapshot), verified_at,
                         (link['revision'] if link else 0) + int(link_changed), stamp))
            if created or previous['workflow_stage'] != stage:
                self._event(con, case_id, 'platform_readback' if details_read else 'platform_list_sync', previous['workflow_stage'] if previous else None, stage,
                            'platform' if details_read else 'platform_list', {'report_receiving_id': record_id, 'remote_state': values['remote_status']}, read_at)
            audit(con, 'case', case_id, 'platform_sync' if details_read else 'platform_list_sync', before={'workflow_stage': previous['workflow_stage']} if previous else None,
                  after={'workflow_stage': stage, 'report_receiving_id': record_id, 'remote_written': False}, actor='platform_readback' if details_read else 'platform_list')
        return {'case_id': case_id, 'created': created, 'stage': stage}

    def _event(self, con, case_id, kind, old, stage, source, detail, event_at):
        con.execute('INSERT INTO case_events VALUES(?,?,?,?,?,?,?,?,?)', (uid(), case_id, kind, old, stage, source, dump(detail), event_at, now()))

    def record_result(self, case_id, data):
        case = self.domain.case(case_id)
        if case['workflow_source'] == 'platform' or self.platform.link(case_id)['verified_at']:
            raise DomainError('已关联官网的案件由官网回读更新进度，请刷新官网结果', 409)
        action = data.get('action')
        transitions = {'accepted': ('pending_intake', 'survey'),
                       'submitted_direct_payment': ('pending_intake', 'insurer_review'),
                       'submitted_noncoverage': ('pending_intake', 'insurer_review'),
                       'direct_payment_approved': ('insurer_review', 'direct_payment'),
                       'noncoverage_approved': ('insurer_review', 'noncoverage'),
                       'insurance_returned': ('insurer_review', 'pending_intake'), 'survey_completed': ('survey', 'repair'),
                       'repair_completed': ('repair', 'acceptance'), 'acceptance_completed': ('acceptance', 'settlement'),
                       'archived': ('settlement', 'closed')}
        if action not in transitions or transitions[action][0] != case['workflow_stage']:
            raise DomainError('当前进度不允许此办理结果，不能跳过当前办理环节', 409)
        if action in ('direct_payment_approved', 'noncoverage_approved'):
            branch = self.db.one("SELECT kind FROM case_events WHERE case_id=? AND to_stage='insurer_review' ORDER BY created_at DESC,rowid DESC LIMIT 1", (case_id,))
            expected = {'direct_payment_approved': 'submitted_direct_payment', 'noncoverage_approved': 'submitted_noncoverage'}[action]
            if not branch or branch['kind'] != expected:
                raise DomainError('保司复核结果与此前受理分支不一致，请核对办理凭据', 409)
        note = str(data.get('note') or '').strip()
        event_at = clean_date(data.get('event_at'), '办理时间')
        if not note or len(note) > 2000 or not event_at:
            raise DomainError('请填写实际办理时间和外部办理凭据或说明')
        stage = transitions[action][1]
        with self.db.tx() as con:
            current = con.execute('SELECT revision,workflow_stage FROM cases WHERE id=?', (case_id,)).fetchone()
            if data.get('revision') != current['revision']:
                raise DomainError('案件已变化，请刷新后核对', 409)
            con.execute('UPDATE cases SET workflow_stage=?,status=?,workflow_source=?,workflow_verified_at=?,revision=revision+1,updated_at=? WHERE id=?',
                        (stage, STATUS_LEGACY[stage], 'manual_record', event_at, now(), case_id))
            self._event(con, case_id, action, current['workflow_stage'], stage, 'manual_record', {'note': note}, event_at)
        return self.domain.case(case_id)

    def detail(self, case_id):
        case = self.domain.case(case_id)
        events = self.db.all('SELECT * FROM case_events WHERE case_id=? ORDER BY created_at DESC,id DESC', (case_id,))
        for event in events:
            event['detail'] = json.loads(event['detail'])
        return {'case': case, 'events': events, 'official_read_at': self.platform.link(case_id)['verified_at'], 'stage_label': WORKFLOW_STAGES[case['workflow_stage']],
                'form_stage': STAGE_FORM.get(case['workflow_stage'], 'intake'), 'watermark': self.watermark(case),
                'stage_access': {stage: stage_access(case, stage) for stage in FORM_LABELS},
                'insurer': self.domain.completeness(case_id, case['insurer_template_id']),
                'reviews': self.db.all('SELECT id,stage,state,created_at,confirmed_at,finished_at FROM platform_review_batches WHERE case_id=? ORDER BY created_at DESC', (case_id,))}

    def watermark(self, case):
        area = '-'.join(v for v in (case['building'] + '栋' if case['building'] else '', case['unit'] + '单元' if case['unit'] else '', case['room'] + '号' if case['room'] else case['location_detail']) if v)
        return {'text': f"编号：{case['short_code'] or '待补楼栋房号后生成'}\n施工项目：{case['project_name']}\n施工区域：{area}\n施工内容：查勘打卡",
                'contents': ['查勘打卡', 'XXX问题', '施工打卡', 'XXX施工', 'XXX完工']}

    def board(self, board='intake', project_id='', building='', query='', status='', archived=False, page=1, page_size=30, source='auto'):
        # Every nested form/material read shares this request's snapshot. Nothing
        # is cached across requests, so edits are visible on the next refresh.
        with self.db.read_snapshot():
            return self._board(board, project_id, building, query, status, archived, page, page_size, source)

    def _board(self, board, project_id, building, query, status, archived, page, page_size, source):
        if board not in ('intake', 'work'):
            raise DomainError('看板不存在', 404)
        if source not in ('auto', 'platform', 'local'):
            raise DomainError('案件来源筛选无效')
        if source == 'auto':
            source = 'platform' if self.db.setting('platform_lists') else 'workflow'
        scope = 'intake' if board == 'intake' else 'repair'
        where, values = case_filters(project_id, query)
        source_sql = ' FROM cases c JOIN projects pr ON pr.id=c.project_id'
        ordering = 'c.updated_at DESC'
        if building:
            where.append('c.building=?')
            values.append(building)
        if source == 'platform':
            source_sql += ' JOIN platform_list_entries e ON e.case_id=c.id AND e.scope=?'
            values.insert(0, scope)
            ordering = 'e.position,c.updated_at DESC'
            from .identity import project_filter
            access_scope, access_args = project_filter('c.project_id')
            source_total = self.db.one('SELECT COUNT(*) AS n FROM platform_list_entries e JOIN cases c ON c.id=e.case_id WHERE e.scope=? AND ' + access_scope, (scope, *access_args))['n']
        else:
            if source == 'local':
                where.append('NOT EXISTS(SELECT 1 FROM platform_links l WHERE l.case_id=c.id AND l.verified_at IS NOT NULL)')
            where.append('c.workflow_stage ' + ('IN' if board == 'intake' else 'NOT IN') + ' (' + ','.join('?' for _ in INTAKE) + ')')
            values.extend(sorted(INTAKE))
            if not archived:
                where.append('c.workflow_stage NOT IN (' + ','.join('?' for _ in ARCHIVED) + ')')
                values.extend(sorted(ARCHIVED))
        filtered_sql = source_sql + (' WHERE ' + ' AND '.join(where) if where else '')
        counters = {r['workflow_stage']: r['n'] for r in self.db.all(
            'SELECT c.workflow_stage,COUNT(*) AS n' + filtered_sql + ' GROUP BY c.workflow_stage', values)}
        if source != 'platform':
            source_total = sum(counters.values())
        if status:
            filtered_sql += (' AND ' if where else ' WHERE ') + 'c.workflow_stage=?'
            values.append(status)
        total = counters.get(status, 0) if status else sum(counters.values())
        page, page_size = max(1, page), min(100, max(1, page_size))
        pool = [decode_case(r) for r in self.db.all(
            'SELECT c.*,pr.name AS project_name,(SELECT COUNT(*) FROM photos p WHERE p.case_id=c.id) AS photo_count'
            + CASE_PLATFORM_COLUMNS + filtered_sql + ' ORDER BY ' + ordering + ' LIMIT ? OFFSET ?',
            [*values, page_size, (page - 1) * page_size])]
        case_ids = [c['id'] for c in pool]
        placeholders = ','.join('?' for _ in case_ids) or 'NULL'
        counts = {}
        for row in self.db.all(f"SELECT p.case_id,p.photo_group,COUNT(*) AS count FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id IN ({placeholders}) AND b.media_type='image' AND p.recognition_state<>'conflict' GROUP BY p.case_id,p.photo_group", case_ids):
            counts.setdefault(row['case_id'], {})[row['photo_group']] = row['count']
        legacy_counts = {r['case_id']: r['count'] for r in self.db.all(f"SELECT p.case_id,COUNT(*) AS count FROM visit_shared_checkins s JOIN photos p ON p.id=s.photo_id JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id IN ({placeholders}) AND b.media_type='image' AND p.recognition_state<>'conflict' AND NOT EXISTS(SELECT 1 FROM photos copy WHERE copy.reuse_source_id=p.id) GROUP BY p.case_id", case_ids)}
        shared_counts = {r['case_id']: r['count'] for r in self.db.all(f"SELECT case_id,COUNT(*) AS count FROM photos WHERE case_id IN ({placeholders}) AND reuse_source_id IS NOT NULL AND photo_group='work_checkin' AND recognition_state<>'conflict' GROUP BY case_id", case_ids)}
        for case_id, count in legacy_counts.items():
            counts.setdefault(case_id, {})['work_checkin'] = counts.get(case_id, {}).get('work_checkin', 0) + count
            shared_counts[case_id] = shared_counts.get(case_id, 0) + count
        visit_counts = {r['case_id']: r['count'] for r in self.db.all(f"SELECT case_id,COUNT(*) AS count FROM visits WHERE case_id IN ({placeholders}) AND stage='work' GROUP BY case_id", case_ids)}
        exceptions = {r['case_id']: r['count'] for r in self.db.all(f"SELECT p.case_id,COUNT(*) AS count FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id IN ({placeholders}) AND b.media_type='image' AND (p.photo_group='unknown' OR p.recognition_state IN ('failed','conflict')) GROUP BY p.case_id", case_ids)}
        items = []
        for case in pool:
            stage = case['workflow_stage']
            form = 'intake' if board == 'intake' else STAGE_FORM.get(stage, 'intake')
            plan = self.platform.preview(case['id'], form)
            snapshot = plan['link']['snapshot']
            reported_at = ({**snapshot.get('claim', {}), **snapshot.get('row', {})}.get('reportDate')
                           if plan['link']['report_receiving_id'] else case.get('report_at'))
            try:
                report_date = datetime.strptime(str(reported_at or '')[:10], '%Y-%m-%d').date().isoformat()
            except ValueError:
                report_date = None
            insurer = self.domain.completeness(case['id'], case['insurer_template_id'])
            applicable_stages = {'survey'} if stage in INTAKE | {'survey'} else {'survey', 'work', 'acceptance'}
            active_missing = sum(i['missing'] > 0 for i in insurer['items'] if i['stage'] in applicable_stages)
            review = self.db.one('SELECT state FROM platform_review_batches WHERE case_id=? AND stage=? ORDER BY created_at DESC LIMIT 1', (case['id'], form))
            platform_status = '待关联' if not plan['link']['verified_at'] else '待审核' if plan['prepared_for_review'] else f"待补{plan['error_count']}项"
            if stage == 'historical_unverified':
                platform_status, next_action = '历史待核实', '查看历史记录'
            elif stage == 'insurer_intake':
                platform_status, next_action = '待保司处理', '等待保司处理'
            elif unsubmitted_report(plan['link']['snapshot'].get('row', {}), plan['link']['snapshot'].get('scope')):
                platform_status = '报案待提交'
                next_action = '官网报案待提交'
            elif stage == 'insurer_review':
                next_action = '等待保司复核'
            elif board == 'intake' and stage != 'pending_intake':
                next_action = '查看受理记录'
            elif exceptions.get(case['id']):
                next_action = '确认照片归类'
            elif stage in ARCHIVED | {'settlement'}:
                next_action = '查看办理结果'
            elif plan['prepared_for_review']:
                next_action = '审核并提交' if not review or review['state'] not in ('submitted', 'verified') else '刷新官网结果'
            elif stage == 'pending_intake':
                next_action = '办理受理'
            else:
                next_action = {'survey': '整理查勘资料', 'repair': '整理施工资料', 'acceptance': '补齐验收资料'}[stage]
            items.append({**case, 'stage_label': WORKFLOW_STAGES[stage], 'form_stage': form, 'report_date': report_date,
                          'action_label': ('查看报案' if next_action == '官网报案待提交' else '受理' if board == 'intake' else '处理') if plan['access']['editable'] else '查看',
                          'photo_counts': {key: counts.get(case['id'], {}).get(key, 0) for key in PHOTO_GROUPS},
                          'shared_checkin_count': shared_counts.get(case['id'], 0), 'work_visit_count': visit_counts.get(case['id'], 0),
                          'online_photos': online_photo_counts(plan['link']),
                          'exception_count': exceptions.get(case['id'], 0), 'platform_status': platform_status,
                          'platform_missing': plan['error_count'], 'insurer_missing': active_missing,
                          'insurer_status': f'待补{active_missing}项' if active_missing else '已备齐', 'next_action': next_action})
        return {'items': items, 'total': total, 'page': page, 'page_size': page_size, 'counts': counters,
                'source': source, 'source_total': source_total, 'photo_groups': PHOTO_GROUPS, 'sync': self.sync_status()}

    def refresh_case(self, case_id, check_permission=None):
        if check_permission:
            check_permission()
        link = self.platform.link(case_id)
        if not link['report_receiving_id']:
            raise DomainError('请先关联官网案件')
        scope = link['snapshot'].get('scope', 'intake')
        try:
            snapshot = self.platform.client.snapshot(link['report_receiving_id'], scope)
        except DomainError as error:
            if error.status != 404:
                raise
            snapshot = self.platform.client.snapshot(link['report_receiving_id'], 'repair' if scope == 'intake' else 'intake')
        # Accepted intake records remain in the intake list. Read repair details
        # explicitly instead of waiting for the intake record to disappear.
        row = {**snapshot.get('claim', {}), **snapshot.get('row', {})}
        if snapshot.get('scope') == 'intake' and str(row.get('acceptanceResult')) == '1' and str(row.get('flowStatus')) in ('2', '9'):
            try:
                snapshot = self.platform.client.snapshot(link['report_receiving_id'], 'repair')
            except DomainError as error:
                if error.status != 404:
                    raise
        if check_permission:
            check_permission()
            current_link = self.platform.link(case_id)
            if current_link['report_receiving_id'] != link['report_receiving_id'] or current_link['revision'] != link['revision']:
                raise DomainError('官网关联已变化，请重新查询后刷新', 409)
        result = self.apply_snapshot(snapshot, case_id)
        self.synchronizer.record_single(snapshot, result['case_id'])
        return self.detail(case_id)
