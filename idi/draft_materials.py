"""Append the current, validated incoming materials; never select historical files."""
from copy import deepcopy
import json

from .domain import DomainError
from .platform_contract import STAGES


def check_material_scope(platform, case, allowed_photo_ids):
    plan = platform.preview(case['id'], case['workflow_stage'])
    selected = {p['id'] for field in plan['file_fields'] for p in field['selected']}
    if selected - set(allowed_photo_ids):
        raise DomainError('草稿含本次群任务授权范围外的选材，已保留全部选材，请在网页人工核对后保存', 409)


def prepare_materials(platform, case, incoming, *, actor):
    stage = case['workflow_stage']
    base = {'stage': stage, 'ready_to_save': False, 'business_submitted': False, 'human_review_required': True}
    if stage not in ('survey', 'repair'):
        return {**base, 'state': 'unsupported_stage', 'reason': '当前环节不支持自动保存草稿，资料已保留在本机'}
    db = platform.db
    draft, link = platform.draft(case['id'], stage), platform.link(case['id'])
    files = deepcopy(draft['files']) if draft['revision'] else {}
    remote_ids = {str(f['id']) for field in STAGES[stage]['fields'] if field['kind'] == 'files'
                  for f in (platform._remote(link, stage).get(field['key']) or []) if isinstance(f, dict) and f.get('id')}
    uploaded = set()
    for row in db.all("SELECT result FROM platform_review_batches WHERE case_id=? AND stage=? AND state IN ('save_verified','verified')", (case['id'], stage)):
        uploaded.update(pid for pid, receipt in json.loads(row['result']).get('uploaded', {}).items() if str(receipt.get('id')) in remote_ids)
    excluded = {(r['field'], r['photo_id']) for r in db.all('SELECT field,photo_id FROM platform_draft_exclusions WHERE case_id=? AND stage=?', (case['id'], stage))}
    applicable, omitted = set(), set()
    for photo in incoming:
        current = platform.domain.photo(photo['id'])
        if any(current[k] != photo[k] for k in ('revision', 'blob_hash', 'case_id')) or current['case_id'] != case['id']:
            raise DomainError('图片归属或分类已变化，请重新核对', 409)
        if current['recognition_state'] == 'conflict' or not platform.storage.verify(current):
            raise DomainError('图片存在未解决冲突或原件校验失败，未自动选材', 409)
        for field in STAGES[stage]['fields']:
            suggest = field.get('suggest')
            shared = suggest == ['work', photo['role']] and photo.get('shared_checkin_visit_id') and photo['role'] in ('building_checkin', 'door_checkin')
            if (suggest != [photo['stage'], photo['role']] and not shared) or field.get('readonly'):
                continue
            if (photo.get('extension') or '').lower() not in field['extensions']:
                omitted.add(photo['id'])
                continue
            applicable.add(photo['id'])
            if (field['key'], photo['id']) in excluded:
                omitted.add(photo['id'])
            elif photo['id'] not in uploaded:
                files[field['key']] = list(dict.fromkeys([*files.get(field['key'], []), photo['id']]))
    if incoming and not applicable:
        return {**base, 'state': 'stage_mismatch', 'reason': '本次照片用途与当前附件栏不匹配，已保留归档'}
    if applicable and applicable <= uploaded:
        return {**base, 'state': 'already_saved', 'reason': '本次照片已有回执并存在于官网快照，未重复上传'}
    if applicable and applicable <= omitted:
        return {**base, 'state': 'excluded', 'reason': '本次照片已被人工排除或文件格式不适用，未重新加入'}
    if files != (draft['files'] if draft['revision'] else {}) or not draft['revision']:
        plan = platform.save_draft(case['id'], stage, {'values': deepcopy(draft['values']), 'files': files,
            'revision': draft['revision'], 'link_revision': link['revision']}, actor=actor, photo_guards=incoming,
            case_guard={k: case[k] for k in ('project_id','platform_report_code','short_code','workflow_stage')})
    else:
        plan = platform.preview(case['id'], stage)
    return {**base, 'state': 'local_draft', 'draft_revision': plan['draft']['revision'],
            'selected_count': len({p['id'] for f in plan['file_fields'] for p in f['selected']}),
            'issues': [i['message'] for i in plan['issues'] if i['severity'] == 'error'],
            'ready_to_save': plan['error_count'] == 0}
