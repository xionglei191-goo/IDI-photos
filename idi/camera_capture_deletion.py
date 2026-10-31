"""Device-owned capture withdrawal with durable protection against late uploads."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack

from .db import audit, dump, now
from .domain import DomainError
from .photo_deletion import delete_photos


def deletion_record(db, device_id, capture_id, *, connection=None):
    sql = 'SELECT * FROM camera_capture_deletions WHERE device_id=? AND capture_id=?'
    if connection is not None:
        row = connection.execute(sql, (device_id, capture_id)).fetchone()
        return dict(row) if row else None
    return db.one(sql, (device_id, capture_id))


def ensure_not_deleted(db, device_id, capture_id, *, connection=None):
    if deletion_record(db, device_id, capture_id, connection=connection):
        raise DomainError('资料已删除，不会重新归档', 410)


class CameraCaptureDeletion:
    def __init__(self, official, material_locks=()):
        self.official = official
        self.camera, self.db, self.domain = official.camera, official.db, official.domain
        # Same order as the workbench delete route, followed by the ingestion order.
        self.locks = (*material_locks, self.camera.upload_lock, official.lock)

    def delete(self, device, capture_id, data):
        if not isinstance(capture_id, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,64}', capture_id):
            raise DomainError('拍摄编号格式无效')
        if (not isinstance(data, dict) or set(data) != {'case_id'}
                or not isinstance(data['case_id'], str) or not 1 <= len(data['case_id']) <= 80
                or data['case_id'] != data['case_id'].strip()):
            raise DomainError('请提供本条拍摄记录的案件编号')
        with ExitStack() as stack:
            for lock in self.locks:
                stack.enter_context(lock)
            with self.db.tx() as con:
                return self._delete(con, device, capture_id, data['case_id'])

    def _delete(self, con, device, capture_id, supplied_case):
        # Authentication may have waited on a running upload. Recheck revocation,
        # bound person and current project membership inside the write transaction.
        current = con.execute('SELECT * FROM camera_devices WHERE id=? AND revoked_at IS NULL',
                              (device['id'],)).fetchone()
        if (not current or current['token_hash'] != device['token_hash']
                or current['user_id'] != device.get('user_id')):
            raise DomainError('设备连接已失效，请重新配对', 401)
        device = self.camera.effective_device(current)
        row = con.execute('SELECT * FROM camera_official_captures WHERE device_id=? AND capture_id=?',
                          (device['id'], capture_id)).fetchone()
        row = dict(row) if row else None
        previous = deletion_record(self.db, device['id'], capture_id, connection=con)
        case_id = json.loads(row['metadata'])['case_id'] if row else previous['case_id'] if previous else supplied_case
        case = con.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
        if not case:
            raise DomainError('案件不存在，请联系管理员核对', 404)
        self.camera.require_case_access(device, dict(case), connection=con)
        if supplied_case != case_id:
            raise DomainError('拍摄记录与案件不一致，未删除', 409)
        if previous:
            if previous['requested_by'] != device.get('user_id'):
                raise DomainError('只能删除本人拍摄的资料', 403)
            return {**json.loads(previous['result']), 'already_deleted': True}
        if not row:
            if con.execute('SELECT 1 FROM camera_official_captures WHERE capture_id=? AND device_id<>?',
                           (capture_id, device['id'])).fetchone():
                raise DomainError('只能删除本机本人拍摄的资料', 403)
            if con.execute('SELECT 1 FROM camera_captures WHERE device_id=? AND capture_id=?',
                           (device['id'], capture_id)).fetchone():
                raise DomainError('旧版照片请在工作台删除', 409)

        stamp, removed, shared = now(), False, False
        photo_id = row['photo_id'] if row else None
        source_key = hashlib.sha256((device['id'] + ':' + capture_id).encode()).hexdigest()
        own_source = None
        if photo_id:
            photo = con.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
            own_source = con.execute("SELECT * FROM photo_sources WHERE photo_id=? AND source_type='xhey_camera' AND source_key=?",
                                     (photo_id, source_key)).fetchone()
            if not photo or not own_source:
                raise DomainError('归档来源已变化，请在工作台核对后删除', 409)
            source_meta = json.loads(own_source['metadata'])
            if source_meta.get('uploaded_by') != device.get('user_id') or source_meta.get('device_id') != device['id']:
                raise DomainError('只能删除本人拍摄的资料', 403)
            if photo['case_id'] != case_id or photo['project_id'] != case['project_id']:
                raise DomainError('归档资料所属案件已变化，请在工作台核对后删除', 409)
            # A promoted chat copy / another camera's capture still owns its active
            # record. Withdraw only this capture's source; never erase their work.
            shared = bool(con.execute('SELECT 1 FROM photo_sources WHERE photo_id=? AND id<>?',
                                      (photo_id, own_source['id'])).fetchone()
                          or con.execute('SELECT 1 FROM camera_official_captures WHERE photo_id=? AND NOT (device_id=? AND capture_id=?)',
                                         (photo_id, device['id'], capture_id)).fetchone()
                          or con.execute('SELECT 1 FROM camera_captures WHERE photo_id=?', (photo_id,)).fetchone())
            if shared:
                audit(con, 'photo', photo_id, 'withdraw_camera_source', before=dict(own_source),
                      after={'capture_id': capture_id, 'device_id': device['id'], 'active_record_retained': True,
                             'original_retained': True}, actor='user:' + device['user_id'])
                con.execute('DELETE FROM photo_sources WHERE id=?', (own_source['id'],))
            else:
                delete_photos(self.domain, [photo_id], {photo_id: photo['revision']},
                              connection=con, actor='user:' + device['user_id'])
                removed = True

        if row:
            con.execute("UPDATE camera_official_captures SET state='deleted',photo_id=NULL,error='',updated_at=? WHERE device_id=? AND capture_id=?",
                        (stamp, device['id'], capture_id))
            # delete_photos already cancels queued photo work when it removes an
            # archive. The targeted cleanup also handles a retained shared record.
            task = con.execute('SELECT * FROM camera_video_times WHERE device_id=? AND capture_id=?',
                               (device['id'], capture_id)).fetchone()
            if task and task['state'] == 'queued':
                con.execute("UPDATE jobs SET state='cancelled',finished_at=?,error='拍摄人已删除资料' WHERE id=? AND state='queued'",
                            (stamp, task['job_id']))
                con.execute("UPDATE camera_video_times SET state='skipped',result=?,updated_at=? WHERE device_id=? AND capture_id=?",
                            (dump({'state': 'skipped', 'reason': '拍摄人已删除资料', 'watermark_time': None,
                                   'original_hash': row['blob_hash'], 'applied_to_photo': False}), stamp, device['id'], capture_id))
        result = {'capture_id': capture_id, 'case_id': case_id, 'state': 'deleted', 'deleted': True,
                  'deleted_at': stamp, 'originals_retained': True,
                  'archive_removed': removed, 'shared_archive_retained': shared}
        con.execute('INSERT INTO camera_capture_deletions(device_id,capture_id,case_id,requested_by,deleted_at,result) VALUES(?,?,?,?,?,?)',
                    (device['id'], capture_id, case_id, device.get('user_id'), stamp, dump(result)))
        audit(con, 'camera_official', capture_id, 'delete',
              before={'capture': row, 'source': dict(own_source) if own_source else None},
              after={**result, 'device_id': device['id']}, actor='user:' + device['user_id'])
        return {**result, 'already_deleted': False}
