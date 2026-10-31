"""Pure Feishu JSON 2.0 presentation; no business writes or identity decisions."""
from __future__ import annotations

import math

from .db import dump
from .platform_contract import visible
from .review_requirements import pending_checkins

PAGE_SIZE = 6
MAX_CARD_BYTES = 28000


def text(value):
    return {'tag': 'plain_text', 'content': str(value)}


def paragraph(value):
    return {'tag': 'div', 'text': text(value)}


def button(delivery, title, action, *, primary=False, disabled=False, **values):
    result = {'tag': 'button', 'text': text(title), 'type': 'primary' if primary else 'default',
              'behaviors': [{'type': 'callback', 'value': {
                  'notification_id': delivery['id'], 'card_token': delivery['card_token'], 'action': action, **values}}]}
    if disabled:
        result.update(disabled=True, disabled_tips=text('请补齐字段、核对完整资料后再提交'))
    return result


def link_button(url):
    return {'tag': 'button', 'text': text('查看完整资料 / 原件'),
            'behaviors': [{'type': 'open_url', 'default_url': url,
                           'pc_url': url, 'ios_url': url, 'android_url': url}]}


def field_options(field, plan, dictionaries):
    if field['kind'] == 'personnel':
        return [{'code': p['userId'], 'name': p['name']} for p in plan.get('personnel', [])]
    return field.get('options') or dictionaries.get(field.get('dictionary', ''), [])


def field_value(field, plan, dictionaries):
    value = str(plan['values'].get(field['key'], '') or '')
    if field.get('name_key') and plan['values'].get(field['name_key']):
        return str(plan['values'][field['name_key']])
    options = field_options(field, plan, dictionaries)
    if field['kind'] == 'personnel':
        names = {str(o['code']): o['name'] for o in options}
        return '、'.join(names.get(code, '待核对：' + code) for code in value.split(',') if code) or '待补充'
    return next((str(o['name']) for o in options if str(o['code']) == value), value or '待补充')


def control_options(field, plan, dictionaries):
    """Keep unknown current codes visible, just as the web review does."""
    options = [{'code': str(o['code']), 'name': o['name']} for o in field_options(field, plan, dictionaries)]
    value = str(plan['values'].get(field['key'], '') or '')
    selected = value.split(',') if field['kind'] == 'personnel' else [value]
    for code in selected:
        if code and not any(o['code'] == code for o in options):
            options.append({'code': code, 'name': '待核对：' + code})
    if field['kind'] != 'personnel':
        options.insert(0, {'code': '__empty__', 'name': '请选择 / 清空'})
    return options


def editable_fields(plan, dictionaries):
    """The callback accepts exactly the controls actually displayed in the form."""
    result = []
    for field in plan['fields']:
        if field['kind'] in ('files', 'readonly') or not visible(field, plan['values']):
            continue
        value = str(plan['values'].get(field['key'], '') or '')
        options = field_options(field, plan, dictionaries)
        # Feishu inputs accept at most 1,000 characters. Never silently truncate
        # an existing business value or an official option list to make it fit.
        if (len(value) > 1000 or len(options) > 100
                or field['kind'] in ('select', 'dictionary', 'personnel') and not options):
            continue
        if field['kind'] in ('select', 'dictionary', 'personnel') and len(control_options(field, plan, dictionaries)) > 100:
            continue
        result.append(field)
    return result


def field_control(field, plan, dictionaries):
    key, kind = field['key'], field['kind']
    value = str(plan['values'].get(key, '') or '')
    common = {'name': key, 'width': 'fill', 'required': False}
    if kind in ('select', 'dictionary', 'personnel'):
        options = [{'text': text(o['name']), 'value': o['code']} for o in control_options(field, plan, dictionaries)]
        if kind == 'personnel':
            return {**common, 'tag': 'multi_select_static', 'options': options,
                    'selected_values': [code for code in value.split(',') if code]}
        return {**common, 'tag': 'select_static', 'options': options, 'initial_option': value or '__empty__'}
    control = {**common, 'tag': 'input', 'default_value': value, 'max_length': 1000}
    if kind == 'textarea':
        control.update(input_type='multiline_text', rows=3)
    if kind in ('date', 'datetime'):
        control['placeholder'] = text('YYYY-MM-DD HH:mm:ss（北京时间）' if kind == 'datetime' else 'YYYY-MM-DD')
    return control


def field_rows(plan, view, dictionaries, *, editing=False):
    """Use one layout for display/edit, in ReviewBusinessFields order.

    Long text and personnel span a row; other fields use two columns. Feishu's
    stretch mode stacks the same columns on narrow screens. Readonly values
    stay in their original positions when the surrounding controls change.
    """
    rows, pending = [], []
    editable = {f['key'] for f in editable_fields(plan, dictionaries)} if editing else set()

    def row(columns):
        rows.append({'tag': 'column_set', 'flex_mode': 'stretch', 'horizontal_spacing': '16px',
                     'columns': columns})

    for index, field in enumerate(plan['fields']):
        if field['kind'] == 'files' or not visible(field, plan['values']):
            continue
        key = field['key']
        content = [paragraph(field['label'] + ('（必填）' if editing and field.get('required') else ''))]
        content.append(field_control(field, plan, dictionaries) if key in editable
                       else paragraph(field_value(field, plan, dictionaries)))
        source = plan.get('field_sources', {}).get(key, {})
        if key in view.get('changed_fields', view.get('values', {})):
            content.append(paragraph('本次修改 · 尚未保存'))
        elif source.get('origin') == 'automatic':
            content.append(paragraph('自动填写，待核对'))
        if editing:
            if field['kind'] == 'readonly':
                content.append(paragraph(field.get('hint') or '由业务资料自动带出'))
            elif key not in editable:
                content.append(paragraph('请在完整资料中修改此项，当前值已保留。'))
            errors = [i['message'] for i in plan['issues'] if i['severity'] == 'error' and i.get('field') == key]
            if errors:
                content.append(paragraph('待补充：' + '；'.join(errors)))
        column = {'tag': 'column', 'element_id': f'review_field_{index}', 'width': 'weighted',
                  'weight': 1, 'vertical_spacing': '4px', 'elements': content}
        if field['kind'] in ('textarea', 'personnel'):
            if pending:
                row(pending)
                pending = []
            row([column])
        else:
            pending.append(column)
            if len(pending) == 2:
                row(pending)
                pending = []
    if pending:
        row(pending)
    return rows


def form_elements(delivery, plan, dictionaries, view=None):
    result = field_rows(plan, view or {}, dictionaries, editing=True)
    errors = [i['message'] for i in plan['issues'] if i['severity'] == 'error']
    if errors:
        result.append(paragraph('提交前请补齐：' + '；'.join(errors) + '。填写后点“下一步”重新检查。'))
    submit = button(delivery, '下一步：提交前核对', 'preview', primary=True)
    submit.update(name='preview_changes', form_action_type='submit')
    result.append(submit)
    return result


def attachments(plan):
    return [(field, source, item) for field in plan['file_fields']
            for source in ('existing', 'selected') for item in field[source]]


def submission_actions(delivery, plan, view, images):
    preview = view.get('mode') == 'preview'
    errors = [i['message'] for i in plan['issues'] if i['severity'] == 'error']
    allowed = not errors and not any(i.get('error') for i in images) and view.get('direct_allowed', True)
    submit = button(delivery, '保存修改并正式提交' if preview else '确认正式提交', 'submit',
                    primary=True, disabled=not allowed, acknowledged=True)
    if not allowed:
        submit['disabled_tips'] = text('暂不能正式提交：' + ('；'.join(errors) if errors else '请核对完整附件或打开完整资料处理'))
    case = plan['case']
    submit['confirm'] = {'title': text('保存修改并正式提交官网' if preview else '确认正式提交官网'),
                         'text': text(f'确认已核对 {case.get("short_code") or case.get("platform_report_code")} 的全部字段、实际业务时间和全部附件，并提交“{plan["label"]}”？')}
    edit = button(delivery, '继续修改' if preview else '补充修改信息', 'edit')
    refresh = button(delivery, '放弃修改，返回原资料' if preview else '刷新卡片', 'refresh')
    return [submit, edit, refresh] if preview else [edit, submit, refresh]


def build_card(delivery, plan, view, dictionaries, images=(), *, url=''):
    mode = view.get('mode', 'ready')
    checkins = pending_checkins(plan)
    titles = {'ready': plan['label'] + ('资料待补齐' if checkins else '资料待核对'), 'edit': plan['label'] + ' · 1/2 修改信息',
              'preview': plan['label'] + ' · 2/2 核对并提交', 'processing': '正在办理，请勿重复点击',
              'result': view.get('status', '办理结果'), 'stale': '资料已变化，请重新核对'}
    template = 'green' if view.get('verified') else 'orange' if checkins or mode in ('edit', 'stale') else 'blue'
    elements = []
    if mode == 'ready':
        elements.append({'tag': 'markdown', 'content':
                         f'<at id={delivery["open_id"]}></at> ' + ('请补充打卡资料' if checkins else '请核对拟提交信息')})
    case = plan['case']
    link = plan.get('link', {})
    place = ' '.join(filter(None, [str(case['building']) + '栋' if case.get('building') else '',
                                  str(case['unit']) + '单元' if case.get('unit') else '',
                                  case.get('room'), case.get('location_detail')])) or '位置待补充'
    elements += [paragraph(f'{case.get("short_code") or case.get("platform_report_code") or "编号待补"} · {plan["label"]}'),
                 paragraph(f'{case["project_name"]} · {place}'),
                 paragraph(f'报修编号：{link.get("platform_report_code") or case.get("platform_report_code") or "未提供"}'
                           f' · 保单：{case.get("policy_number") or "未提供"}'
                           f' · 报案编号：{link.get("insurer_report_number") or "未提供"}')]
    if view.get('notice'):
        elements.append(paragraph(view['notice']))
    if checkins and mode in ('ready', 'edit', 'preview'):
        elements.append(paragraph(('已收到完工照片，' if plan['stage'] == 'repair' else '已收到查勘缺陷资料，')
                                  + '待补打卡资料。'))
        elements.append(paragraph('官网本环节附件已核对，待补：' + '、'.join(item['label'] for item in checkins) + '。'))
        elements.append(paragraph('官网已有照片计入资料完整性，无需重复上传。补齐后将更新本卡片，核对通过后才能正式提交。'))
    if mode not in ('stale', 'processing'):
        elements.append({'tag': 'markdown', 'content': '**业务信息**'})
        if mode == 'edit':
            elements += [paragraph('修改下方信息后，点击“下一步：提交前核对”进入确认页。日期按北京时间填写，确认前不会保存或正式提交。'),
                         {'tag': 'form', 'name': 'review_fields',
                          'elements': form_elements(delivery, plan, dictionaries, view)}]
            elements.append(button(delivery, '放弃修改，返回原资料', 'refresh'))
        else:
            elements.extend(field_rows(plan, view, dictionaries))
        errors = [i['message'] for i in plan['issues'] if i['severity'] == 'error']
        if errors and mode not in ('result', 'edit'):
            elements.append(paragraph('暂不能正式提交：' + '；'.join(errors)))
            elements.append(paragraph('请点击“继续修改”补齐后重新核对。' if mode == 'preview'
                                      else '请点击“补充修改信息”补齐业务字段，并确认照片是否传齐。'))
        if mode == 'preview':
            elements.extend(submission_actions(delivery, plan, view, images))
        elements.append({'tag': 'hr'})
        for field in plan['file_fields']:
            elements.append(paragraph(f'{field["label"]} {len(field["existing"]) + len(field["selected"])} 项'
                                      f'（官网已有 {len(field["existing"])} · 本次选入 {len(field["selected"])}）'))
            for item in images:
                if (item.get('field_key') != field['key'] if item.get('field_key')
                        else not item['label'].startswith(field['label'] + ' · ')):
                    continue
                elements.append(paragraph(item['label']))
                if item.get('image_key'):
                    elements.append({'tag': 'img', 'img_key': item['image_key'], 'alt': text(item['label']),
                                     'scale_type': 'fit_horizontal', 'preview': True})
                else:
                    elements.append(paragraph(item.get('error') or '此附件请打开完整资料查看原件'))
        total = len(attachments(plan))
        pages, page = max(1, math.ceil(total / PAGE_SIZE)), view.get('page', 0)
        if total:
            elements.append(paragraph(f'附件预览 {page + 1}/{pages} 页 · 共 {total} 项。点击图片可放大；原件、视频和 PDF 在完整资料中查看。'))
        if mode == 'edit' and pages > 1:
            elements.append(paragraph('进入提交前核对页后，可继续翻页核对照片。'))
        if mode in ('ready', 'preview'):
            if page > 0:
                elements.append(button(delivery, '上一页照片', 'photos', page=page - 1))
            if page + 1 < pages:
                elements.append(button(delivery, '下一页照片', 'photos', page=page + 1))
        if mode == 'ready':
            elements.extend(submission_actions(delivery, plan, view, images))
        if mode == 'result' and view.get('can_readback'):
            elements.append(button(delivery, '回读官网结果', 'readback', primary=True))
        if mode == 'result' and view.get('can_refresh'):
            elements.append(button(delivery, '重新核对后再操作', 'refresh'))
    if url:
        elements.append(link_button(url))
    else:
        elements.append(paragraph('完整资料与原件请在 IDI 工作台的“拟提交 → 核对拟提交信息”中查看。'))
    result = {'schema': '2.0', 'config': {'update_multi': True},
              'header': {'title': text(titles.get(mode, '资料核对')), 'template': template},
              'body': {'elements': elements, 'vertical_spacing': '12px'}}
    if len(dump(result).encode()) > MAX_CARD_BYTES or any(
            len(str(plan['values'].get(f['key'], '') or '')) > 5000 for f in plan['fields'] if f['kind'] != 'files'):
        # A shortened preview must never retain a button claiming the full
        # contents were reviewed. The authenticated full review remains usable.
        result['body']['elements'] = [paragraph('本案资料较多，超出飞书单张卡片容量。请打开完整资料核对并提交。')]
        if url:
            result['body']['elements'].append(link_button(url))
        else:
            result['body']['elements'].append(paragraph('请进入 IDI 工作台的“拟提交 → 核对拟提交信息”。'))
        view['direct_allowed'] = False
    return result
