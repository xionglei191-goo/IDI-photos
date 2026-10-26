"""Complete follow-up totals with bounded, explicit project pagination."""
from collections import Counter

from .agent_dates import parsed
from .context_memory import token_estimate
from .db import now
from .domain import WORKFLOW_STAGES


def followup_summary(owner, args):
    # Keep a single snapshot for the totals and project rows. Detailed changes
    # belong to read_case_progress/read_project_summary, not this overview.
    with owner.db.read_snapshot():
        cases = owner.domain.cases()
        rows, stages = [], Counter()
        for ref, project in owner.projects.items():
            project_cases = [case for case in cases if case['project_id'] == project['id']]
            eligible = [case for case in project_cases if case['followup_eligible']]
            counts = Counter(WORKFLOW_STAGES.get(case['workflow_stage'], case['workflow_stage']) for case in eligible)
            stages.update(counts)
            rows.append({'project_ref': ref, 'project_name': project['name'],
                         'case_count': len(eligible), 'all_case_count': len(project_cases),
                         'stages': dict(counts),
                         'local_photo_count': sum(case.get('photo_count', 0) for case in eligible)})
    totals = {'project_count': len(rows), 'followup_project_count': sum(row['case_count'] > 0 for row in rows),
              'case_count': sum(row['case_count'] for row in rows),
              'all_case_count': sum(row['all_case_count'] for row in rows),
              'local_photo_count': sum(row['local_photo_count'] for row in rows), 'stages': dict(stages)}
    totals['excluded_case_count'] = totals['all_case_count'] - totals['case_count']
    result = {'totals': totals, 'totals_complete': True, 'projects': [], 'total': len(rows),
              'page': 1, 'page_size': 20, 'has_more': False, 'next_page': None,
              'queried_at': parsed(now()).isoformat(timespec='seconds'), 'timezone': 'Asia/Shanghai',
              'scope': '当前对话全部获授权项目；数据来自本机台账与最近官网同步，查询不刷新官网。'
                       '查勘维修按案件清单归属及本地办理阶段筛选，不是权限模式或项目开关。',
              'instruction': 'totals 是完整总数，projects 是分页项目明细，含跟进案件为0的项目。'
                             'has_more 为真时按 next_page 和返回的 page_size 继续查询，不能把本页项目数当成总数。'
                             '进展事件用 read_case_progress，单项目资料概况用 read_project_summary。'
                             'queried_at 是读取本机台账的北京时间，不能称为官网同步完成时间。'
                             '本机照片数不是完成率；没有计划期限与实际进度证据，不能判断正常或滞后。'}
    # Size pages before the generic tool-result limiter sees them. In particular,
    # it must never halve the project list and silently lose project coverage.
    max_row_cost = max((token_estimate(row) for row in rows), default=1)
    size = min(args.get('page_size', 20), max(1, (6000 - token_estimate(result) - 100) // max_row_cost))
    page = min(args.get('page', 1), max(1, (len(rows) + size - 1) // size))
    start = (page - 1) * size
    result.update(projects=rows[start:start + size], page=page, page_size=size,
                  has_more=start + size < len(rows), next_page=page + 1 if start + size < len(rows) else None)
    return result


def summary_notice(result):
    totals = result['totals']
    return (f"统计核对：本次对话共 {totals['project_count']} 个项目，其中 "
            f"{totals['followup_project_count']} 个项目、{totals['case_count']} 个案件符合查勘维修跟进条件。"
            f"统计读取时间：{result['queried_at']}（北京时间）；本次查询不刷新官网。")
