"""Independent work check-in records made from explicitly reused survey photos."""
from __future__ import annotations

import json
from copy import deepcopy

from .db import audit, dump, now, rowdict, uid
from .domain import DomainError

CHECKIN_FIELDS = {'buildingFileInfoList': 'building_checkin', 'houseFileInfoList': 'door_checkin'}


def _date(photo):
    return (photo.get('watermark_time') or photo.get('exif_time') or '')[:10]


def valid_work_copy(photo):
    metadata = photo.get('reuse_metadata') or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    source = metadata.get('source', {})
    return bool(photo.get('reuse_source_id') and metadata.get('purpose') == 'same_day_work_checkin'
                and source.get('id') == photo['reuse_source_id']
                and source.get('case_id') == photo.get('case_id')
                and source.get('blob_hash') == photo.get('blob_hash')
                and source.get('role') == photo.get('role') in CHECKIN_FIELDS.values()
                and source.get('stage') == 'survey' and source.get('photo_group') == 'survey_checkin'
                and photo.get('stage') == 'work' and photo.get('photo_group') == 'work_checkin'
                and _date(source) == _date(photo))


def copy_purpose_conflicts(photo, fields, conflicts):
    """An approved second use does not alter the original watermark evidence."""
    conflicts = set(conflicts)
    if (valid_work_copy(photo) and fields.get('stage') in (None, '', 'survey', 'work')
            and fields.get('photo_group') in (None, '', 'survey_checkin', 'work_checkin')):
        conflicts.difference_update(('stage', 'photo_group'))
        if fields.get('role') == 'other':
            conflicts.discard('role')
    return conflicts


def copy_for_work(con, source_id, case_id, *, revision=None, visit_id=None, repair_date=None,
                  actor='local', legacy=False):
    source = rowdict(con.execute('SELECT p.*,b.media_type FROM photos p JOIN blobs b '
                                'ON b.hash=p.blob_hash WHERE p.id=?', (source_id,)).fetchone())
    if not source or source['case_id'] != case_id:
        raise DomainError('请选择本案的查勘打卡照片', 409)
    if revision is not None and source['revision'] != revision:
        raise DomainError('查勘原照已变化，请刷新后重新复用', 409)
    if (source['reuse_source_id'] or source['stage'] != 'survey' or source['photo_group'] != 'survey_checkin'
            or source['role'] not in CHECKIN_FIELDS.values() or source['media_type'] != 'image'):
        raise DomainError('仅可将已区分楼栋或入户的查勘打卡复制为维修打卡')
    if source['recognition_state'] == 'conflict':
        raise DomainError('查勘原照存在识别冲突，请先核对', 409)
    if repair_date and _date(source) and str(repair_date)[:10] != _date(source):
        raise DomainError('查勘打卡与维修进场日期不同，不能作为当天同次进场复用', 409)
    existing = rowdict(con.execute('SELECT * FROM photos WHERE case_id=? AND blob_hash=? '
                                  'AND reuse_source_id IS NOT NULL', (case_id, source['blob_hash'])).fetchone())
    if existing:
        if not valid_work_copy(existing) or existing['role'] != source['role']:
            raise DomainError('已有维修打卡副本的信息已修改，请先核对该副本', 409)
        if visit_id and existing['visit_id'] and existing['visit_id'] != visit_id:
            raise DomainError('该副本已用于另一次施工进场，不能覆盖本次缺项', 409)
        if visit_id and existing['visit_id'] != visit_id:
            con.execute('UPDATE photos SET visit_id=?,revision=revision+1,updated_at=? WHERE id=?',
                        (visit_id, now(), existing['id']))
            audit(con, 'photo', existing['id'], 'assign_visit',
                  {'visit_id': existing['visit_id']}, {'visit_id': visit_id}, actor)
        return existing['id'], False
    stamp, copy_id = now(), uid()
    columns = [r['name'] for r in con.execute('PRAGMA table_info(photos)')]
    snapshot = {k: source[k] for k in ('id', 'case_id', 'revision', 'blob_hash', 'stage', 'role',
                                       'photo_group', 'watermark_time', 'exif_time')}
    metadata = {'purpose': 'same_day_work_checkin', 'source': snapshot, 'copied_at': stamp, 'actor': actor}
    values = {k: source[k] for k in columns}
    values.update(id=copy_id, visit_id=visit_id, stage='work', photo_group='work_checkin', revision=1,
                  confirmed_fields=dump(sorted(set(json.loads(source['confirmed_fields']))
                                              | {'stage', 'role', 'photo_group'})),
                  reuse_source_id=source_id, reuse_source_revision=source['revision'], reuse_metadata=dump(metadata),
                  created_at=source['created_at'] if legacy else stamp, updated_at=stamp)
    con.execute('INSERT INTO photos(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')',
                tuple(values[k] for k in columns))
    for row in con.execute('SELECT * FROM photo_sources WHERE photo_id=?', (source_id,)).fetchall():
        saved = dict(row)
        details = json.loads(saved['metadata'])
        details['checkin_reuse'] = metadata
        saved.update(id=uid(), photo_id=copy_id, metadata=dump(details))
        con.execute('INSERT INTO photo_sources(' + ','.join(saved) + ') VALUES(' + ','.join('?' for _ in saved) + ')',
                    tuple(saved.values()))
    # Retain the source OCR as evidence; it is not a new recognition or new photo.
    for row in con.execute('SELECT * FROM recognitions WHERE photo_id=?', (source_id,)).fetchall():
        saved = dict(row)
        saved.update(id=uid(), photo_id=copy_id)
        con.execute('INSERT INTO recognitions(' + ','.join(saved) + ') VALUES(' + ','.join('?' for _ in saved) + ')',
                    tuple(saved.values()))
    audit(con, 'photo', copy_id, 'reuse_as_work_checkin', snapshot,
          {'id': copy_id, 'stage': 'work', 'photo_group': 'work_checkin', 'visit_id': visit_id,
           'source_photo_id': source_id, 'original_retained': True}, actor)
    return copy_id, True


def verified_copy_receipts(platform, case_id, stage, link=None):
    """Use an existing receipt when the same image was already saved in this field."""
    if stage != 'repair':
        return {}
    copies = [p for p in platform.db.all(
        'SELECT * FROM photos WHERE case_id=? AND reuse_source_id IS NOT NULL', (case_id,)) if valid_work_copy(p)]
    if not copies:
        return {}
    link = link or platform.link(case_id)
    remote = platform._remote(link, stage)
    remote_ids = {key: {str(f.get('id')) for f in remote.get(key, []) if isinstance(f, dict)}
                  for key in CHECKIN_FIELDS}
    receipts = {}
    for row in platform.db.all("SELECT snapshot,result FROM platform_review_batches WHERE case_id=? AND stage=? "
                              "AND state IN ('save_verified','verified') ORDER BY created_at", (case_id, stage)):
        snapshot, result = json.loads(row['snapshot']), json.loads(row['result'])
        if snapshot['link']['report_receiving_id'] != link['report_receiving_id']:
            continue
        for field in snapshot['file_fields']:
            if field['key'] not in CHECKIN_FIELDS:
                continue
            originals = {p['id']: p['blob_hash'] for p in field['selected']}
            for copy in copies:
                source_id = copy['reuse_source_id']
                receipt = result.get('uploaded', {}).get(source_id)
                if (copy['role'] == CHECKIN_FIELDS[field['key']] and receipt
                        and originals.get(source_id) == copy['blob_hash']
                        and str(receipt.get('id')) in remote_ids[field['key']]):
                    receipts[copy['id']] = deepcopy(receipt)
    return receipts


def migrate_legacy_copies(con):
    # Historical shared choices were already explicitly confirmed for one entry.
    for row in con.execute('SELECT s.*,v.case_id,v.visit_date FROM visit_shared_checkins s '
                           "JOIN visits v ON v.id=s.visit_id WHERE v.stage='work' AND v.same_day_repair=1").fetchall():
        try:
            copy_for_work(con, row['photo_id'], row['case_id'], visit_id=row['visit_id'],
                          repair_date=row['visit_date'], actor='migration', legacy=True)
        except DomainError as error:
            audit(con, 'photo', row['photo_id'], 'reuse_migration_needs_check',
                  after={'reason': str(error)}, actor='migration')
    # Unsaved manual choices should also use their independent copied record.
    for draft in con.execute("SELECT * FROM platform_drafts WHERE stage='repair'").fetchall():
        files, changed = json.loads(draft['files_json']), False
        repair_date = json.loads(draft['values_json']).get('approachStartWorkDate')
        if not repair_date:
            link = con.execute('SELECT snapshot FROM platform_links WHERE case_id=?', (draft['case_id'],)).fetchone()
            repair_date = json.loads(link['snapshot']).get('repair', {}).get('approachStartWorkDate') if link else None
        choices = con.execute("SELECT m.*,p.stage AS photo_stage,p.photo_group,p.role FROM platform_manual_file_selections m "
                              'JOIN photos p ON p.id=m.photo_id AND p.revision=m.photo_revision AND p.blob_hash=m.blob_hash '
                              "WHERE m.case_id=? AND m.stage='repair'", (draft['case_id'],)).fetchall()
        for choice in choices:
            if (choice['field'] not in CHECKIN_FIELDS or choice['photo_stage'] != 'survey'
                    or choice['photo_group'] != 'survey_checkin' or choice['role'] != CHECKIN_FIELDS[choice['field']]
                    or choice['photo_id'] not in files.get(choice['field'], [])):
                continue
            try:
                copy_id, _ = copy_for_work(con, choice['photo_id'], draft['case_id'],
                                           revision=choice['photo_revision'], repair_date=repair_date,
                                           actor='migration', legacy=True)
            except DomainError:
                continue
            files[choice['field']] = list(dict.fromkeys(copy_id if p == choice['photo_id'] else p
                                                       for p in files[choice['field']]))
            changed = True
        if changed:
            con.execute('UPDATE platform_drafts SET files_json=?,revision=revision+1,updated_at=? WHERE case_id=? AND stage=?',
                        (dump(files), now(), draft['case_id'], draft['stage']))
            audit(con, 'platform_draft', draft['case_id'], 'copy_manual_checkins',
                  after={'stage': 'repair', 'files': files}, actor='migration')
