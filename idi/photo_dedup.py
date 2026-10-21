"""Match exact capture codes and image content; prefer SDK originals over chat copies."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

from .db import audit, dump, now, uid

log = logging.getLogger(__name__)
CODE_PATTERN = re.compile(r'(?<![A-Z0-9])([A-Z0-9]{14})(?![A-Z0-9])')
MIN_CONFIDENCE = 0.95


def normalize_watermark_code(value):
    """Whitespace/case only. Never guess O/0, I/1 or incomplete characters."""
    value = re.sub(r'\s+', '', value or '').upper()
    return value if re.fullmatch(r'[A-Z0-9]{14}', value) else ''


def watermark_from_ocr(text, evidence=None):
    candidates = {code for code in CODE_PATTERN.findall(text.upper()) if re.search(r'[A-Z]', code)}
    # A labelled code may have spaces inserted between characters.
    for line in text.splitlines():
        labelled = re.search(r'(?:防伪码|水印码)\s*[:：]?\s*(.+)$', line)
        if labelled:
            code = normalize_watermark_code(labelled[1])
            if code:
                candidates.add(code)
    if len(candidates) != 1:
        return {'code': '', 'source': 'ocr', 'confidence': 0,
                'reason': '照片包含多个水印码，请人工核对' if candidates else ''}
    code = candidates.pop()
    scores = [float(line.get('confidence', 0)) for line in (evidence or [])
              if code in re.sub(r'\s+', '', line.get('text', '')).upper()]
    confidence = min(scores) if scores else 0
    return {'code': code, 'source': 'ocr', 'confidence': confidence,
            'reason': '' if confidence >= MIN_CONFIDENCE else '水印码识别不够清晰，请人工核对'}


def same_picture(first, second):
    """A conservative check for resizing/recompression, not general scene similarity."""
    if first == second:
        return True
    try:
        with Image.open(io.BytesIO(first)) as a, Image.open(io.BytesIO(second)) as b:
            if a.width * a.height > Image.MAX_IMAGE_PIXELS or b.width * b.height > Image.MAX_IMAGE_PIXELS:
                return False
            a, b = ImageOps.exif_transpose(a).convert('RGB'), ImageOps.exif_transpose(b).convert('RGB')
            if abs((a.width / a.height) / (b.width / b.height) - 1) > 0.01:
                return False
            a = a.resize((96, 96), Image.Resampling.LANCZOS)
            b = b.resize((96, 96), Image.Resampling.LANCZOS)
            difference = ImageStat.Stat(ImageChops.difference(a, b))
            return max(difference.mean) <= 5 and max(difference.rms) <= 12
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


class PhotoDedup:
    def __init__(self, db, storage, recognizer):
        self.db, self.storage, self.recognizer = db, storage, recognizer

    def backfill(self):
        """Index existing evidence only; never OCR or merge historical archives here."""
        rows = self.db.all("""SELECT p.id,p.blob_hash FROM photos p JOIN blobs b ON b.hash=p.blob_hash
            WHERE p.watermark_code='' AND p.reuse_source_id IS NULL AND b.media_type='image'""")
        for photo in rows:
            identity = None
            sources = self.db.all("SELECT metadata FROM photo_sources WHERE photo_id=? AND source_type='xhey_camera'",
                                  (photo['id'],))
            codes = set()
            for source in sources:
                meta = json.loads(source['metadata'])
                if meta.get('original_hash') != photo['blob_hash']:
                    continue
                code = normalize_watermark_code(meta.get('verification', {}).get('antiFakeCode')
                                                or meta.get('sdk_anti_fake_code'))
                if code:
                    codes.add(code)
            if len(codes) == 1:
                identity = {'code': codes.pop(), 'source': 'sdk', 'confidence': 1, 'reason': ''}
            elif not codes:
                record = self.db.one("""SELECT raw_text,suggestions FROM recognitions
                    WHERE photo_id=? AND provider='local_ocr' AND state='completed'
                    ORDER BY created_at DESC,rowid DESC LIMIT 1""", (photo['id'],))
                if record:
                    suggestions = json.loads(record['suggestions'])
                    if suggestions.get('blob_hash', photo['blob_hash']) == photo['blob_hash']:
                        identity = watermark_from_ocr(record['raw_text'], suggestions.get('evidence'))
            if identity and identity['code']:
                with self.db.tx() as con:
                    current = con.execute('SELECT blob_hash,watermark_code FROM photos WHERE id=?', (photo['id'],)).fetchone()
                    if current and current['blob_hash'] == photo['blob_hash'] and not current['watermark_code']:
                        self.remember(con, photo['id'], identity)

    def prepare(self, data, path, *, case_id=None, project_id=None):
        """Read the same incoming bytes once. Failure falls back to ordinary archival."""
        from .recognition import extract_fields
        if not self.db.setting('recognition', {}).get('local_ocr', True):
            return None
        try:
            # OCR must not run on an unchecked image or a changing inbox file.
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width * picture.height > Image.MAX_IMAGE_PIXELS:
                    return None
                picture.verify()
            digest = hashlib.sha256(data).hexdigest()
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return None
            text, evidence = self.recognizer.local_ocr(path)
            identity = watermark_from_ocr(text, evidence)
            if identity['confidence'] < MIN_CONFIDENCE:
                text, evidence, identity = self.refine_small_watermark(data, text, evidence, identity)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return None
            fields = extract_fields(text)
            candidates, matched = self.recognizer.match(text, fields, project_id)
            explicit = any(fields.get(k) for k in ('platform_report_code', 'short_code', 'identifier_conflict'))
            scope_conflict = (bool(explicit and not matched)
                              or bool(case_id and matched and matched != case_id)
                              or any(c.get('needs_review') for c in candidates))
            return {'blob_hash': digest, 'raw_text': text, 'evidence': evidence, 'fields': fields,
                    'identity': identity,
                    'case_id': case_id or matched, 'project_id': project_id,
                    'scope_conflict': scope_conflict}
        except Exception:
            # Preserve the attachment even when OCR is unavailable or temporarily fails.
            log.info('Watermark preflight unavailable; using normal image import', exc_info=False)
            return None

    def refine_small_watermark(self, data, text, evidence, identity):
        """Retry tiny footer lettering locally; never substitute a known database code."""
        try:
            with Image.open(io.BytesIO(data)) as picture:
                picture = ImageOps.exif_transpose(picture).convert('RGB')
                top = int(picture.height * 0.65)
                crop = picture.crop((0, top, picture.width, picture.height))
                scale = min(3, 2560 / crop.width)
                crop = crop.resize((round(crop.width * scale), round(crop.height * scale)),
                                   Image.Resampling.LANCZOS)
            staging = (self.storage.settings.data_dir / 'staging').resolve()
            if not staging.is_relative_to(self.storage.settings.data_dir.resolve()):
                return text, evidence, identity
            staging.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='watermark-ocr-', dir=staging) as folder:
                path = Path(folder) / 'footer.png'
                crop.save(path)
                cropped_text, cropped_lines = self.recognizer.local_ocr(path)
            refined = watermark_from_ocr(cropped_text, cropped_lines)
            if not refined['code'] or refined['confidence'] <= identity['confidence']:
                return text, evidence, identity
            # Replace the OCR of that region, retaining exact coordinates in the full image.
            lines = [line for line in evidence if not line.get('box')
                     or sum(p[1] for p in line['box']) / len(line['box']) < top]
            lines += [{**line, 'method': 'enlarged_footer',
                       'box': [[round(x / scale, 2), round(y / scale + top, 2)] for x, y in line['box']]}
                      for line in cropped_lines if line.get('box')]
            merged_text = '\n'.join(line['text'] for line in lines)
            merged = watermark_from_ocr(merged_text, lines)
            if merged['code'] == refined['code']:
                return merged_text, lines, merged
        except Exception:
            log.info('Small watermark retry unavailable; retaining initial OCR', exc_info=False)
        return text, evidence, identity

    def find(self, con, data, identity, *, case_id=None, project_id=None, scope_conflict=False):
        code = identity.get('code', '')
        reason = identity.get('reason', '')
        if not code or identity.get('confidence', 0) < MIN_CONFIDENCE:
            return None, reason
        if scope_conflict:
            return None, '照片的案件编号或位置存在冲突，请人工核对'
        candidates = [dict(r) for r in con.execute("""SELECT p.*,b.stored_path FROM photos p
            JOIN blobs b ON b.hash=p.blob_hash WHERE p.watermark_code=? AND p.reuse_source_id IS NULL
            AND b.media_type='image'""", (code,))]
        if not candidates:
            return None, reason
        if case_id and not project_id:
            case = con.execute('SELECT project_id FROM cases WHERE id=?', (case_id,)).fetchone()
            project_id = case['project_id'] if case else None
        eligible = [p for p in candidates
                    if (not case_id or p['case_id'] in (None, case_id))
                    and (not project_id or p['project_id'] in (None, project_id))]
        if len(eligible) != 1 or len(candidates) != 1:
            return None, '水印码对应多个记录或不同案件，请人工核对'
        target = eligible[0]
        if target['recognition_state'] == 'conflict':
            return None, '已有照片的归属或识别信息存在冲突，请人工核对'
        if case_id and not target['case_id'] and 'case_id' in json.loads(target['confirmed_fields']):
            return None, '已有照片已人工确认暂不归案，请人工核对'
        if target['watermark_code_confidence'] < MIN_CONFIDENCE or target['watermark_dedup_reason']:
            return None, '已有照片的水印码尚待核对，未自动合并'
        if not self.storage.verify({'blob_hash': target['blob_hash']}):
            return None, '已有照片原件校验失败，未自动合并'
        if not same_picture(data, self.storage.resolve(target['stored_path']).read_bytes()):
            return None, '水印码相同，但照片画面存在差异，请人工核对'
        return target, ''

    @staticmethod
    def remember(con, photo_id, identity, reason=None):
        code = identity.get('code', '')
        if not code and not reason:
            return
        con.execute("""UPDATE photos SET watermark_code=?,watermark_code_source=?,
            watermark_code_confidence=?,watermark_dedup_reason=? WHERE id=?""",
                    (code, identity.get('source', ''), identity.get('confidence', 0),
                     identity.get('reason', '') if reason is None else reason, photo_id))

    @staticmethod
    def cache(con, prepared):
        if prepared:
            con.execute('INSERT OR IGNORE INTO photo_ocr_cache VALUES(?,?,?,?)',
                        (prepared['blob_hash'], prepared['raw_text'], dump(prepared['evidence']), now()))

    @staticmethod
    def is_mobile(con, photo):
        return bool(con.execute("""SELECT 1 FROM photo_sources WHERE photo_id=? AND source_type='xhey_camera'
            AND json_extract(metadata,'$.original_hash')=?""", (photo['id'], photo['blob_hash'])).fetchone())

    def promote(self, con, target, blob):
        """Keep IDs/annotations/selections and supersede only the primary image version."""
        if target['blob_hash'] == blob['hash'] or self.is_mobile(con, target):
            return False
        stamp = now()
        photos = [target, *[dict(p) for p in con.execute(
            'SELECT * FROM photos WHERE reuse_source_id=? AND blob_hash=?',
            (target['id'], target['blob_hash']))]]
        for photo in photos:
            con.execute('INSERT INTO photo_file_versions VALUES(?,?,?,?,?,?,?)',
                        (uid(), photo['id'], photo['blob_hash'], blob['hash'], dump(photo),
                         '手机原片替换已有导入版本', stamp))
            con.execute('UPDATE photos SET blob_hash=?,revision=revision+1,updated_at=? WHERE id=?',
                        (blob['hash'], stamp, photo['id']))
            con.execute("""UPDATE platform_manual_file_selections SET blob_hash=?,photo_revision=?
                WHERE photo_id=? AND blob_hash=? AND photo_revision=?""",
                        (blob['hash'], photo['revision'] + 1, photo['id'], photo['blob_hash'], photo['revision']))
            # Keep previous OCR evidence, but never use it as OCR of the new bytes.
            con.execute("UPDATE recognitions SET state='superseded' WHERE photo_id=? AND state='completed'", (photo['id'],))
            con.execute("""UPDATE photo_ai_tasks SET state='cancelled',error='原图已更新，等待重新分析',updated_at=?
                WHERE photo_id=? AND state IN ('queued','waiting')""", (stamp, photo['id']))
            audit(con, 'photo', photo['id'], 'prefer_mobile_original',
                  {'blob_hash': photo['blob_hash'], 'revision': photo['revision']},
                  {'blob_hash': blob['hash'], 'revision': photo['revision'] + 1}, actor='camera')
        for case_id in {p['case_id'] for p in photos} - {None}:
            con.execute("""UPDATE platform_review_batches SET state='stale'
                WHERE case_id=? AND state IN ('prepared','reviewed','failed')""", (case_id,))
            con.execute("""UPDATE automatic_upload_cases SET signature='',review_guard='',retry_at=0,updated_at=?
                WHERE case_id=?""", (stamp, case_id))
        return True
