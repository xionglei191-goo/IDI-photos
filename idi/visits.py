"""Explicit entry records and same-entry check-in reuse, without duplicating originals."""
import sqlite3
from datetime import datetime

from .db import audit, now, rowdict, uid
from .domain import DomainError, STAGES, clean_date


def allows_photo(visit_stage, photo):
    return photo['stage'] == visit_stage or (visit_stage == 'work' and photo['photo_group'] == 'completion')


def photo_date(photo):
    return (photo.get('watermark_time') or photo.get('exif_time') or '')[:10]


def list_visits(domain, case_id):
    domain.case(case_id)
    visits = domain.db.all('SELECT * FROM visits WHERE case_id=? ORDER BY visit_date,COALESCE(started_at,created_at),id', (case_id,))
    photos = domain.db.all('SELECT p.id,p.visit_id,p.role,p.photo_group,p.recognition_state,b.media_type FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id=?', (case_id,))
    shared = domain.db.all('SELECT s.photo_id,s.visit_id,p.role,p.recognition_state,b.media_type FROM visit_shared_checkins s JOIN photos p ON p.id=s.photo_id JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id=?', (case_id,))
    for visit in visits:
        own = [p for p in photos if p['visit_id'] == visit['id']]
        reused = [p for p in shared if p['visit_id'] == visit['id']]
        visit['photo_ids'] = [p['id'] for p in own]
        visit['shared_checkin_ids'] = [p['photo_id'] for p in reused]
        valid = [p for p in own if p['recognition_state'] != 'conflict' and p['media_type'] == 'image']
        valid_shared = [p for p in reused if p['recognition_state'] != 'conflict' and p['media_type'] == 'image']
        visit['counts'] = {
            'building_checkin': sum(p['role'] == 'building_checkin' for p in valid + valid_shared),
            'door_checkin': sum(p['role'] == 'door_checkin' for p in valid + valid_shared),
            'construction': sum(p['photo_group'] == 'construction' for p in valid),
            'completion': sum(p['photo_group'] == 'completion' for p in valid),
            'shared_checkin': len(valid_shared),
        }
    return visits


def save_visit(domain, case_id, data, visit_id=None):
    domain.case(case_id)
    stage = data.get('stage')
    if stage not in STAGES or stage == 'unknown':
        raise DomainError('请选择进场阶段')
    same_day = data.get('same_day_repair', False)
    if not isinstance(same_day, bool) or (same_day and stage != 'work'):
        raise DomainError('当天维修共用只适用于施工进场')
    date = clean_date(data.get('visit_date'), '进场')
    if not date:
        raise DomainError('请填写进场日期')
    date = date[:10]
    key, notes = str(data.get('visit_key', '')).strip(), str(data.get('notes', '')).strip()
    if len(key) > 120 or len(notes) > 10000:
        raise DomainError('进场标记或维修记录过长')
    start = clean_date(data.get('started_at'), '进场开始时间')
    end = clean_date(data.get('finished_at'), '本次结束时间')
    if start and (len(start) < 16 or start[:10] != date):
        raise DomainError('进场开始时间须与进场日期一致')
    if end and (len(end) < 16 or end[:10] < date or (start and datetime.fromisoformat(end) < datetime.fromisoformat(start))):
        raise DomainError('本次结束时间不能早于进场时间')
    crew = data.get('crew_number')
    if crew in (None, ''):
        crew = 1 if stage == 'work' else None
    elif isinstance(crew, bool) or not str(crew).isdigit() or not 1 <= int(crew) <= 10000:
        raise DomainError('进场人数应为 1 至 10000 的整数')
    if crew is not None:
        crew = int(crew)
    created = visit_id is None
    visit_id = visit_id or uid()
    stamp = now()
    try:
        with domain.db.tx() as con:
            before = rowdict(con.execute('SELECT * FROM visits WHERE id=? AND case_id=?', (visit_id, case_id)).fetchone())
            if not created and not before:
                raise DomainError('进场记录不存在', 404)
            if before and data.get('revision') != before['revision']:
                raise DomainError('进场记录已变化，请刷新后重试', 409)
            previous_ids = [r[0] for r in con.execute('SELECT id FROM photos WHERE visit_id=?', (visit_id,))]
            previous_shared = [r[0] for r in con.execute('SELECT photo_id FROM visit_shared_checkins WHERE visit_id=?', (visit_id,))]
            ids = data.get('photo_ids', previous_ids)
            shared_ids = data.get('shared_checkin_ids', previous_shared)
            for selected in (ids, shared_ids):
                if not isinstance(selected, list) or len(selected) > 500 or any(not isinstance(i, str) for i in selected) or len(set(selected)) != len(selected):
                    raise DomainError('请从本案选择不重复的照片，每次最多 500 项')
            if set(ids) & set(shared_ids):
                raise DomainError('共用打卡与本次施工照片不能重复选择')
            if shared_ids and stage != 'work':
                raise DomainError('只有施工进场记录可以共用查勘打卡')
            if shared_ids and not same_day:
                raise DomainError('请先标记本次为查勘后当天维修、同一次进场')
            end_date = end[:10] if end else date
            for photo_id in ids + shared_ids:
                photo = rowdict(con.execute('SELECT p.*,b.media_type FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE p.id=?', (photo_id,)).fetchone())
                if not photo or photo['case_id'] != case_id:
                    raise DomainError('照片必须属于本案')
                if photo['recognition_state'] == 'conflict':
                    raise DomainError('照片存在识别冲突，请先核对')
                if photo_id in ids:
                    if not allows_photo(stage, photo):
                        raise DomainError('照片与进场阶段不一致；查勘打卡请通过共用栏选择')
                    if photo['visit_id'] and photo['visit_id'] != visit_id:
                        raise DomainError('照片已属于另一次进场，请先在原记录移除', 409)
                    if photo_date(photo) and not date <= photo_date(photo) <= end_date:
                        raise DomainError('照片日期不在本次进场时间范围内，请区分不同次施工')
                else:
                    if photo['stage'] != 'survey' or photo['photo_group'] != 'survey_checkin' or photo['role'] not in ('building_checkin', 'door_checkin') or photo['media_type'] != 'image':
                        raise DomainError('共用栏仅接受已区分楼栋或入户的查勘打卡照片')
                    if photo_date(photo) != date:
                        raise DomainError('共用查勘打卡必须与本次进场同一天，且为同一次进场')
                    owner = con.execute('SELECT visit_id FROM visit_shared_checkins WHERE photo_id=?', (photo_id,)).fetchone()
                    if owner and owner[0] != visit_id:
                        raise DomainError('该打卡已用于另一次施工进场，不能覆盖本次缺项', 409)
            fields = {'stage': stage, 'visit_date': date, 'visit_key': key, 'notes': notes,
                      'started_at': start, 'finished_at': end, 'crew_number': crew, 'same_day_repair': int(same_day)}
            if created:
                con.execute('INSERT INTO visits(id,case_id,stage,visit_date,visit_key,basis,notes,created_at,started_at,finished_at,crew_number,same_day_repair) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (visit_id, case_id, stage, date, key, 'manual', notes, stamp, start, end, crew, int(same_day)))
            else:
                con.execute('UPDATE visits SET ' + ','.join(k + '=?' for k in fields) + ',revision=revision+1 WHERE id=?', (*fields.values(), visit_id))
            for photo_id in set(previous_ids) ^ set(ids):
                old = rowdict(con.execute('SELECT visit_id,revision FROM photos WHERE id=?', (photo_id,)).fetchone())
                target = visit_id if photo_id in ids else None
                con.execute('UPDATE photos SET visit_id=?,revision=revision+1,updated_at=? WHERE id=?', (target, stamp, photo_id))
                audit(con, 'photo', photo_id, 'assign_visit', old, {'visit_id': target})
            con.execute('DELETE FROM visit_shared_checkins WHERE visit_id=?', (visit_id,))
            con.executemany('INSERT INTO visit_shared_checkins VALUES(?,?,?)', [(pid, visit_id, stamp) for pid in shared_ids])
            audit(con, 'visit', visit_id, 'create' if created else 'update', before,
                  {**fields, 'photo_ids': ids, 'shared_checkin_ids': shared_ids})
    except sqlite3.IntegrityError as error:
        raise DomainError('同一日期与标记的进场记录已存在，请用不同标记区分多次进场', 409) from error
    return next(v for v in list_visits(domain, case_id) if v['id'] == visit_id)
