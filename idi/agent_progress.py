"""Read dated evidence of case activity without treating sync timestamps as work."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from .agent_dates import parsed
from .db import dump
from .domain import DomainError, WORKFLOW_STAGES


def progress_evidence(owner, args, project_id=None, case=None):
    from .agent_capabilities import date_bounds, page_result

    start, end = (parsed(value) for value in date_bounds(args))
    if not start or not end:
        raise DomainError('请提供进展查询的日期范围，或使用 date_period（例如 today）')
    as_of = owner.dates.current
    projects = [project_id] if project_id else owner.project_ids
    cases = {c['id']: c for c in owner.domain.cases() if c['id'] in owner.case_refs
             and c['project_id'] in projects and (not case or c['id'] == case['id'])}
    ids = dump(sorted(cases))
    changes, pending = defaultdict(list), []
    unknown_dates = 0

    def in_period(value, recorded_at):
        nonlocal unknown_dates
        instant, recorded = parsed(value), parsed(recorded_at)
        if instant is None or recorded is None:
            unknown_dates += 1
            return False
        return start <= instant < end and instant <= as_of and recorded <= as_of

    def add(case_id, kind, label, at, **details):
        changes[case_id].append({'kind': kind, 'label': label,
                                'evidence_at': parsed(at).isoformat(), **details})

    # Initial imports and unchanged readbacks only establish a baseline. A real
    # transition discovered by a readback has an observation time, not a known
    # business occurrence time.
    events = owner.db.all('SELECT * FROM case_events WHERE case_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,id', (ids,))
    for event in events:
        if not event['from_stage'] or event['from_stage'] == event['to_stage']:
            continue
        observed = event['source'] in ('platform', 'platform_list')
        if not observed and event['source'] != 'manual_record':
            continue
        at = event['created_at'] if observed else event['event_at']
        if in_period(at, event['created_at']):
            add(event['case_id'], 'workflow_change_observed' if observed else 'handling_record',
                '官网同步发现环节变化' if observed else '人工登记办理记录', at,
                source=event['source'], time_basis='observed_at' if observed else 'recorded_business_time',
                occurred_at=None if observed else event['event_at'], recorded_at=event['created_at'],
                from_stage=event['from_stage'], to_stage=event['to_stage'],
                from_label=WORKFLOW_STAGES.get(event['from_stage'], event['from_stage']),
                to_label=WORKFLOW_STAGES.get(event['to_stage'], event['to_stage']))

    photos = owner.db.all("SELECT p.case_id,p.project_id,p.photo_group,p.created_at FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id IN (SELECT value FROM json_each(?)) AND b.media_type='image' AND p.reuse_source_id IS NULL", (ids,))
    photo_groups = defaultdict(list)
    for photo in photos:
        if photo['project_id'] != cases[photo['case_id']]['project_id']:
            continue
        if in_period(photo['created_at'], photo['created_at']):
            photo_groups[photo['case_id']].append(photo)
    for case_id, received in photo_groups.items():
        times = sorted(parsed(p['created_at']).isoformat() for p in received)
        add(case_id, 'photos_archived', '新增归档照片', times[-1], count=len(received),
            first_archived_at=times[0], source='local_archive', time_basis='archived_at',
            groups=dict(Counter(p['photo_group'] for p in received)),
            note='归档时间不代表拍摄或施工时间；未据照片认定完工或验收。')

    visits = owner.db.all("SELECT case_id,stage,started_at,visit_date,created_at FROM visits WHERE case_id IN (SELECT value FROM json_each(?)) AND basis='manual'", (ids,))
    for visit in visits:
        at = visit['started_at'] or visit['visit_date']
        if in_period(at, visit['created_at']):
            add(visit['case_id'], 'manual_visit', '人工登记进场', at, source='manual_visit',
                time_basis='recorded_visit_time', stage=visit['stage'], recorded_at=visit['created_at'],
                note='人工登记的进场时间，不能据此认定完工或验收。')

    receipts = owner.db.all('SELECT case_id,stage,state,save_only,result,confirmed_at,created_at FROM platform_review_batches WHERE case_id IN (SELECT value FROM json_each(?))', (ids,))
    for receipt in receipts:
        try:
            result = json.loads(receipt['result'])
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict) or receipt['save_only'] or result.get('action') != 'submit':
            continue
        # finished_at is rewritten by reconciliation. The confirmation timestamp
        # keeps an old submission from becoming "today's submission" on refresh.
        if not in_period(receipt['confirmed_at'], receipt['created_at']):
            continue
        readback = parsed(result.get('readback_at'))
        if receipt['state'] == 'verified' and not result.get('mismatches') and readback and readback <= as_of:
            add(receipt['case_id'], 'formal_submission_verified', '正式提交已回读核实', receipt['confirmed_at'],
                source='verified_submission_receipt', time_basis='submission_confirmed_at',
                stage=receipt['stage'], readback_at=result.get('readback_at'))
        elif receipt['state'] in ('submitting', 'submitted', 'uncertain', 'verified'):
            pending.append({'case_ref': owner.case_refs[receipt['case_id']], 'stage': receipt['stage'],
                            'state': receipt['state'], 'confirmed_at': receipt['confirmed_at'],
                            'label': '提交结果或查询时点内的回读证据仍待核实，不计入已完成提交'})

    items, counts, case_counts = [], Counter(), defaultdict(set)
    for case_id, evidence in changes.items():
        current = owner.case(owner.case_refs[case_id])
        for entry in evidence:
            counts[entry['kind']] += entry.get('count', 1)
            case_counts[entry['kind']].add(case_id)
        evidence.sort(key=lambda value: value['evidence_at'], reverse=True)
        items.append({'case_ref': owner.case_refs[case_id],
            **{k: current.get(k) for k in ('project_name', 'short_code', 'platform_report_code', 'title', 'building', 'unit', 'room')},
            'evidence': evidence[:20], 'evidence_total': len(evidence), 'evidence_has_more': len(evidence) > 20,
            'latest_evidence_at': evidence[0]['evidence_at']})
    items.sort(key=lambda item: (item['latest_evidence_at'], item['case_ref']), reverse=True)
    result = page_result(items, args, 10)
    kinds = ('workflow_change_observed', 'handling_record', 'photos_archived', 'manual_visit', 'formal_submission_verified')
    result.update(summary={'cases_with_evidence': len(items),
        'categories': {kind: {'count': counts[kind], 'case_count': len(case_counts[kind])} for kind in kinds},
        'categories_may_overlap': True}, unknown_date_count=unknown_dates,
        pending_submissions=pending[:20], pending_submission_count=len(pending),
        identifier_labels={'platform_report_code': '报修编号', 'short_code': '系统短编号'},
        freshness={'live_official_query': False, 'as_of': as_of.isoformat()},
        scope='仅当前授权范围内有事件证据的案件；按人工登记业务时间、照片归档时间、提交确认时间或官网变化发现时间分别统计。纯台账刷新、首次同步入库、重复使用的照片副本、保存草稿均不算业务阶段推进。',
        interpretation='官网发现环节变化不代表当天实际办理；归档照片不代表当天拍摄或施工。不同类别可能属于同一案件，不可相加为案件总数。0条只表示当前记录未找到相应证据，不能断言现场没有进展。')
    return result
