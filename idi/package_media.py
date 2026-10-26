"""Reconcile official photo identities before creating a delivery snapshot.

Downloaded originals live in the official-media cache, never in the upload queue.
"""
from __future__ import annotations

import hashlib
import threading
from urllib.parse import quote

from .db import dump
from .domain import DomainError, photo_group
from .identity import current
from .platform_client import attachment_url
from .platform_photos import _media_kind


FIELDS = (
    ('survey', 'buildingFileInfoList', 'survey', 'building_checkin', '查勘楼栋打卡'),
    ('survey', 'houseFileInfoList', 'survey', 'door_checkin', '查勘入户打卡'),
    ('survey', 'fileInfoList', 'survey', 'problem', '查勘缺陷'),
    ('repair', 'buildingFileInfoList', 'work', 'building_checkin', '维修楼栋打卡'),
    ('repair', 'houseFileInfoList', 'work', 'door_checkin', '维修入户打卡'),
    ('repair', 'preRepairFileInfo', 'survey', 'problem', '报案及查勘缺陷'),
    ('repair', 'recoveredFileInfo', 'acceptance', 'finished', '完工照片'),
)


class PackageMedia:
    def __init__(self, platform, access):
        self.platform, self.access = platform, access
        self.db, self.storage = platform.db, platform.storage
        self._locks = [threading.RLock() for _ in range(32)]

    def linked(self, case_id):
        return bool(self.platform.link(case_id)['report_receiving_id'])

    def _permission(self, case_id):
        case = self.platform.domain.case(case_id)
        person = current()
        if person and not self.access.principal(person.id).allows(case['project_id'], manage=True):
            raise DomainError('资料包任务的项目管理权限已变化', 403)

    def inventory(self, case_id, *, strict=False):
        link = self.platform.link(case_id)
        if not link['report_receiving_id']:
            return link, [], None
        snapshot = link['snapshot']
        entries, errors = [], []
        if not link.get('verified_at') or snapshot.get('details_deferred'):
            errors.append('官网附件详情尚未完整读取')
        row = {**snapshot.get('claim', {}), **snapshot.get('row', {})}
        seen = set()
        for section, key, stage, role, label in FIELDS:
            detail = snapshot.get(section) or {}
            id_key = 'siteSurveyId' if section == 'survey' else 'repairAcceptanceId'
            if not detail and id_key in row and not row[id_key]:
                continue
            records = detail.get(key)
            if not isinstance(records, list):
                errors.append(label + '栏尚未完整读取')
                continue
            for record in records:
                if not isinstance(record, dict):
                    errors.append(label + '附件格式异常')
                    continue
                if str(record.get('invalid', '')).lower() in ('1', 'true'):
                    continue
                # Empty placeholders are not attachments. Documents do not belong
                # in photo deliveries, even when returned in a media field.
                if not any(record.get(k) for k in ('id', 'fileUrl', 'fullFileUrl', 'fileName', 'oldFileName')):
                    continue
                kind = _media_kind(record)
                if kind == 'other':
                    continue
                file_id = str(record.get('id') or '')
                name = record.get('oldFileName') or record.get('fileName') or file_id
                try:
                    url = attachment_url(record.get('fileUrl') or record.get('fullFileUrl'))
                    if not file_id or kind not in ('image', 'video'):
                        raise DomainError('附件身份或类型无法确认')
                except DomainError as error:
                    errors.append(f'{label} / {name}：{error}')
                    continue
                identity = (section, key, file_id, url)
                if identity in seen:
                    continue
                seen.add(identity)
                entries.append({'section': section, 'key': key, 'stage': stage, 'role': role,
                                'label': label, 'kind': kind, 'file_id': file_id, 'url': url, 'record': record})
        if strict and errors:
            raise DomainError('无法确认官网照片齐全，未打包：' + '；'.join(errors[:10]), 409)
        token = hashlib.sha256(dump([link['report_receiving_id'], sorted(
            [(e['section'], e['key'], e['file_id'], e['url'],
              e['record'].get('oldFileName') or e['record'].get('fileName'), e['record'].get('createDate')) for e in entries]),
            sorted(errors)]).encode()).hexdigest()
        return link, entries, token

    def signature(self, case_id):
        return self.inventory(case_id)[2]

    def _cached_blob(self, link, entry):
        return self.db.one('SELECT b.*,m.original_name,m.synced_at FROM platform_media m JOIN blobs b ON b.hash=m.blob_hash '
                           'WHERE m.case_id=? AND m.report_receiving_id=? AND m.file_id=? AND m.source_url=?',
                           (link['case_id'], link['report_receiving_id'], entry['file_id'], entry['url']))

    def require_complete(self, case_id, *, include_videos=True):
        """Called inside the export's read snapshot to close refresh/queue races."""
        link, entries, _ = self.inventory(case_id, strict=True)
        missing = []
        for entry in entries:
            if not include_videos and entry['kind'] == 'video':
                continue
            blob = self._cached_blob(link, entry)
            if not blob or not self.storage.verify(blob):
                missing.append(entry['label'] + ' / ' + (entry['record'].get('oldFileName') or entry['record'].get('fileName') or entry['file_id']))
        if missing:
            raise DomainError('官网照片未补齐，未打包：' + '；'.join(missing[:10]), 409)

    def check(self, case_id, *, include_videos=True):
        with self._locks[hash(case_id) % len(self._locks)]:
            return self._check(case_id, include_videos=include_videos)

    def _check(self, case_id, *, include_videos=True):
        self._permission(case_id)
        if not self.linked(case_id):
            return
        self.platform.workflow.refresh_case(case_id, check_permission=lambda: self._permission(case_id))
        link, entries, token = self.inventory(case_id, strict=True)
        failures = []
        for entry in entries:
            if not include_videos and entry['kind'] == 'video':
                continue
            self._permission(case_id)
            if self.signature(case_id) != token:
                raise DomainError('核对期间官网附件已变化，请重试打包', 409)
            # Reuse the same authenticated, identity-checked downloader as the
            # official photo viewer. It verifies cached bytes and upload receipts.
            plan = {'case': {'id': case_id}, 'link': link,
                    'file_fields': [{'key': entry['key'], 'existing': [entry['record']]}]}
            try:
                blob, name = self.platform.media.ensure(plan, entry['key'], entry['file_id'])
                if blob['media_type'] not in ('image', 'video') or not self.storage.verify(blob):
                    raise DomainError('原件未通过文件校验')
            except Exception as error:
                failures.append(f"{entry['label']} / {entry['record'].get('oldFileName') or entry['record'].get('fileName') or entry['file_id']}：{error}")
        self._permission(case_id)
        if self.signature(case_id) != token:
            raise DomainError('核对期间官网附件已变化，请重试打包', 409)
        if failures:
            raise DomainError(f'官网照片未补齐（{len(failures)}项），未打包：' + '；'.join(failures[:10]), 409)
        return token

    def cached_photos(self, case_id):
        link, entries, _ = self.inventory(case_id)
        photos = []
        for entry in entries:
            blob = self._cached_blob(link, entry)
            if not blob:
                continue
            identity = [case_id, link['report_receiving_id'], entry['section'], entry['key'], entry['file_id'], entry['url']]
            photo_id = 'official-' + hashlib.sha256(dump(identity).encode()).hexdigest()[:32]
            url = f"/api/platform/cases/{quote(case_id, safe='')}/stages/{entry['section']}/files/{quote(entry['key'], safe='')}/{quote(entry['file_id'], safe='')}"
            photos.append({**blob, 'id': photo_id, 'blob_hash': blob['hash'], 'case_id': case_id,
                           'stage': entry['stage'], 'role': entry['role'],
                           'photo_group': photo_group(entry['stage'], entry['role']), 'part': '', 'process': '',
                           'watermark_time': None, 'exif_time': None, 'recognition_state': 'pending',
                           'confirmed_fields': [], 'revision': 0, 'visit_id': None, 'visit_date': None,
                           'visit_key': None, 'shared_checkin_visit_id': None, 'shared_checkin_date': None,
                           'shared_checkin_key': None, 'created_at': entry['record'].get('createDate') or '',
                           'thumbnail_url': url + '/thumbnail', 'original_url': url + '/original',
                           'official_source': {'file_id': entry['file_id'], 'section': entry['section'],
                                               'field': entry['key'], 'source_url': entry['url']},
                           'sources': [{'source_type': 'official_platform', 'original_name': blob['original_name'],
                                        'group_name': '', 'sender': '', 'message_time': entry['record'].get('createDate'),
                                        'received_at': blob['synced_at']}]})
        return photos


def supplement(local, official):
    """Keep local metadata and distinct uses of shared check-in originals."""
    photos = list(local)
    seen = {(p['blob_hash'], p['photo_group'], p['role']) for p in photos}
    for photo in official:
        key = (photo['blob_hash'], photo['photo_group'], photo['role'])
        if key not in seen:
            photos.append(photo)
            seen.add(key)
    return photos
