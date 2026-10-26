"""Join archived-photo evidence to current cases without inferring from filenames."""
import re
from collections import Counter

from .agent_dates import parsed
from .context_memory import token_estimate
from .db import now
from .domain import DomainError, WORKFLOW_STAGES


def case_evidence(case, ref):
    return {'case_ref': ref, **{key: case.get(key) for key in (
        'project_name', 'platform_report_code', 'short_code', 'workflow_stage',
        'workflow_source', 'workflow_verified_at')},
        'title': case['title'][:240],
        'location': ' '.join(str(case.get(key) or '') for key in ('building', 'unit', 'room')).strip(),
        'stage_label': WORKFLOW_STAGES.get(case['workflow_stage'], case['workflow_stage']),
        'live_official_query': False}


def photo_case_summary(owner, args):
    # Consume every photo page in one SQLite snapshot; case pagination must not
    # turn the first page of photographs into an apparent whole-project total.
    counts = Counter()
    with owner.db.read_snapshot():
        page = 1
        while True:
            photos = owner.capabilities.photo_page({**args, 'page': page, 'page_size': 500})
            counts.update(photo['case_id'] for photo in photos['items'])
            if not photos['has_more']:
                break
            page += 1
        rows = []
        for case_id, count in counts.items():
            if case_id is None:
                continue
            ref = owner.case_refs.get(case_id)
            if not ref:
                raise DomainError('案件范围已变化，请重新查询', 409)
            rows.append({**case_evidence(owner.case(ref), ref), 'photo_count': count})
    rows.sort(key=lambda row: (row['project_name'], row['platform_report_code'] or row['short_code'] or '', row['case_ref']))
    stages = []
    for stage, label in WORKFLOW_STAGES.items():
        selected = [row for row in rows if row['workflow_stage'] == stage]
        if selected:
            stages.append({'workflow_stage': stage, 'stage_label': label, 'case_count': len(selected),
                           'photo_count': sum(row['photo_count'] for row in selected)})
    totals = {'photo_count': sum(counts.values()), 'case_count': len(rows),
              'unassigned_photo_count': counts[None], 'stages': stages}
    result = {'totals': totals, 'totals_complete': True, 'items': [], 'total': len(rows),
              'page': 1, 'page_size': 20, 'has_more': False, 'next_page': None,
              'filters': {key: value for key, value in args.items() if key not in ('page', 'page_size')},
              'queried_at': parsed(now()).isoformat(timespec='seconds'), 'timezone': 'Asia/Shanghai',
              'scope': '当前授权范围内，照片实际关联案件的本机台账与最近官网同步快照；查询不刷新官网。',
              'instruction': 'totals 是所有匹配照片关联案件的去重总数，items 按案件分页。'
                             '照片 stage 是归档分类，案件 workflow_stage 才是当前办理阶段；文件名不能证明案件、位置或阶段。'
                             '有验收照片不等于已完工或验收通过。尚未进入待验收的在办案件需核对查勘中、施工中；'
                             '待验收、待结算、已结束不能计入，历史待核实、待受理等其他状态须单列，不推断为查勘或施工。'
                             'has_more 为真时用 next_page 和返回的 page_size 继续读取，未读到的案件不能自行补名。'
                             'workflow_verified_at 是该案最近核验时间；queried_at 只是本次查询时间。'}
    max_cost = max((token_estimate(row) for row in rows), default=1)
    size = min(args.get('page_size', 20), max(1, (6000 - token_estimate(result) - 100) // max_cost))
    page = min(args.get('page', 1), max(1, (len(rows) + size - 1) // size))
    start = (page - 1) * size
    result.update(items=rows[start:start + size], page=page, page_size=size,
                  has_more=start + size < len(rows), next_page=page + 1 if start + size < len(rows) else None)
    return result


def requires_photo_case_evidence(text):
    return bool(re.search(r'照片|图片|收图', text) and re.search(r'阶段|查勘|施工|验收|完工', text)
                and re.search(r'案件|多少|哪些|尚未|还没|未进入|但是|但尚|但未', text))


def before_acceptance_requested(text):
    return bool(re.search(r'(?:还没有|还没|尚未|未|没有)(?:进入|到达|到|走到|转入)?(?:待验收|验收阶段)',
                          re.sub(r'\s+', '', text)))


def photo_case_notice(result):
    totals = result['totals']
    stages = '；'.join(f"{row['stage_label']} {row['case_count']} 案/{row['photo_count']} 张"
                      for row in totals['stages'])
    return (f"照片关联核对：本次筛选共 {totals['photo_count']} 张，实际关联 {totals['case_count']} 个案件"
            + ('；' + stages if stages else '')
            + (f"；另有 {totals['unassigned_photo_count']} 张未归案" if totals['unassigned_photo_count'] else '') + '。')


def before_acceptance_reply(result):
    """Render this precise cross-stage query from joined rows, never old prose."""
    stages = result['totals']['stages']
    pending = {'survey', 'repair'}
    later = {'acceptance', 'settlement', 'closed'}
    active_count = sum(row['case_count'] for row in stages if row['workflow_stage'] in pending)
    active_photos = sum(row['photo_count'] for row in stages if row['workflow_stage'] in pending)
    lines = [f'有验收阶段归档照片、台账当前仍在查勘或施工的案件共 {active_count} 案，关联 {active_photos} 张照片。']

    def entry(row):
        label = ('报修编号 ' + row['platform_report_code']) if row['platform_report_code'] else ('系统短编号 ' + row['short_code']) if row['short_code'] else '尚无案件编号'
        return (f"- {row['project_name']}：{row['title']}（{label}）；当前为{row['stage_label']}，"
                f"验收阶段照片 {row['photo_count']} 张。")

    active_rows = [row for row in result['items'] if row['workflow_stage'] in pending]
    if active_rows:
        lines.append('\n'.join(entry(row) for row in active_rows))
    if len(active_rows) < active_count:
        lines.append(f'本页列出其中 {len(active_rows)} 案；仍有案件明细需按返回的页码继续查询。')
    other_stages = [row for row in stages if row['workflow_stage'] not in pending | later]
    if other_stages:
        lines.append('另外，' + '；'.join(f"{row['stage_label']} {row['case_count']} 案、{row['photo_count']} 张照片" for row in other_stages)
                     + '。这些状态须单独核对，不能视为查勘或施工中。')
        other_rows = [row for row in result['items'] if row['workflow_stage'] not in pending | later]
        if other_rows:
            lines.append('\n'.join(entry(row) for row in other_rows))
    later_count = sum(row['case_count'] for row in stages if row['workflow_stage'] in later)
    later_photos = sum(row['photo_count'] for row in stages if row['workflow_stage'] in later)
    if later_count:
        lines.append(f'台账已记录为待验收或后续阶段的 {later_count} 案、{later_photos} 张照片，已从上述在办名单中排除。')
    lines.append(photo_case_notice(result))
    lines.append(f"核对时间：{result['queried_at']}（北京时间）。依据本机台账与最近官网同步快照；照片归档不能证明实际完工或验收通过。")
    return '\n\n'.join(lines)
