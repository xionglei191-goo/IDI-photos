"""Archive official SDK originals locally, with optional explicit verification."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Body, File, Form, Request, UploadFile
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .db import audit, dump, now, uid
from .domain import DomainError, GROUP_FIELDS
from .mobile_camera import CaptureMetadata, MAX_PHOTO_BYTES, GROUP_LABELS, case_number, digest, camera_admin, location
from .xhey_camera import XheyClient, XheyError, XheySettings
from .camera_video import inspect_mp4, MAX_VIDEO_BYTES, MAX_RECORD_SECONDS
from .photo_dedup import normalize_watermark_code

LOG = logging.getLogger(__name__)


class OfficialMetadata(CaptureMetadata):
    source: Literal['xhey_sdk', 'xhey_h5']
    media_type: Literal['image', 'video'] = 'image'
    # SDK callback values are useful metadata, not a verification verdict.
    sdk_photo_time: str = Field(default='', max_length=40)
    sdk_anti_fake_code: str = Field(default='', max_length=256)


def sdk_photo_time(meta):
    value = meta.get('sdk_photo_time', '')
    for pattern in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, pattern).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
    return None


class OfficialCamera:
    def __init__(self, camera):
        self.camera = camera
        self.db, self.domain, self.storage = camera.db, camera.domain, camera.storage
        self.settings = XheySettings(self.storage.settings.data_dir)
        self.client_factory = XheyClient
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.thread = None
        self.dedup = None

    def session_metadata(self, device, metadata, source):
        try:
            model = OfficialMetadata.model_validate(metadata)
        except ValidationError as error:
            raise DomainError('官方拍摄信息格式无效') from error
        if model.source != source:
            raise DomainError('拍摄来源与授权接口不一致')
        case = self.domain.case(model.case_id)
        self.check_case(case, device, model.case_number)
        if model.project_name != case['project_name'] or model.location != location(case):
            raise DomainError('案件位置或小区已更新，请重新同步后拍摄', 409)
        stage = GROUP_LABELS[model.photo_group]
        if model.photo_group.endswith('checkin'):
            if model.role not in ('building_checkin', 'door_checkin'):
                raise DomainError('请选择楼栋或入户门用途')
            stage += ' · ' + ('楼栋' if model.role == 'building_checkin' else '入户门')
        elif model.role and model.role != GROUP_FIELDS[model.photo_group][1]:
            raise DomainError('拍摄环节与照片用途不一致')
        return model, {'案件号': model.case_number, '小区': model.project_name,
                       '位置': model.location, '环节': stage, '内容': model.content}

    def native_session(self, device, metadata):
        model, fields = self.session_metadata(device, metadata, 'xhey_sdk')
        signed, template = self.settings.h5_signature()
        options = {**signed, 'maxImageCount': 1, 'needPhotoConfirm': True,
                   'customInputItems': fields}
        if template:
            options['groupWatermarkId'] = template
        return {'capture_id': model.capture_id, 'sdk_version': '1.3.70.9b61b4a6',
                'media_type': model.media_type, 'max_record_seconds': MAX_RECORD_SECONDS,
                'options': options, 'has_team_watermark': bool(template)}

    def h5_session(self, device, metadata):
        model, fields = self.session_metadata(device, metadata, 'xhey_h5')
        if model.media_type != 'image':
            raise DomainError('H5 相机仅支持照片，请使用新版安卓相机录像')
        signed, template = self.settings.h5_signature()
        options = {**signed, 'maxImageCount': 1, 'disablePhotoConfirm': False,
                   'enableCustomWatermark': not bool(template), 'disableOffcialWatermark': False,
                   'watermarkScale': 0.68,
                   'disableWatermarkDragging': True, 'enableLogMessage': False,
                   'enableOssUpload': False, 'enableWorkgroupUpload': False,
                   'customInputItems': fields}
        if template:
            options['watermarkId'] = template
        return {'capture_id': model.capture_id, 'options': options,
                'has_team_watermark': bool(template)}

    def ingest(self, device, metadata, image):
        with self.camera.upload_lock, self.lock:
            return self._ingest(device, metadata, image)

    def _ingest(self, device, metadata, image):
        try:
            model = OfficialMetadata.model_validate_json(metadata)
        except ValidationError as error:
            raise DomainError('官方照片信息格式无效') from error
        # This is an untrusted client-side launch time, not evidence of photo time.
        # A wrong phone clock must not override (or pre-empt) the official verdict.
        if model.captured_at.tzinfo is None:
            raise DomainError('手机记录时间需要时区')
        _, role = GROUP_FIELDS[model.photo_group]
        if model.photo_group.endswith('checkin'):
            if model.role not in ('building_checkin', 'door_checkin'):
                raise DomainError('请选择楼栋或入户门用途')
        elif model.role and model.role != role:
            raise DomainError('拍摄环节与照片用途不一致')
        case = self.domain.case(model.case_id)
        if case['project_id'] not in device['project_ids']:
            raise DomainError('设备无此项目权限', 403)
        video = model.media_type == 'video'
        if video and model.source != 'xhey_sdk':
            raise DomainError('视频仅支持安卓原生 SDK 来源')
        media_info = inspect_mp4(image) if video else None
        if not video:
            if not image or len(image) >= MAX_PHOTO_BYTES:
                raise DomainError('每张官方照片须小于20 MiB；请勿压缩后重新上传', 413)
            self.camera.image_check(image)
        meta = model.model_dump(mode='json')
        # Preserve idempotency hashes for records created before these optional fields.
        for field in ('sdk_photo_time', 'sdk_anti_fake_code'):
            if not meta[field]:
                del meta[field]
        if not video:
            del meta['media_type']  # Keep existing photo request hashes stable.
        image_hash = hashlib.sha256(image).hexdigest()
        request_hash = digest(dump(meta) + image_hash)
        existing = self.db.one('SELECT * FROM camera_official_captures WHERE device_id=? AND capture_id=?', (device['id'], model.capture_id))
        if existing:
            return self.receipt(existing, request_hash, duplicate=True)
        self.check_case(case, device, model.case_number)
        blob = self.storage.put(image, 'xhey-sdk-original.mp4' if video else 'xhey-sdk-original.jpg')
        if media_info:
            with self.db.tx() as con:
                con.execute('UPDATE blobs SET width=?,height=? WHERE hash=?',
                            (media_info['width'], media_info['height'], blob['hash']))
        stamp = now()
        config = self.settings.load()
        # The configured verifier accepts photos only. Never send MP4 to it.
        requested = not video and config.get('auto_verify') is True
        state = ('queued' if self.settings.verification_ready(config) else 'unconfigured') if requested else 'not_requested'
        with self.db.tx() as con:
            existing = con.execute('SELECT * FROM camera_official_captures WHERE device_id=? AND capture_id=?', (device['id'], model.capture_id)).fetchone()
            if existing:
                return self.receipt(dict(existing), request_hash, duplicate=True)
            if con.execute('SELECT 1 FROM camera_captures WHERE device_id=? AND capture_id=?', (device['id'], model.capture_id)).fetchone():
                raise DomainError('拍摄编号已用于其他照片来源', 409)
            con.execute('''INSERT INTO camera_official_captures
                (device_id,capture_id,request_hash,blob_hash,metadata,state,next_attempt_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', (device['id'], model.capture_id, request_hash, blob['hash'], dump(meta), state, stamp, stamp, stamp))
            if not requested:
                row = dict(con.execute('SELECT * FROM camera_official_captures WHERE device_id=? AND capture_id=?',
                                       (device['id'], model.capture_id)).fetchone())
                # Capture, photo, provenance and receipt commit together. No vendor call.
                self.archive(row, device, meta, connection=con)
            audit(con, 'camera_official', model.capture_id, 'receive', after={'device_id': device['id'], 'sha256': image_hash, 'state': state})
        return self.get(device, model.capture_id)

    @staticmethod
    def check_case(case, device, number):
        if case['project_id'] not in device['project_ids']:
            raise DomainError('设备无此项目权限', 403)
        if case_number(case) != number or not number:
            raise DomainError('案件号与台账不一致，未归档', 409)
        if not case['followup_eligible'] or case['workflow_stage'] == 'closed':
            raise DomainError('案件当前不可接收相机照片，原片已保留', 409)

    def row(self, device_id, capture_id):
        row = self.db.one('SELECT * FROM camera_official_captures WHERE device_id=? AND capture_id=?', (device_id, capture_id))
        if not row:
            raise DomainError('官方照片记录不存在', 404)
        return row

    @staticmethod
    def receipt(row, request_hash=None, duplicate=False):
        if request_hash and request_hash != row['request_hash']:
            raise DomainError('同一拍摄编号的照片或信息不同，拒绝覆盖', 409)
        if row['archived_at'] and not row['photo_id']:
            raise DomainError('照片已在工作台删除，不会重新归档', 410)
        media_type = json.loads(row['metadata']).get('media_type', 'image')
        return {'capture_id': row['capture_id'], 'photo_id': row['photo_id'],
                'media_type': media_type, 'verification_supported': media_type == 'image',
                'state': 'uploaded' if row['state'] in ('verified', 'not_requested') and row['photo_id'] else 'received',
                'verification_state': row['state'], 'original_hash': row['blob_hash'], 'stamped_hash': row['blob_hash'],
                'duplicate': duplicate, 'received_at': row['created_at'], 'archived_at': row['archived_at'],
                'verified_at': row['archived_at'] if row['state'] == 'verified' else None,
                'error': row['error'], 'verification': json.loads(row['result']), 'report': json.loads(row['report'])}

    def get(self, device, capture_id):
        row = self.row(device['id'], capture_id)
        meta = json.loads(row['metadata'])
        case = self.domain.case(meta['case_id'])
        if case['project_id'] not in device['project_ids']:
            raise DomainError('设备无此项目权限', 403)
        return self.receipt(row)

    def update(self, row, **fields):
        fields['updated_at'] = now()
        with self.db.tx() as con:
            con.execute('UPDATE camera_official_captures SET ' + ','.join(key + '=?' for key in fields)
                        + ' WHERE device_id=? AND capture_id=?', (*fields.values(), row['device_id'], row['capture_id']))

    def retry(self, device_id, capture_id):
        with self.lock:
            row = self.row(device_id, capture_id)
            self.receipt(row)  # Honor deletion tombstones.
            if row['state'] not in ('error', 'unconfigured', 'cancelled'):
                raise DomainError('当前状态无需重试；未通过的原片请重新拍摄', 409)
            config = self.settings.load()
            if config.get('auto_verify') is not True or not self.settings.verification_ready(config):
                raise DomainError('请先在电脑端开启本地自动验真并配置验真授权', 409)
            if row['task_id'] and row['credential_group_hash'] != digest(config['group_key']):
                raise DomainError('验真账号已改变，请恢复原账号后查询已有任务', 409)
            if row['state'] == 'cancelled':
                raise DomainError('官方任务已取消，请核对后重新拍摄', 409)
            self.update(row, state='processing' if row['task_id'] else 'queued', attempts=0, error='', next_attempt_at=now())
            audit_fields = {'device_id': device_id, 'existing_task': bool(row['task_id'])}
            with self.db.tx() as con:
                audit(con, 'camera_official', capture_id, 'retry', after=audit_fields)
            return self.receipt(self.row(device_id, capture_id))

    def process_one(self):
        with self.lock:
            config = self.settings.load()
            if config.get('auto_verify') is not True or not self.settings.verification_ready(config):
                return False
            row = self.db.one("SELECT * FROM camera_official_captures WHERE state IN ('queued','processing') AND next_attempt_at<=? ORDER BY created_at LIMIT 1", (now(),))
            if not row:
                return False
            try:
                device = self.db.one('SELECT * FROM camera_devices WHERE id=? AND revoked_at IS NULL', (row['device_id'],))
                if not device:
                    raise DomainError('设备已停用，未继续验真或归档')
                device = self.camera.effective_device(device)
                meta = json.loads(row['metadata'])
                self.check_case(self.domain.case(meta['case_id']), device, meta['case_number'])
                client = self.client_factory(config)
                if row['state'] == 'queued':
                    with self.db.tx() as con:
                        claimed = con.execute("UPDATE camera_official_captures SET state='submitting',updated_at=? WHERE device_id=? AND capture_id=? AND state='queued'",
                                              (now(), row['device_id'], row['capture_id'])).rowcount
                    if not claimed:
                        return True
                    blob = self.db.one('SELECT stored_path FROM blobs WHERE hash=?', (row['blob_hash'],))
                    data = self.storage.resolve(blob['stored_path']).read_bytes()
                    if hashlib.sha256(data).hexdigest() != row['blob_hash']:
                        raise DomainError('原片哈希校验失败，未发送验真')
                    task_id = client.create(data)
                    self.update(row, state='processing', task_id=task_id, credential_group_hash=digest(config['group_key']), error='', next_attempt_at=now())
                    return True
                if row['credential_group_hash'] != digest(config['group_key']):
                    raise DomainError('验真账号已改变，请恢复原账号后查询已有任务')
                state, result = client.query(row['task_id'])
                if state == 'verified':
                    self.archive(row, device, meta, result)
                elif state == 'processing':
                    attempts = row['attempts'] + 1
                    if attempts >= 40:
                        raise XheyError('官方任务长时间未完成；原片保留，可手动继续查询原任务')
                    self.update(row, attempts=attempts, next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat(timespec='milliseconds'))
                else:
                    self.update(row, state=state, result=dump(result), error='官方验真未通过，请核对原片或重新拍摄' if state == 'rejected' else '官方任务已取消')
            except (XheyError, DomainError, OSError) as error:
                # In particular, a failed CREATE is never automatically replayed (possible billing).
                self.update(row, state='error', error=str(error) if not isinstance(error, OSError) else '原片无法读取，未归档')
            except Exception:
                self.update(row, state='error', error='验真处理异常，原片保留，请在电脑端核对')
                LOG.error('Official camera processing failed; capture=%s', row['capture_id'])
            return True

    def archive(self, row, device, meta, result=None, *, connection=None):
        device = self.camera.effective_device(device)
        blob = self.db.one('SELECT * FROM blobs WHERE hash=?', (row['blob_hash'],))
        image_bytes = self.storage.resolve(blob['stored_path']).read_bytes()
        if hashlib.sha256(image_bytes).hexdigest() != row['blob_hash']:
            raise DomainError('归档前原片哈希不一致，未归档')
        stamp, photo_id = now(), uid()
        stage, role = GROUP_FIELDS[meta['photo_group']]
        if meta['photo_group'].endswith('checkin'):
            role = meta['role']
        verified = result is not None
        state = 'verified' if verified else 'not_requested'
        video = meta.get('media_type') == 'video'
        if video and verified:
            raise DomainError('视频不支持照片验真接口', 409)
        photo_time = result['photoTime'] if verified else None if video else sdk_photo_time(meta)
        time_basis = 'xhey_verification' if verified else 'sdk_returned_unverified' if photo_time else 'unavailable'
        provenance = {**meta, 'original_hash': row['blob_hash'], 'device_id': device['id'], 'time_basis': time_basis,
                      'uploaded_by': device.get('user_id'), 'uploaded_by_name': device.get('user_name', ''),
                      'verification_state': state, 'verification': result or {}, 'verification_task_id': row['task_id'],
                      'archived_at': stamp, 'verified_at': stamp if verified else None,
                      'case_fields_basis': 'user_selected_not_verified_by_xhey'}
        with (self.db.tx() if connection is None else nullcontext(connection)) as con:
            current = con.execute('SELECT * FROM camera_official_captures WHERE device_id=? AND capture_id=?', (row['device_id'], row['capture_id'])).fetchone()
            if current['archived_at']:
                return  # Includes a deleted photo; it must never be recreated.
            current_device = con.execute('SELECT * FROM camera_devices WHERE id=? AND revoked_at IS NULL', (device['id'],)).fetchone()
            current_case = con.execute('SELECT * FROM cases WHERE id=?', (meta['case_id'],)).fetchone()
            if (not current_device or not current_case or current_case['project_id'] not in json.loads(current_device['project_ids'])
                    or case_number(dict(current_case)) != meta['case_number'] or current_case['workflow_stage'] == 'closed'):
                raise DomainError('归档时设备权限或案件状态已改变，未归档')
            # Followup eligibility is computed by Domain, not just the stage column.
            current_scope = self.camera.effective_device(current_device)
            self.check_case(self.domain.case(meta['case_id']), current_scope, meta['case_number'])
            identity = {'code': normalize_watermark_code(
                (result or {}).get('antiFakeCode') or meta.get('sdk_anti_fake_code')),
                'source': 'verified' if verified else 'sdk', 'confidence': 1, 'reason': ''}
            target, dedup_reason = None, ''
            if not video and self.dedup:
                target, dedup_reason = self.dedup.find(con, image_bytes, identity,
                    case_id=meta['case_id'], project_id=current_case['project_id'])
                if not target and not dedup_reason:
                    exact = [dict(p) for p in con.execute("""SELECT * FROM photos
                        WHERE blob_hash=? AND reuse_source_id IS NULL""", (row['blob_hash'],))]
                    # Also promote a byte-identical chat copy whose OCR did not find a code.
                    if (len(exact) == 1 and exact[0]['case_id'] in (None, meta['case_id'])
                            and exact[0]['project_id'] in (None, current_case['project_id'])
                            and not self.dedup.is_mobile(con, exact[0])):
                        target = exact[0]
            if not target and con.execute('SELECT 1 FROM photos WHERE blob_hash=?', (row['blob_hash'],)).fetchone():
                raise DomainError('这张原片已存在案件资料中，请人工核对，未重复归档')
            if target:
                photo_id = target['id']
                promoted = self.dedup.promote(con, target, blob)
                provenance.update(deduplicated=True, preferred_original=promoted,
                                  previous_hash=target['blob_hash'])
                if not target['case_id']:
                    con.execute('UPDATE photos SET case_id=?,project_id=?,revision=revision+1,updated_at=? WHERE id=?',
                                (meta['case_id'], current_case['project_id'], stamp, photo_id))
                # Preserve every existing classification, annotation and confirmation.
            else:
                con.execute('''INSERT INTO photos(id,blob_hash,project_id,case_id,stage,role,part,process,watermark_time,
                exif_time,notes,recognition_state,created_at,updated_at,photo_group,uploaded_by)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'recognized',?,?,?,?)''',
                        (photo_id, row['blob_hash'], current_case['project_id'], current_case['id'], stage, role, meta['part'],
                         meta['content'], photo_time, blob.get('exif_time'),
                         ('今日官方 SDK 视频原片；视频暂不支持本地验真' if video else
                          '今日官方验真通过' if verified else '今日官方 SDK 原片；未做本地验真')
                          + '；案件信息来自拍摄时选择，不代表官网业务审核完成', stamp, stamp, meta['photo_group'], device.get('user_id')))
            if not video and self.dedup:
                self.dedup.remember(con, photo_id, identity, dedup_reason)
                if dedup_reason:
                    con.execute("UPDATE photos SET recognition_state='conflict' WHERE id=?", (photo_id,))
            con.execute('''INSERT INTO photo_sources(id,photo_id,source_type,source_key,original_name,original_path,sender,received_at,metadata)
                VALUES(?,?,'xhey_camera',?,?,?,?,?,?)''',
                        (uid(), photo_id, digest(device['id'] + ':' + row['capture_id']),
                         meta['case_number'] + ('.mp4' if video else '.jpg'),
                         'xhey-camera://' + row['capture_id'], device['name'], stamp, dump(provenance)))
            con.execute("UPDATE camera_official_captures SET state=?,photo_id=?,archived_at=?,result=?,error='',updated_at=? WHERE device_id=? AND capture_id=?",
                        (state, photo_id, stamp, dump(result or {}), stamp, row['device_id'], row['capture_id']))
            audit(con, 'photo', photo_id, 'xhey_verified_capture' if verified else 'xhey_sdk_capture', after=provenance,
                  actor='user:' + device['user_id'] if device.get('user_id') else 'camera:' + device['id'])

    def report(self, device_id, capture_id):
        with self.lock:
            row = self.row(device_id, capture_id)
            self.receipt(row)
            config = self.settings.load()
            if row['state'] != 'verified' or not self.settings.verification_ready(config) or row['credential_group_hash'] != digest(config['group_key']):
                raise DomainError('仅可为当前验真账号的已通过照片生成报告', 409)
            try:
                report = self.client_factory(config).report(row['task_id'])
            except XheyError as error:
                raise DomainError(str(error), 502) from error
            self.update(row, report=dump(report))
            return report

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.closing.clear()
        with self.db.tx() as con:
            con.execute("UPDATE camera_official_captures SET state='error',error='创建任务时服务中断；结果可能未知，请核对后手动重试',updated_at=? WHERE state='submitting'", (now(),))

        def loop():
            while not self.closing.wait(3):
                try:
                    self.process_one()
                except Exception:
                    LOG.error('Official camera worker unavailable; check local configuration')
        self.thread = threading.Thread(target=loop, name='xhey-verification', daemon=True)
        self.thread.start()

    def stop(self):
        self.closing.set()
        if self.thread:
            self.thread.join(timeout=25)


def official_camera_routes(service):
    router = APIRouter()

    @router.get('/api/camera/sdk-config')
    def sdk_config(request: Request):
        service.camera.authenticate(request)
        # Never restore the old credential-export API. Both clients use signatures.
        return {'ready': False, 'message': '请安装 0.4.0 或更高版本，使用官方原生相机'}

    @router.get('/api/camera/native-config')
    def native_config(request: Request):
        service.camera.authenticate(request)
        return {**service.settings.h5_status(), 'mode': 'native', 'sdk_version': '1.3.70.9b61b4a6',
                'video_enabled': True, 'max_record_seconds': MAX_RECORD_SECONDS,
                'max_video_bytes': MAX_VIDEO_BYTES, 'video_verification_supported': False}

    @router.post('/api/camera/native-session')
    def native_session(request: Request, data: dict = Body(...)):
        result = service.native_session(service.camera.authenticate(request), data)
        from fastapi.responses import JSONResponse
        return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    @router.get('/api/camera/h5-config')
    def h5_config(request: Request):
        service.camera.authenticate(request)
        return service.settings.h5_status()

    @router.post('/api/camera/h5-session')
    def h5_session(request: Request, data: dict = Body(...)):
        result = service.h5_session(service.camera.authenticate(request), data)
        from fastapi.responses import JSONResponse
        return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    @router.get('/api/camera/admin/official')
    def admin(request: Request):
        camera_admin()
        rows = service.db.all('''SELECT c.*,d.name AS device_name FROM camera_official_captures c
            LEFT JOIN camera_devices d ON d.id=c.device_id
            WHERE c.archived_at IS NULL OR c.photo_id IS NOT NULL
            ORDER BY c.created_at DESC,c.device_id,c.capture_id DESC LIMIT 100''')
        captures = []
        for row in rows:
            meta = json.loads(row['metadata'])
            captures.append({**service.receipt(row), 'device_id': row['device_id'],
                             'device_name': row['device_name'] or '未知终端',
                             'case_number': meta['case_number'], 'photo_group': meta.get('photo_group', ''),
                             'stage_label': GROUP_LABELS.get(meta.get('photo_group'), '未知阶段')})
        return {'config': service.settings.status(), 'captures': captures}

    @router.put('/api/camera/admin/official/config')
    def configure(request: Request, data: dict = Body(...)):
        camera_admin()
        with service.lock:
            result = service.settings.save(data)
        with service.db.tx() as con:
            audit(con, 'camera_official_config', 'xhey', 'configure',
                  after={'enabled': result['enabled'], 'auto_verify': result['auto_verify']})
        return result

    @router.post('/api/camera/admin/official/{device_id}/{capture_id}/retry')
    def retry_admin(device_id: str, capture_id: str, request: Request):
        camera_admin()
        return service.retry(device_id, capture_id)

    @router.post('/api/camera/admin/official/{device_id}/{capture_id}/report')
    def report_admin(device_id: str, capture_id: str, request: Request):
        camera_admin()
        return service.report(device_id, capture_id)

    @router.post('/api/camera/official-captures')
    async def upload(request: Request, metadata: str = Form(...),
                     image: UploadFile | None = File(None), video: UploadFile | None = File(None)):
        device = service.camera.authenticate(request)
        if len(metadata) > 12000:
            raise DomainError('照片信息过长', 413)
        if (image is None) == (video is None):
            raise DomainError('每次请上传一张照片或一段视频')
        try:
            is_video = json.loads(metadata).get('media_type', 'image') == 'video'
        except (ValueError, AttributeError) as error:
            raise DomainError('拍摄信息格式无效') from error
        if is_video != (video is not None):
            raise DomainError('拍摄类型与上传文件不一致')
        file = video if is_video else image
        data = await file.read(MAX_VIDEO_BYTES if is_video else MAX_PHOTO_BYTES)
        return await run_in_threadpool(service.ingest, device, metadata, data)

    @router.get('/api/camera/official-captures/{capture_id}')
    def receipt(capture_id: str, request: Request):
        return service.get(service.camera.authenticate(request), capture_id)

    @router.post('/api/camera/official-captures/{capture_id}/retry')
    def retry(capture_id: str, request: Request):
        device = service.camera.authenticate(request)
        service.get(device, capture_id)
        return service.retry(device['id'], capture_id)

    return router
