"""Persist official originals without adding them to pending upload selections."""
from __future__ import annotations

import json
import threading
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from fastapi.responses import FileResponse

from .db import audit, now
from .domain import DomainError
from .platform_client import attachment_url
from .storage import ALLOWED_EXTS, safe_name


class PlatformMedia:
    def __init__(self, platform):
        self.platform = platform
        self.db, self.storage = platform.db, platform.storage
        self._locks = [threading.Lock() for _ in range(32)]

    def _receipt_blob(self, case_id, report_id, file_id, source_url):
        # Names are not identities. Only an exact upload receipt links an
        # official file to a local original, including across business stages.
        for row in self.db.all(
                "SELECT snapshot,result FROM platform_review_batches WHERE case_id=? "
                "AND state IN ('save_verified','verified') ORDER BY created_at DESC", (case_id,)):
            plan, result = json.loads(row['snapshot']), json.loads(row['result'])
            if plan['link']['report_receiving_id'] != report_id:
                continue
            originals = {p['id']: p['blob_hash'] for f in plan['file_fields'] for p in f['selected']}
            for photo_id, receipt in result.get('uploaded', {}).items():
                if str(receipt.get('id', '')) != file_id or photo_id not in originals:
                    continue
                try:
                    if attachment_url(receipt.get('fileUrl') or receipt.get('fullFileUrl')) != source_url:
                        continue
                except DomainError:
                    continue
                blob = self.db.one('SELECT * FROM blobs WHERE hash=?', (originals[photo_id],))
                if blob and self.storage.verify(blob):
                    return blob
        return None

    def ensure(self, plan, field_key, file_id):
        field = next((f for f in plan['file_fields'] if f['key'] == field_key), None)
        matches = [f for f in (field or {}).get('existing', []) if str(f.get('id', '')) == file_id
                   and str(f.get('invalid', '')).lower() not in ('1', 'true')]
        if not file_id or len(matches) != 1:
            raise DomainError('此栏位没有对应的官网附件，请刷新资料后重试', 404)
        record = matches[0]
        source_url = attachment_url(record.get('fileUrl') or record.get('fullFileUrl'))
        case_id, report_id = plan['case']['id'], plan['link']['report_receiving_id']
        if not report_id or not plan['link'].get('verified_at'):
            raise DomainError('案件尚未核对官网附件', 409)
        name = safe_name(record.get('oldFileName') or record.get('fileName') or record.get('name') or
                         PurePosixPath(urlsplit(source_url).path).name, 180)
        if PurePosixPath(name).suffix.lower() not in ALLOWED_EXTS:
            raise DomainError('此官网附件类型暂不支持同步预览', 422)
        key = (case_id, report_id, file_id, source_url)
        with self._locks[hash(key) % len(self._locks)]:
            cached = self.db.one(
                'SELECT b.* FROM platform_media m JOIN blobs b ON b.hash=m.blob_hash '
                'WHERE m.case_id=? AND m.report_receiving_id=? AND m.file_id=? AND m.source_url=?', key)
            blob = cached if cached and self.storage.verify(cached) else self._receipt_blob(*key)
            if not blob:
                # Project write/readback snapshots may have been read by a
                # different account. File browsing still uses the company
                # reader; pin that reader's current session for this download.
                account = (self.platform.client.status()['account']
                           if getattr(self.platform, 'project_accounts', None)
                           else plan['link'].get('snapshot', {}).get('account', ''))
                data = self.platform.client.download_attachment(
                    source_url, self.storage.settings.max_file_bytes, account)
                try:
                    blob = self.storage.put(data, name)
                except ValueError as error:
                    raise DomainError(str(error), 422) from error
            if blob['media_type'] == 'image' and (not blob.get('thumbnail_path') or
                    not self.storage.resolve(blob['thumbnail_path']).is_file()):
                # Restore a missing preview from the retained original.
                from PIL import Image, ImageOps
                import io
                with Image.open(self.storage.resolve(blob['stored_path'])) as image:
                    preview = ImageOps.exif_transpose(image).convert('RGB')
                    preview.thumbnail((640, 640))
                    output = io.BytesIO()
                    preview.save(output, 'JPEG', quality=82)
                blob['thumbnail_path'] = f"thumbnails/{blob['hash'][:2]}/{blob['hash']}.jpg"
                self.storage._atomic(self.storage.resolve(blob['thumbnail_path']), output.getvalue())
                with self.db.tx() as con:
                    con.execute('UPDATE blobs SET thumbnail_path=? WHERE hash=?', (blob['thumbnail_path'], blob['hash']))
            if not cached or cached['hash'] != blob['hash']:
                with self.db.tx() as con:
                    con.execute(
                        'INSERT INTO platform_media VALUES(?,?,?,?,?,?,?) '
                        'ON CONFLICT(case_id,report_receiving_id,file_id,source_url) DO UPDATE SET '
                        'blob_hash=excluded.blob_hash,original_name=excluded.original_name,synced_at=excluded.synced_at',
                        (*key, blob['hash'], name, now()))
                    audit(con, 'platform_media', file_id, 'sync',
                          after={'case_id': case_id, 'report_receiving_id': report_id, 'blob_hash': blob['hash'],
                                 'original_name': name, 'source_url': source_url}, actor='official_media_sync')
            return blob, name

    def response(self, plan, field_key, file_id, variant):
        if variant not in ('thumbnail', 'original'):
            raise DomainError('未知附件预览方式', 404)
        blob, name = self.ensure(plan, field_key, file_id)
        path = blob['thumbnail_path'] if variant == 'thumbnail' else blob['stored_path']
        if not path:
            raise DomainError('此附件没有图片缩略图，请查看原件', 404)
        return FileResponse(
            self.storage.resolve(path), media_type='image/jpeg' if variant == 'thumbnail' else blob['mime_type'],
            filename=name if variant == 'original' else None, content_disposition_type='inline',
            headers={'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})
