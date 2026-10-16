"""Editing is limited to the currently actionable business stage."""

FORM_LABELS = {'intake': '理赔受理', 'survey': '现场查勘', 'repair': '修复成果', 'acceptance': '维修成果验收'}
STAGE_FORM = {'pending_intake': 'intake', 'insurer_review': 'intake', 'survey': 'survey', 'repair': 'repair',
              'acceptance': 'acceptance', 'settlement': 'acceptance', 'closed': 'acceptance'}
EDITABLE_FORM = {'pending_intake': 'intake', 'survey': 'survey', 'repair': 'repair', 'acceptance': 'acceptance'}
FORM_STEP = {'intake': 0, 'survey': 2, 'repair': 3, 'acceptance': 4}
WORKFLOW_STEP = {'pending_intake': 0, 'insurer_review': 1, 'survey': 2, 'repair': 3, 'acceptance': 4,
                 'settlement': 5, 'closed': 5, 'direct_payment': 5, 'noncoverage': 5}


def stage_access(case, stage):
    current = case['workflow_stage']
    editable = EDITABLE_FORM.get(current) == stage
    active_form = EDITABLE_FORM.get(current)
    mode = 'editable' if editable else 'unverified'
    reason = '当前办理环节，可以准备业务信息和资料。' if editable else '当前办理节点尚未核实，仅供查看。'
    if not editable:
        if current == 'historical_unverified':
            mode, reason = 'unverified', '历史记录缺少受理结果，办理进度待核实，仅供查看。'
        elif current == 'insurer_intake':
            mode, reason = 'waiting', '官网报案当前由保险公司处理，维保受理尚未开始，仅供查看。'
        elif current == 'insurer_review' and stage == 'intake':
            mode, reason = 'waiting', '当前受理分支已提交保司复核，等待复核结果，本环节仅供查看。'
        elif current in ('closed', 'direct_payment', 'noncoverage'):
            mode, reason = 'closed', '本案已结束或已转入其他处理流程，历史环节仅供查看。'
        elif current in WORKFLOW_STEP and FORM_STEP[stage] < WORKFLOW_STEP[current]:
            mode, reason = 'completed', f'{FORM_LABELS[stage]}已完成，本环节仅供查看。'
        elif current in WORKFLOW_STEP and FORM_STEP[stage] > WORKFLOW_STEP[current]:
            mode, reason = 'future', '本案尚未进入此环节，完成前置流程后才能办理。'
    return {'editable': editable, 'mode': mode, 'reason': reason, 'active_stage': active_form,
            'active_label': FORM_LABELS.get(active_form, ''), 'workflow_stage': current}
