"""Decide whether an archive observation is actionable for a project lead."""
from __future__ import annotations

import re

from .domain import WORKFLOW_STAGES

FEEDBACK_POLICY_VERSION = 2


def case_context(case):
    if not case:
        return None
    code = case.get('platform_report_code') or case.get('report_number') or case.get('short_code') or ''
    location = ' · '.join(filter(None, (
        (case.get('building', '') + '栋') if case.get('building') else '',
        (case.get('unit', '') + '单元') if case.get('unit') else '',
        (case.get('room', '') + '房') if case.get('room') else '',
    )))
    location = case.get('location_detail') or location
    return {'case_id': case['id'], 'code': code, 'short_code': case.get('short_code') or '',
            'location': location, 'title': case.get('title') or '',
            'workflow_stage': case.get('workflow_stage'),
            'stage': WORKFLOW_STAGES.get(case.get('workflow_stage'), '阶段待核实')}


def classify_feedback(issue, context, photo, review, confirmation, context_signature):
    def result(state, reason, **extra):
        return {'state': state, 'reason': reason, 'context_token': context_signature, **extra}

    if issue['kind'] == 'missing':
        return result('internal', '本机未归档不能证明负责人未提交；仅供内部归档核对，不生成群内催补。')
    if not context or not context['code'] or not context['location'] or not context['title']:
        return result('blocked', '先核实案件编号、位置和维修事项，避免负责人无法定位。')
    if context['workflow_stage'] in ('closed', 'direct_payment', 'noncoverage'):
        return result('internal', '该案件已结束或不属于维修流程，不生成补拍要求。')
    evidence = issue['evidence']
    if (not photo or photo['case_id'] != issue['case_id'] or
            photo['revision'] != evidence.get('photo_revision') or
            photo['blob_hash'] != evidence.get('blob_hash') or not review or review['error']):
        return result('blocked', '照片或审核依据已变化，请完成复查后再核对。')
    if confirmation and confirmation['context_signature'] == context_signature:
        return result('ready', '已人工核对原图，并记录具体问题和处理要求。', basis='human',
                      **{k: confirmation[k] for k in ('title', 'detail', 'action')})
    # Numerical heuristics and OCR failures cannot establish a defect in the actual photo.
    if evidence.get('source') not in ('ai', 'agent'):
        return result('review', '图像指标只是线索；核对原图并填写具体问题与处理要求后，才能反馈。')
    generic_action = re.fullmatch(r'请?(?:按(?:要求|规定))?(?:补齐|补充|补拍)(?:相关|全部|所需)?(?:资料|照片|图片)[。.!！]?', issue['action'].strip())
    if (evidence.get('certainty') != 'clear' or issue['issue_key'].endswith(':ai_stage') or
            len(issue['detail'].strip()) < 10 or len(issue['action'].strip()) < 8 or generic_action or
            not evidence.get('model') or evidence['model'] != review['model']):
        return result('review', 'AI 尚未提供可明确核对的照片问题，需要人工确认。')
    return result('ready', 'AI 给出了可见依据和对应动作，反馈中会标注为 AI 建议。', basis='ai',
                  **{k: issue[k] for k in ('title', 'detail', 'action')})
