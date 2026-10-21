"""Photo counts derived only from saved official attachment metadata."""
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .platform_contract import STAGES


def active_attachment(item):
    return (isinstance(item, dict) and item.get('id') and item.get('fileUrl')
            and str(item.get('invalid', '')).lower() not in ('1', 'true'))


def attachment_removal_history(previous, snapshot, read_at):
    """Record disappearance only from successfully read, explicit file lists."""
    from copy import deepcopy

    history = deepcopy(previous.get('removed_attachments', {}))
    keys = {field['key'] for stage in STAGES.values() for field in stage['fields'] if field['kind'] == 'files'}
    for section in ('claim', 'survey', 'repair'):
        old, fresh = previous.get(section) or {}, snapshot.get(section) or {}
        for key in keys:
            items = fresh.get(key)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                continue
            active = {str(item['id']) for item in items if active_attachment(item)}
            removed = {str(item['id']): item for item in history.get(section, {}).get(key, [])
                       if str(item['id']) not in active}
            before = old.get(key) if isinstance(old.get(key), list) else []
            candidates = [item for item in before if active_attachment(item)]
            candidates += [item for item in items if item.get('id') and item.get('fileUrl')
                           and str(item.get('invalid', '')).lower() in ('1', 'true')]
            for item in candidates:
                identity = str(item['id'])
                if identity not in active and identity not in removed:
                    removed[identity] = {**item, 'removed_at': read_at}
            if removed or key in history.get(section, {}):
                history.setdefault(section, {})[key] = list(removed.values())
    return {section: {key: items for key, items in fields.items() if items}
            for section, fields in history.items() if any(fields.values())}


PHOTO_FIELDS = {
    'survey_checkin': ('survey', 'siteSurveyId', ('buildingFileInfoList', 'houseFileInfoList')),
    'survey_problem': ('survey', 'siteSurveyId', ('fileInfoList',)),
    'work_checkin': ('repair', 'repairAcceptanceId', ('buildingFileInfoList', 'houseFileInfoList')),
    'completion': ('repair', 'repairAcceptanceId', ('recoveredFileInfo',)),
}
IMAGE_TYPES = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tif', 'tiff', 'heic', 'heif', 'avif'}
VIDEO_TYPES = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', '3gp', 'wmv'}
OTHER_TYPES = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'zip', 'rar', '7z'}


def _media_kind(item):
    declared = str(item.get('fileType') or '').lower().lstrip('.')
    types = [declared]
    for key in ('oldFileName', 'fileName', 'newFileName', 'name', 'fileUrl', 'fullFileUrl', 'path'):
        value = item.get(key)
        if isinstance(value, str):
            types.append(PurePosixPath(unquote(urlsplit(value).path)).suffix.lower().lstrip('.'))
    kinds = set()
    for value in types:
        if value in IMAGE_TYPES or value.startswith('image/'):
            kinds.add('image')
        elif value in VIDEO_TYPES or value.startswith('video/'):
            kinds.add('video')
        elif value in OTHER_TYPES or value in ('application/pdf', 'application/zip'):
            kinds.add('other')
    return kinds.pop() if len(kinds) == 1 else 'unknown'


def _result(state, count=None, note='', **counts):
    return {'state': state, 'count': count, 'videos': 0, 'other': 0, 'unknown': 0, 'note': note, **counts}


def online_photo_counts(link):
    """Keep unread fields distinct from verified empty lists; never fetch files."""
    snapshot = link.get('snapshot') or {}
    groups = {}
    for group, (stage, id_key, fields) in PHOTO_FIELDS.items():
        if not link.get('verified_at'):
            state = 'unread' if link.get('report_receiving_id') else 'unlinked'
            groups[group] = _result(state, note='官网附件尚未读取' if state == 'unread' else '案件尚未关联官网')
            continue
        if snapshot.get('details_deferred'):
            groups[group] = _result('unread', note='官网详情尚未读取')
            continue
        row = snapshot.get('row') or {}
        note = '最近同步的官网附件快照；仅统计图片'
        missing_defect_summary = False
        if group == 'survey_problem':
            repair = snapshot.get('repair') or {}
            if isinstance(repair.get('preRepairFileInfo'), list):
                # The official repair/acceptance page already combines original
                # report photos and survey defects. Do not add survey photos again.
                stage, id_key, fields = 'repair', 'repairAcceptanceId', ('preRepairFileInfo',)
                note = '官网维修环节缺陷照片，含报案及查勘带入；按附件去重，仅统计图片'
            else:
                missing_defect_summary = bool(row.get('repairAcceptanceId') or repair)
                note = '官网现场查勘缺陷照片；仅统计图片'
        detail = snapshot.get(stage) or {}
        if not detail and id_key in row and row[id_key] in (None, ''):
            groups[group] = (_result('unread', note='官网维修环节缺陷照片汇总栏尚未读取')
                             if missing_defect_summary else _result('known', 0, '最近同步时尚无此环节记录'))
            continue
        readable = [field for field in fields if isinstance(detail.get(field), list)]
        if not readable:
            groups[group] = _result('unread', note='官网附件栏尚未读取，不能确认数量')
            continue
        counts = dict.fromkeys(('image', 'video', 'other', 'unknown'), 0)
        seen = set()
        for field in readable:
            for item in detail[field]:
                if not isinstance(item, dict):
                    counts['unknown'] += 1
                    continue
                if str(item.get('invalid', '')).lower() in ('1', 'true'):
                    continue
                identity = ('id', str(item['id'])) if item.get('id') else next(
                    (('url', str(item[key])) for key in ('fileUrl', 'fullFileUrl', 'path') if item.get(key)), None)
                if identity is not None:
                    if identity in seen:
                        continue
                    seen.add(identity)
                counts[_media_kind(item)] += 1
        partial = len(readable) != len(fields) or counts['unknown'] > 0 or missing_defect_summary
        if missing_defect_summary:
            note = '官网维修环节缺陷照片汇总栏尚未读取；当前仅统计已确认的查勘照片'
        elif partial:
            note += '；部分附件栏或文件类型未核实，数量仅为已确认部分'
        groups[group] = _result('partial' if partial else 'known', counts['image'],
                                note,
                                videos=counts['video'], other=counts['other'], unknown=counts['unknown'])
    groups['construction'] = _result('unsupported', note='官网没有独立的施工过程照片栏')
    return {'read_at': link.get('verified_at'), 'groups': groups}
