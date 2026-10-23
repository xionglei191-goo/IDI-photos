"""Evidence-backed local form suggestions. These never change a case or submit it."""
from __future__ import annotations

import re
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .platform_contract import STAGES


PREFILL_RULES_VERSION = 'automatic-prefill-v4'
WORK_START_CURRENT_TIME_SOURCE = '没有有效施工打卡水印时间，按自动填写时的北京时间填入；请核对实际时间'

ENGINEERING_TERMS = {
    '地基基础和主体结构': ('地基', '基础沉降', '主体结构', '承重', '梁柱', '楼板结构'),
    '保温和防水工程': ('防水', '屋面渗漏', '外墙渗漏', '外墙渗水'),
    '装饰装修工程': ('地砖', '瓷砖', '墙砖', '乳胶漆', '油漆', '吊顶', '踢脚线', '柜门', '门把手', '木门', '门套', '门框', '门窗', '窗框', '玻璃', '地板', '墙面起皮'),
    '建筑给水排水工程': ('马桶', '坐便器', '水龙头', '水管', '给水', '排水', '地漏', '洗手盆', '洗脸盆', '花洒', '角阀'),
    '通风与空调工程': ('空调', '通风', '风管'),
    '建筑电气工程': ('插座', '配电', '电线', '灯具', '漏电'),
    '智能建筑工程': ('门禁', '对讲', '智能建筑'),
    '建筑节能工程': ('建筑节能',),
    '电梯工程': ('电梯',),
}
DEFECT_TERMS = {
    '堵': ('堵塞', '堵住', '排水不畅'),
    '漏': ('漏水', '渗水', '渗漏'),
    '裂': ('开裂', '裂缝', '龟裂', '断裂', '破裂'),
    '臭': ('反臭', '异味', '发臭', '臭味'),
    '其他': ('开胶', '晃动', '松动', '松脱', '掉落', '脱落', '起皮', '起泡', '空鼓', '破损', '变形', '膨胀', '发胀', '生锈', '无法关闭', '关不上'),
}
_DEFECT_PATTERN = '|'.join(re.escape(t) for terms in DEFECT_TERMS.values() for t in terms)
_PART_PATTERN = re.compile(r'主卧|次卧|客厅|卫生间|厨房|阳台|走廊|楼梯间|屋面|外墙|墙面|地面|顶面|门|窗|马桶|坐便器|地砖|瓷砖|水龙头|水管|地漏|洗手盆|花洒|踢脚线|吊顶|柜|插座|空调|电梯')


def _stamp(value):
    try:
        if not isinstance(value, str) or len(value) < 16:
            return None
        result = datetime.fromisoformat(value.replace('T', ' '))
        return result if result.tzinfo is None else None
    except ValueError:
        return None


def _unique(values):
    return list(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))


def canonical_value(field, value):
    value = '' if value is None else str(value).strip()
    if field['kind'] == 'datetime' and (stamp := _stamp(value)):
        return stamp.strftime('%Y-%m-%d %H:%M:%S')
    if field['kind'] == 'integer' and value.isdigit():
        return str(int(value))
    return value


def input_signature(case, photos, defaults=None):
    # An added or reclassified photo invalidates an automatic draft value even
    # before the queue has processed the photo. Manual values have no such guard.
    data = {'case': {k: case.get(k) for k in ('id', 'project_id', 'platform_report_code', 'short_code', 'workflow_stage', 'title', 'repair_parts')},
            'photos': sorted((p['id'], p['revision'], p['blob_hash']) for p in photos)}
    if defaults:
        data['defaults'] = defaults
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def without_automatic_remote(link, stage, sources, fields):
    link = deepcopy(link)
    remote = link.get('snapshot', {}).get(stage, {})
    for key, source in sources.items():
        if source.get('source') in ('官网已保存的现场查勘记录', '官网已保存的修复成果记录'):
            continue
        if source['origin'] == 'automatic' and key in fields and key in remote and canonical_value(fields[key], remote[key]) == canonical_value(fields[key], source['value']):
            # Values derived by this queue are not independent evidence. Values
            # originally read from an official record retain their precedence.
            remote.pop(key)
    return link


def _sites(texts):
    sites = []
    for text in texts:
        for phrase in re.split(r'[\s，,、；;。\n]+', text):
            defect = re.search(_DEFECT_PATTERN, phrase)
            if defect:
                site = re.sub(r'(?:存在|出现|发生|有)$', '', phrase[:defect.start()]).strip('：:')
                if 1 < len(site) <= 60 and _PART_PATTERN.search(site):
                    sites.append(site)
    unique = _unique(sites)
    return [s for s in unique if not any(s != other and s in other for other in unique)]


def survey_suggestions(case, link, photos, selections, values, dictionaries):
    suggestions, notes = {}, {}
    fields = {f['key']: f for f in STAGES['survey']['fields']}

    def add(key, value, source, photo_ids=(), needs_review=True):
        if key not in suggestions and value is not None and str(value).strip():
            suggestions[key] = {'value': str(value).strip(), 'source': source,
                                'photo_ids': list(dict.fromkeys(photo_ids)), 'needs_review': needs_review}

    def option_code(dictionary, name):
        matches = [o['code'] for o in dictionaries.get(dictionary, []) if o['name'] == name]
        return matches[0] if len(matches) == 1 else None

    # Reuse an actual saved survey field before deriving anything from photos.
    # Intake's planned date is not an actual survey date.
    snapshot = link.get('snapshot', {}) if link.get('verified_at') else {}
    remote = snapshot.get('survey') or {}
    for key, field in fields.items():
        value = remote.get(key)
        if field['kind'] in ('files', 'readonly') or value in (None, '') or isinstance(value, bool):
            continue
        value = str(value).strip()
        if field['kind'] in ('dictionary', 'select'):
            options = field.get('options', dictionaries.get(field.get('dictionary', ''), []))
            if not any(o['code'] == value for o in options):
                continue
        if field['kind'] in ('datetime', 'date'):
            if not _stamp(value if field['kind'] == 'datetime' else value + ' 00:00'):
                continue
        if field['kind'] == 'personnel':
            available = {p['userId'] for p in snapshot.get('personnel', [])}
            if not value or not set(value.split(',')) <= available:
                continue
        add(key, value, '官网已保存的现场查勘记录', needs_review=False)

    valid = [p for p in photos if p.get('case_id') == case['id'] and p.get('media_type') == 'image'
             and p.get('recognition_state') in ('recognized', 'confirmed')]
    chosen = {pid for key in ('buildingFileInfoList', 'houseFileInfoList', 'fileInfoList') for pid in selections.get(key, [])}
    survey = [p for p in valid if p.get('photo_group') in ('survey_checkin', 'survey_problem')
              and (not chosen or p['id'] in chosen)]
    timed = [(p, _stamp(p.get('watermark_time'))) for p in survey]
    timed = [(p, stamp) for p, stamp in timed if stamp]
    dates = {stamp.date() for _, stamp in timed}
    if len(dates) == 1:
        checkins = [(p, stamp) for p, stamp in timed if p['photo_group'] == 'survey_checkin']
        photo, stamp = min(checkins or timed, key=lambda pair: (pair[1], pair[0]['id']))
        add('siteSurveyDate', stamp.strftime('%Y-%m-%d %H:%M'),
            ('最早查勘打卡' if checkins else '查勘问题照片') + '水印：' + photo['original_name'] + '；请核对实际到场时间', [photo['id']])
    elif len(dates) > 1:
        notes['siteSurveyDate'] = '所选查勘照片来自多个日期，请选择本次照片或填写实际查勘时间。'
    elif 'siteSurveyDate' not in suggestions:
        notes['siteSurveyDate'] = '没有本次查勘打卡或问题照片的有效水印时间；维修打卡时间不能直接当作查勘时间。'

    problem = [p for p in survey if p['photo_group'] == 'survey_problem']
    descriptions = _unique([case.get('title', ''), *[p.get('process', '') for p in problem]])
    text = '；'.join(descriptions)
    problem_ids = [p['id'] for p in problem]
    claim = snapshot.get('claim') or {}
    reason = str(claim.get('reasonDescription') or '').strip()
    if len(reason) >= 4 and reason not in ('现场查勘', '原因待核实', '待现场查勘'):
        add('defectDesc', reason, '官网受理时填写的原因说明；仅作现场原因分析参考，请核对')
    elif 'defectDesc' not in suggestions:
        notes['defectDesc'] = '现有资料仅描述缺陷现象，原因请按现场检查结果填写。'

    parts = _unique([p.get('part', '') for p in problem])
    if not parts:
        parts = _unique(case.get('repair_parts') or [])
    if parts:
        add('defectiveSite', '、'.join(parts), '本案已登记的维修部位 / 所选问题照片部位', problem_ids)
    else:
        sites = _sites(descriptions)
        if sites:
            add('defectiveSite', '、'.join(sites), '报修问题与所选查勘照片中的部位用语', problem_ids)

    for key, dictionary, rules in (('divisionWorksCode', 'part_engineering', ENGINEERING_TERMS),
                                   ('defectTypeCode', 'defect_type', DEFECT_TERMS)):
        if key in suggestions:
            continue
        names = [name for name, terms in rules.items() if any(term in text for term in terms)]
        if key == 'divisionWorksCode' and len(names) > 1:
            # Choose consistently from matched, currently available categories.
            # Dictionary codes must still come from the official option list.
            for name in names:
                code = option_code(dictionary, name)
                if code:
                    add(key, code, '资料匹配' + '、'.join(names) + '；按自动填报规则选用：' + name, problem_ids)
                    break
        elif len(names) == 1:
            code = option_code(dictionary, names[0])
            if code:
                add(key, code, '依据报修问题与所选查勘照片文字匹配官网选项：' + names[0], problem_ids)
        elif len(names) > 1:
            notes[key] = '资料涉及' + '、'.join(names) + '，请核对后选择本次查勘类别。'

    # Same-day work is supported by dated construction evidence, not today's date,
    # imports, file names, a personnel option, or the presence of a finished photo alone.
    survey_at = _stamp(values.get('siteSurveyDate') or suggestions.get('siteSurveyDate', {}).get('value'))
    work = [p for p in valid if p.get('photo_group') == 'construction']
    finished = [p for p in valid if p.get('photo_group') == 'completion']
    work_dates = [_stamp(p.get('watermark_time')) for p in work + finished]
    if (survey_at and work and all(work_dates) and min(work_dates) >= survey_at
            and {stamp.date() for stamp in work_dates} == {survey_at.date()}):
        add('repairMethod', '1', '同案施工照片水印与本次查勘在同一天；请核对实际维修安排', [p['id'] for p in work])
        if str(values.get('repairMethod') or suggestions.get('repairMethod', {}).get('value')) == '1':
            add('planRepairDate', survey_at.strftime('%Y-%m-%d'), '同日施工照片水印日期', [p['id'] for p in work])
    if 'repairMethod' not in suggestions and not values.get('repairMethod'):
        notes['repairMethod'] = '尚未确认查勘与维修的实际安排，无法自动选择当天维修、计划维修或零注拒。'
    if not values.get('repairUserId') and 'repairUserId' not in suggestions:
        personnel = snapshot.get('personnel', [])
        people = {p['userId']: p for p in personnel if p.get('userId')}
        if len(people) == 1:
            person = next(iter(people.values()))
            add('repairUserId', person['userId'], '官网维修人员仅有一个候选，自动选择：' + (person.get('name') or person['userId']))
        elif people:
            notes['repairUserId'] = '官网有多个维修人员候选，请选择本次维修人员。'
        else:
            notes['repairUserId'] = '当前没有已核对的官网维修人员候选，请刷新官网案件资料后选择。'
    if not values.get('estimateRestorationCostCode') and 'estimateRestorationCostCode' not in suggestions:
        notes['estimateRestorationCostCode'] = '现有资料没有明确的费用预估范围，请按实际核价选择。'
    return suggestions, notes


def repair_suggestions(case, link, photos, selections, values, dictionaries, automatic_sources=None):
    suggestions, notes = {}, {}
    remote = link.get('snapshot', {}).get('repair', {}) if link.get('verified_at') else {}
    for key in ('approachStartWorkDate', 'finishWorkDate'):
        if stamp := _stamp(remote.get(key)):
            suggestions[key] = {'value': stamp.strftime('%Y-%m-%d %H:%M:%S'), 'source': '官网已保存的修复成果记录', 'photo_ids': [], 'needs_review': False}
    valid = [p for p in photos if p.get('case_id') == case['id'] and p.get('media_type') == 'image'
             and p.get('recognition_state') in ('recognized', 'confirmed')]
    chosen_checkins = {pid for key in ('buildingFileInfoList', 'houseFileInfoList') for pid in selections.get(key, [])}
    chosen_finished = set(selections.get('recoveredFileInfo', []))
    groups = {
        'approachStartWorkDate': [p for p in valid if (p.get('photo_group') == 'work_checkin' or p.get('shared_checkin_visit_id'))
                                  and (not chosen_checkins or p['id'] in chosen_checkins)],
        'finishWorkDate': [p for p in valid if p.get('photo_group') == 'completion' and (not chosen_finished or p['id'] in chosen_finished)],
    }
    for key, candidates in groups.items():
        if key in suggestions:
            continue
        timed = [(p, stamp) for p in candidates if (stamp := _stamp(p.get('watermark_time')))]
        dates = {stamp.date() for _, stamp in timed}
        if key == 'approachStartWorkDate' and not timed:
            previous = (automatic_sources or {}).get(key, {})
            stamp = (_stamp(previous.get('value')) if previous.get('origin') == 'automatic'
                     and previous.get('source') == WORK_START_CURRENT_TIME_SOURCE else None)
            # Keep the first fallback stable across retries, even on UTC hosts.
            stamp = stamp or datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
            suggestions[key] = {'value': stamp.strftime('%Y-%m-%d %H:%M:%S'),
                'source': WORK_START_CURRENT_TIME_SOURCE, 'photo_ids': [], 'needs_review': True}
            continue
        if key == 'approachStartWorkDate' and len(timed) != len(candidates):
            notes[key] = '部分照片缺少有效水印时间，请核对本次资料。'
            continue
        if key == 'approachStartWorkDate':
            latest = max(dates)
            candidates = [p for p, stamp in timed if stamp.date() == latest]
            timed = [(p, stamp) for p, stamp in timed if stamp.date() == latest]
            dates = {latest}
        if not timed:
            notes[key] = '没有本次' + ('施工打卡' if key == 'approachStartWorkDate' else '完工照片') + '的有效水印时间，请核对实际时间。'
        elif len(dates) > 1:
            notes[key] = '照片来自多个日期，请选定本次' + ('进场打卡' if key == 'approachStartWorkDate' else '完工') + '资料后再填写。'
        elif len(timed) != len(candidates):
            notes[key] = '部分照片缺少有效水印时间，请核对本次资料。'
        else:
            photo, stamp = (min if key == 'approachStartWorkDate' else max)(timed, key=lambda pair: (pair[1], pair[0]['id']))
            suggestions[key] = {'value': stamp.strftime('%Y-%m-%d %H:%M:%S'),
                'source': ('最近日期最早施工打卡' if key == 'approachStartWorkDate' else '最后一张完工照片') + '水印：' + photo['original_name'] + '；请核对实际时间',
                'photo_ids': [p['id'] for p in candidates], 'needs_review': True}
    start = _stamp(values.get('approachStartWorkDate') or suggestions.get('approachStartWorkDate', {}).get('value'))
    end = _stamp(values.get('finishWorkDate') or suggestions.get('finishWorkDate', {}).get('value'))
    if start and end and end < start:
        suggestions.pop('finishWorkDate', None)
        notes['finishWorkDate'] = '完工照片时间早于进场时间，请核对是否混入其他次维修资料。'
    return suggestions, notes


def stage_suggestions(stage, case, link, photos, selections, values, dictionaries, defaults=None, automatic_sources=None):
    if stage == 'survey':
        suggestions, notes = survey_suggestions(case, link, photos, selections, values, dictionaries)
        key = 'estimateRestorationCostCode'
        default = (defaults or {}).get(key)
        option = next((o for o in dictionaries.get('estimate_restoration_cost', []) if o['code'] == default), None)
        if key not in suggestions and option:
            suggestions[key] = {'value': option['code'], 'source': '按已确认的预填默认设置：修复预估 ' + option['name'],
                                'photo_ids': [], 'needs_review': False}
            notes.pop(key, None)
        return suggestions, notes
    if stage == 'repair':
        return repair_suggestions(case, link, photos, selections, values, dictionaries, automatic_sources)
    return {}, {}
