"""Native camera ingestion. A capture keeps its raw original and rendered derivative.

Device credentials only authorize the camera API and the selected projects. Capture
IDs are durable idempotency keys; a deleted photo is a tombstone, never reimported.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .db import audit, dump, now, uid
from .domain import DomainError, GROUP_FIELDS, WORKFLOW_STAGES
from .identity import current

BEIJING = timezone(timedelta(hours=8))
GROUP_LABELS = {'survey_checkin': '查勘打卡', 'survey_problem': '问题照片',
                'work_checkin': '施工打卡', 'construction': '施工过程', 'completion': '完工照片'}
MAX_PHOTO_BYTES = 20 * 1024 * 1024


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def location(case):
    if case.get('location_type') == 'public':
        return case.get('location_detail') or case.get('building') or '公共区域'
    return ' · '.join(f'{case[key]}{suffix}' for key, suffix in
                      (('building', '栋'), ('unit', '单元'), ('floor', '层'), ('room', '室')) if case.get(key)) or case.get('location_detail') or '位置待补'


def case_number(case):
    # Short codes and policy numbers are never substituted for a case number.
    return case.get('platform_report_code') or case.get('report_number') or ''


def is_local_admin_request(request):
    settings = request.app.state.services['settings']
    host = urlsplit('http://' + request.headers.get('host', '')).hostname
    local = request.client and (request.client.host in ('127.0.0.1', '::1', 'testclient') or
                               (request.client.host in settings.local_admin_proxies and host in ('127.0.0.1', 'localhost')))
    return bool(local and not request.headers.get('x-forwarded-proto'))


def camera_admin():
    """Use the authenticated role on every configured entry, including HTTPS."""
    person = current()
    if person is None:
        raise DomainError('请先登录 IDI', 401)
    if not person.admin:
        raise DomainError('手机配对管理需要系统管理员权限', 403)


class CaptureMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    schema_version: Literal[1] = 1
    capture_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')
    case_id: str = Field(min_length=1, max_length=80)
    case_number: str = Field(min_length=1, max_length=80)
    project_name: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=160)
    captured_at: datetime
    photo_group: Literal['survey_checkin', 'survey_problem', 'work_checkin', 'construction', 'completion']
    role: str = Field(default='', max_length=40)
    part: str = Field(default='', max_length=80)
    content: str = Field(min_length=1, max_length=240)
    app_version: str = Field(default='0.1.0', max_length=30)


class MobileCamera:
    def __init__(self, db, domain, storage):
        self.db, self.domain, self.storage = db, domain, storage
        self.upload_lock = threading.RLock()
        self.access = None

    def projects(self):
        return [{'id': row['id'], 'name': row['name']} for row in self.domain.projects()]

    def create_pairing(self, data):
        name = str(data.get('name', '')).strip()
        project_ids = data.get('project_ids')
        if not name or len(name) > 80:
            raise DomainError('请填写设备名称（最多80字）')
        valid = {row['id'] for row in self.projects()}
        if not isinstance(project_ids, list) or not project_ids or any(not isinstance(p, str) or p not in valid for p in project_ids):
            raise DomainError('请选择允许这台手机访问的项目')
        user_id = data.get('user_id')
        if self.access:
            person = self.access.principal(user_id)
            if any(not person.allows(p) for p in project_ids):
                raise DomainError('手机所选项目必须已分配给绑定人员', 403)
        code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(10))
        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec='milliseconds')
        with self.db.tx() as con:
            con.execute('DELETE FROM camera_pairings WHERE expires_at<?', (now(),))
            con.execute('INSERT INTO camera_pairings(code_hash,name,project_ids,expires_at,used_at,user_id) VALUES(?,?,?,?,NULL,?)',
                        (digest(code), name, dump(sorted(set(project_ids))), expires, user_id))
            audit(con, 'camera_pairing', digest(code), 'create', after={'name': name, 'project_ids': project_ids, 'user_id': user_id})
        return {'code': code, 'expires_at': expires, 'expires_in_minutes': 15}

    def pair(self, code):
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z2-9]{10}', code.strip().upper()):
            raise DomainError('配对码无效或已过期', 401)
        token = secrets.token_urlsafe(32)
        device_id = uid()
        with self.db.tx() as con:
            pair = con.execute('SELECT * FROM camera_pairings WHERE code_hash=? AND used_at IS NULL AND expires_at>?',
                               (digest(code.strip().upper()), now())).fetchone()
            if not pair:
                raise DomainError('配对码无效、已使用或已过期，请在电脑上重新生成', 401)
            if self.access:
                person = self.access.principal(pair['user_id'])
                if not any(person.allows(p) for p in json.loads(pair['project_ids'])):
                    raise DomainError('绑定人员已无所选项目权限，请重新生成配对码', 403)
            con.execute('UPDATE camera_pairings SET used_at=? WHERE code_hash=?', (now(), pair['code_hash']))
            con.execute('INSERT INTO camera_devices(id,name,token_hash,project_ids,created_at,last_seen_at,revoked_at,user_id) VALUES(?,?,?,?,?,NULL,NULL,?)',
                        (device_id, pair['name'], digest(token), pair['project_ids'], now(), pair['user_id']))
            audit(con, 'camera_device', device_id, 'pair', after={'name': pair['name']})
        return {'device_id': device_id, 'device_name': pair['name'], 'token': token, 'schema_version': 1,
                'user_id': pair['user_id'], 'user_name': person.display_name if self.access else ''}

    def effective_device(self, device):
        device = dict(device)
        allowed = json.loads(device['project_ids']) if isinstance(device['project_ids'], str) else device['project_ids']
        if self.access:
            person = self.access.principal(device.get('user_id'))
            allowed = [p for p in allowed if person.allows(p)]
            device['user_name'] = person.display_name
        if not allowed:
            raise DomainError('绑定人员已无可用项目权限，请联系管理员', 403)
        device['project_ids'] = allowed
        return device

    def authenticate(self, request):
        authorization = request.headers.get('Authorization', '')
        if not authorization.startswith('Bearer ') or len(authorization) > 200:
            raise DomainError('请先连接并配对这台手机', 401)
        device = self.db.one('SELECT * FROM camera_devices WHERE token_hash=? AND revoked_at IS NULL', (digest(authorization[7:]),))
        if not device:
            raise DomainError('设备连接已失效，请重新配对', 401)
        return self.effective_device(device)

    def cases(self, device):
        allowed = set(device['project_ids'])
        items = []
        for case in self.domain.cases():
            if case['project_id'] not in allowed or not case['followup_eligible'] or case['workflow_stage'] == 'closed':
                continue
            items.append({'id': case['id'], 'project_id': case['project_id'], 'project_name': case['project_name'],
                          'case_number': case_number(case), 'location': location(case), 'title': case['title'],
                          'parts': case['repair_parts'], 'workflow_stage': case['workflow_stage'],
                          'stage_label': WORKFLOW_STAGES.get(case['workflow_stage'], '待核对'),
                          'capture_enabled': bool(case_number(case)), 'revision': case['revision']})
        with self.db.tx() as con:
            con.execute('UPDATE camera_devices SET last_seen_at=? WHERE id=?', (now(), device['id']))
        return {'items': items, 'total': len(items), 'synced_at': now(), 'device_name': device['name']}

    @staticmethod
    def image_check(data):
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != 'JPEG' or image.width * image.height > 60_000_000:
                    raise DomainError('相机接口只接收不超过6000万像素的 JPEG 照片')
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise DomainError('照片损坏，无法读取') from error

    def ingest(self, device, metadata, original, stamped):
        # Windows may hold an existing blob open for verification while another
        # request replaces its identical bytes. Serialize this ingestion boundary.
        with self.upload_lock:
            return self._ingest(device, metadata, original, stamped)

    def _ingest(self, device, metadata, original, stamped):
        device = self.effective_device(device)
        try:
            model = CaptureMetadata.model_validate_json(metadata)
        except ValidationError as error:
            raise DomainError('照片信息格式无效：' + str(error.errors(include_url=False)[0]['loc'])) from error
        meta = model.model_dump(mode='json')
        if model.captured_at.tzinfo is None:
            raise DomainError('拍摄时间必须包含时区，不能使用上传时间代替')
        if model.captured_at > datetime.now(timezone.utc) + timedelta(minutes=10):
            raise DomainError('手机拍摄时间超过当前时间，请检查手机时钟')
        if model.captured_at.year < 2000:
            raise DomainError('手机拍摄时间无效，请检查手机时钟')
        stage, role = GROUP_FIELDS[model.photo_group]
        if model.photo_group.endswith('checkin'):
            if model.role not in ('building_checkin', 'door_checkin'):
                raise DomainError('打卡照片请选择楼栋或入户门用途')
            role = model.role
        elif model.role and model.role != role:
            raise DomainError('拍摄环节与照片用途不一致')
        request_hash = digest(dump(meta) + hashlib.sha256(original).hexdigest() + hashlib.sha256(stamped).hexdigest())
        case = self.domain.case(model.case_id)
        if case['project_id'] not in device['project_ids']:
            raise DomainError('这台手机没有该项目的上传权限', 403)
        existing = self.db.one('SELECT * FROM camera_captures WHERE device_id=? AND capture_id=?', (device['id'], model.capture_id))
        if existing:
            return self.receipt(existing, request_hash, duplicate=True)
        if not case_number(case) or model.case_number != case_number(case):
            raise DomainError('案件号与台账不一致，照片已保留在手机，请核对后再处理', 409)
        for data in (original, stamped):
            if not data or len(data) >= MAX_PHOTO_BYTES:
                raise DomainError('每张照片须小于20 MiB', 413)
            self.image_check(data)
        raw_blob = self.storage.put(original, 'camera-original.jpg')
        output_blob = self.storage.put(stamped, 'camera-watermark.jpg')
        snapshot_differences = []
        if model.project_name != case['project_name']:
            snapshot_differences.append('手机缓存项目名与当前台账不同')
        if model.location != location(case):
            snapshot_differences.append('手机缓存位置与当前台账不同')
        photo_id, stamp = uid(), now()
        captured = model.captured_at.astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        provenance = {**meta, 'device_id': device['id'], 'device_name': device['name'],
                      'uploaded_by': device.get('user_id'), 'uploaded_by_name': device.get('user_name', ''),
                      'original_hash': raw_blob['hash'],
                      'stamped_hash': output_blob['hash'], 'snapshot_differences': snapshot_differences,
                      'time_basis': 'device_capture', 'watermark_order': ['time', 'case_number', 'project', 'location', 'stage', 'content']}
        with self.db.tx() as con:
            # Recheck under the write lock: parallel requests return one receipt.
            if con.execute('SELECT 1 FROM camera_official_captures WHERE device_id=? AND capture_id=?', (device['id'], model.capture_id)).fetchone():
                raise DomainError('拍摄编号已用于官方照片', 409)
            concurrent = con.execute('SELECT * FROM camera_captures WHERE device_id=? AND capture_id=?', (device['id'], model.capture_id)).fetchone()
            if concurrent:
                return self.receipt(dict(concurrent), request_hash, duplicate=True)
            same_image = con.execute('SELECT id FROM photos WHERE case_id=? AND blob_hash=? AND reuse_source_id IS NULL', (case['id'], output_blob['hash'])).fetchone()
            if same_image:
                photo_id = same_image['id']
            else:
                con.execute('''INSERT INTO photos(id,blob_hash,project_id,case_id,stage,role,part,process,watermark_time,
                            exif_time,notes,recognition_state,created_at,updated_at,photo_group,uploaded_by)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,'conflict',?,?,?,?)''',
                            (photo_id, output_blob['hash'], case['project_id'], case['id'], stage, role, model.part,
                             model.content, captured, output_blob.get('exif_time'), '自行绘制水印，未经今日官方验真；不可作为保司合规拍摄凭证。' + '；'.join(snapshot_differences), stamp, stamp, model.photo_group, device.get('user_id')))
                audit(con, 'photo', photo_id, 'camera_capture', after=provenance, actor='camera:' + device['id'])
            source_key = digest(device['id'] + ':' + model.capture_id)
            con.execute('''INSERT OR IGNORE INTO photo_sources
                        (id,photo_id,source_type,source_key,original_name,original_path,sender,received_at,metadata)
                        VALUES(?,?,'camera',?,?,?,?,?,?)''',
                        (uid(), photo_id, source_key, f'{model.case_number}_{model.capture_id}.jpg',
                         'camera://' + device['id'] + '/' + model.capture_id, device['name'], stamp, dump(provenance)))
            con.execute('INSERT INTO camera_captures VALUES(?,?,?,?,?,?,?,?)',
                        (device['id'], model.capture_id, request_hash, photo_id, raw_blob['hash'], output_blob['hash'], dump(provenance), stamp))
            con.execute('UPDATE camera_devices SET last_seen_at=? WHERE id=?', (stamp, device['id']))
        return {'capture_id': model.capture_id, 'photo_id': photo_id, 'state': 'uploaded', 'duplicate': False,
                'original_hash': raw_blob['hash'], 'stamped_hash': output_blob['hash'], 'received_at': stamp}

    @staticmethod
    def receipt(row, request_hash, duplicate):
        if row['request_hash'] != request_hash:
            raise DomainError('同一拍摄编号对应了不同照片或信息，已拒绝覆盖', 409)
        if not row['photo_id']:
            raise DomainError('这张照片已在工作台删除，不会重复导入', 410)
        return {'capture_id': row['capture_id'], 'photo_id': row['photo_id'], 'state': 'uploaded', 'duplicate': duplicate,
                'original_hash': row['original_hash'], 'stamped_hash': row['stamped_hash'], 'received_at': row['received_at']}


def camera_routes(service):
    router = APIRouter()

    @router.get('/camera-setup', response_class=HTMLResponse)
    def setup(request: Request):
        camera_admin()
        return (Path(__file__).parent / 'camera_setup.html').read_text(encoding='utf-8')

    @router.get('/camera/download')
    def download():
        path = service.storage.settings.data_dir / 'mobile-app' / 'idi-camera.apk'
        if not path.is_file():
            raise DomainError('安卓安装包尚未发布，请联系电脑端管理员', 404)
        return FileResponse(path, media_type='application/vnd.android.package-archive', filename='IDI-Camera.apk',
                            headers={'Cache-Control': 'no-store'})

    @router.get('/api/camera/admin')
    def admin(request: Request):
        camera_admin()
        devices = service.db.all('SELECT d.id,d.name,d.project_ids,d.created_at,d.last_seen_at,d.revoked_at,d.user_id,u.display_name AS user_name FROM camera_devices d LEFT JOIN idi_users u ON u.id=d.user_id ORDER BY d.created_at DESC')
        for device in devices:
            device['project_ids'] = json.loads(device['project_ids'])
        return {'projects': service.projects(), 'devices': devices, 'users': service.access.users() if service.access else [],
                'server_urls': list(service.storage.settings.public_origins) or service.storage.settings.lan_urls}

    @router.post('/api/camera/admin/pairings')
    def pairing(request: Request, data: dict = Body(...)):
        camera_admin()
        return service.create_pairing(data)

    @router.delete('/api/camera/admin/devices/{device_id}')
    def revoke(device_id: str, request: Request):
        camera_admin()
        with service.db.tx() as con:
            result = con.execute('UPDATE camera_devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL', (now(), device_id))
            if not result.rowcount:
                raise DomainError('设备不存在或已停用', 404)
            audit(con, 'camera_device', device_id, 'revoke')
        return {'revoked': True}

    @router.post('/api/camera/pair')
    def pair(data: dict = Body(...)):
        return service.pair(data.get('code'))

    @router.get('/api/camera/cases')
    def cases(request: Request):
        return service.cases(service.authenticate(request))

    @router.post('/api/camera/captures')
    async def capture(request: Request, metadata: str = Form(...), original: UploadFile = File(...), stamped: UploadFile = File(...)):
        device = service.authenticate(request)
        if len(metadata) > 12000:
            raise DomainError('照片信息过长', 413)
        raw = await original.read(MAX_PHOTO_BYTES)
        output = await stamped.read(MAX_PHOTO_BYTES)
        return await run_in_threadpool(service.ingest, device, metadata, raw, output)

    @router.get('/api/camera/captures/{capture_id}/original')
    def raw_original(capture_id: str, request: Request):
        device = service.authenticate(request)
        item = service.db.one('SELECT * FROM camera_captures WHERE device_id=? AND capture_id=? AND photo_id IS NOT NULL', (device['id'], capture_id))
        if not item:
            raise DomainError('照片不存在', 404)
        photo = service.domain.photo(item['photo_id'])
        if photo['project_id'] not in device['project_ids']:
            raise DomainError('设备无此项目权限', 403)
        blob = service.db.one('SELECT * FROM blobs WHERE hash=?', (item['original_hash'],))
        return FileResponse(service.storage.resolve(blob['stored_path']), media_type='image/jpeg', filename='original.jpg')

    return router
