"""Read APP-recorded watermark times, with OCR fallback for new video uploads."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from .db import audit, dump, now, uid
from .domain import DomainError


APP_VIDEO_TIME_BASIS = 'app_sdk_watermark_render'
_VIDEO_TIME_KEYS = {'value', 'source', 'event', 'precision', 'observed_at', 'sdk_version'}
_VIDEO_TIME_OPTIONAL_KEYS = {'template_id', 'raw_text'}
_ISO_ZONE = r'(?:Z|[+-](?:0\d|1[0-4]):[0-5]\d)'
_ISO_SECOND = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}' + _ISO_ZONE + '$')
_ISO_OBSERVED = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?' + _ISO_ZONE + '$')


def app_video_time(meta):
    """Validate the recorded start-time envelope without treating it as verification.

    The APP reads the SDK renderer when recording starts. Its observation clock is
    retained as provenance only: it is never substituted for the watermark value.
    Invalid envelopes fall back to OCR, so optional time metadata cannot strand an
    otherwise valid original. Old uploads have no envelope and retain their hashes.
    """
    if meta.get('media_type') != 'video' or meta.get('source') != 'xhey_sdk':
        return None
    value = meta.get('video_capture_time')
    if (not isinstance(value, dict) or not _VIDEO_TIME_KEYS.issubset(value)
            or set(value) - _VIDEO_TIME_KEYS - _VIDEO_TIME_OPTIONAL_KEYS):
        return None
    if 'template_id' in value and (type(value['template_id']) is not int or value['template_id'] not in (10, 20)):
        return None
    if 'raw_text' in value and (not isinstance(value['raw_text'], str) or not 1 <= len(value['raw_text']) <= 120):
        return None
    if (value.get('source') != 'sdk_watermark_render' or value.get('event') != 'recording_start'
            or value.get('precision') != 'second'):
        return None
    try:
        if (not isinstance(value['value'], str) or not _ISO_SECOND.fullmatch(value['value'])
                or not isinstance(value['observed_at'], str) or not _ISO_OBSERVED.fullmatch(value['observed_at'])
                or not isinstance(value['sdk_version'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}', value['sdk_version'])):
            return None
        captured = datetime.fromisoformat(value['value'])
        observed = datetime.fromisoformat(value['observed_at'])
        if (abs(captured.utcoffset()) > timedelta(hours=14)
                or abs(observed.utcoffset()) > timedelta(hours=14)
                or captured.year < 2000 or captured > datetime.now(timezone.utc) + timedelta(minutes=10)):
            return None
        # photos.watermark_time and existing OCR results use Beijing local time.
        return captured.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class CameraVideoTimes:
    def __init__(self, db, storage, recognizer, jobs):
        self.db, self.storage, self.recognizer = db, storage, recognizer
        jobs.register('camera_video_time', self.process)

    def enqueue(self, con, row, meta):
        """Called inside the new capture's archive transaction, never on replay."""
        if meta.get('media_type') != 'video' or app_video_time(meta):
            return
        capture = con.execute('SELECT photo_id FROM camera_official_captures WHERE device_id=? AND capture_id=?',
                              (row['device_id'], row['capture_id'])).fetchone()
        if not capture or not capture['photo_id']:
            return
        stamp, job_id = now(), uid()
        payload = {'device_id': row['device_id'], 'capture_id': row['capture_id'],
                   'photo_ids': [capture['photo_id']], 'case_id': meta['case_id'], 'blob_hash': row['blob_hash']}
        con.execute("INSERT INTO jobs(id,kind,payload,created_at,state) VALUES(?,'camera_video_time',?,?,'queued')",
                    (job_id, dump(payload), stamp))
        con.execute('''INSERT INTO camera_video_times
            (device_id,capture_id,photo_id,blob_hash,job_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)''',
                    (row['device_id'], row['capture_id'], capture['photo_id'], row['blob_hash'], job_id, stamp, stamp))

    def extract(self, path):
        from .video_watermark_time import extract_video_watermark_time
        return extract_video_watermark_time(path, self.recognizer.local_ocr)

    def process(self, payload, progress):
        key = (payload['device_id'], payload['capture_id'])
        task = self.db.one('SELECT * FROM camera_video_times WHERE device_id=? AND capture_id=?', key)
        # A job alone must never create extraction work for an old capture.
        if not task or task['blob_hash'] != payload['blob_hash'] or payload['photo_ids'] != [task['photo_id']]:
            raise DomainError('视频时间任务与原片不一致')
        if task['state'] in ('recognized', 'skipped'):
            return {'state': 'completed', 'time_state': task['state']}
        with self.db.tx() as con:
            con.execute("UPDATE camera_video_times SET state='running',updated_at=? WHERE device_id=? AND capture_id=?", (now(), *key))
        try:
            if not self.db.setting('recognition', {}).get('local_ocr', True):
                raise DomainError('本地文字识别已关闭')
            blob = self.db.one('SELECT * FROM blobs WHERE hash=?', (task['blob_hash'],))
            if not blob or not self.storage.verify(blob):
                raise DomainError('视频原片不存在或哈希不一致')
            capture = self.db.one('SELECT photo_id FROM camera_official_captures WHERE device_id=? AND capture_id=?', key)
            photo = self.db.one('SELECT blob_hash FROM photos WHERE id=?', (task['photo_id'],))
            if not capture or capture['photo_id'] != task['photo_id'] or not photo or photo['blob_hash'] != task['blob_hash']:
                result = {'state': 'skipped', 'reason': '资料已删除或原片已变更', 'watermark_time': None}
            else:
                result = self.extract(self.storage.resolve(blob['stored_path']))
        except Exception as error:
            result = {'state': 'failed', 'watermark_time': None, 'reason': str(error)[:300]}
        result = {**result, 'original_hash': task['blob_hash'], 'processed_at': now(), 'applied_to_photo': False}
        with self.db.tx() as con:
            capture = con.execute('SELECT photo_id FROM camera_official_captures WHERE device_id=? AND capture_id=?', key).fetchone()
            photo = con.execute('SELECT * FROM photos WHERE id=?', (task['photo_id'],)).fetchone()
            if not capture or capture['photo_id'] != task['photo_id'] or not photo or photo['blob_hash'] != task['blob_hash']:
                result.update(state='skipped', watermark_time=None, reason='资料已删除或原片已变更')
            elif result['state'] == 'recognized' and result.get('watermark_time'):
                # Preserve edits/confirmations and any existing visit association.
                shared = con.execute('SELECT 1 FROM visit_shared_checkins WHERE photo_id=?', (task['photo_id'],)).fetchone()
                if (not photo['watermark_time'] and 'watermark_time' not in json.loads(photo['confirmed_fields'])
                        and photo['recognition_state'] != 'confirmed' and not photo['visit_id'] and not shared):
                    con.execute('UPDATE photos SET watermark_time=?,revision=revision+1,updated_at=? WHERE id=?',
                                (result['watermark_time'], now(), task['photo_id']))
                    result['applied_to_photo'] = True
                else:
                    result['preserved_existing_fields'] = True
            if photo and result['state'] != 'skipped':
                source_key = hashlib.sha256((key[0] + ':' + key[1]).encode()).hexdigest()
                source = con.execute("SELECT id,metadata FROM photo_sources WHERE photo_id=? AND source_type='xhey_camera' AND source_key=?",
                                     (task['photo_id'], source_key)).fetchone()
                if source:
                    meta = json.loads(source['metadata'])
                    meta['video_time_extraction'] = result
                    if result['applied_to_photo']:
                        meta['time_basis'] = 'video_watermark_ocr'
                    con.execute('UPDATE photo_sources SET metadata=? WHERE id=?', (dump(meta), source['id']))
                audit(con, 'photo', task['photo_id'], 'video_watermark_time', after=result, actor='local_ocr')
            con.execute('UPDATE camera_video_times SET state=?,result=?,updated_at=? WHERE device_id=? AND capture_id=?',
                        (result['state'], dump(result), now(), *key))
        progress(1, 1)
        return {'state': 'failed' if result['state'] == 'failed' else 'completed',
                'time_state': result['state'], 'watermark_time': result.get('watermark_time'), 'reason': result.get('reason', '')}
