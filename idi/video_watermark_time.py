"""Read a video's first-frame watermark; never infer time from the file or phone clock."""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

MIN_CONFIDENCE = .90
MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_FRAME_PIXELS = 3840 * 2160
MAX_FRAME_SIDE = 4096
MAX_FRAME_BYTES = 32 * 1024 * 1024
DECODE_TIMEOUT_SECONDS = 15
_LABEL = re.compile(r'^\s*(?:(?:拍摄)?时\s*)?间\s*:?\s*')
_TIME = re.compile(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})\s*(\d{2}):(\d{2}):(\d{2})(?!\d)')
_ANCHORS = {
    'location': re.compile(r'^\s*地\s*点\s*:\s*\S'),
    'case': re.compile(r'^\s*(?:案\s*)?件\s*号\s*:\s*ZLBH\d{10,}'),
    'community': re.compile(r'^\s*小\s*区\s*:\s*\S'),
    'position': re.compile(r'^\s*位\s*置\s*:\s*\S'),
}


class DecodeError(ValueError):
    pass


def _result(state, reason, *, watermark_time=None, confidence=None, evidence=None):
    return {'state': state, 'watermark_time': watermark_time,
            'time_basis': 'video_watermark_ocr', 'method': 'first_frame',
            'confidence': confidence, 'evidence': evidence or {}, 'reason': reason}


def _local_path(value):
    raw = os.fspath(value)
    if not isinstance(raw, str) or '\x00' in raw or '://' in raw or raw.startswith(('//', '\\\\')):
        raise DecodeError('invalid_video_path')
    path = Path(raw).resolve(strict=True)
    if str(path).startswith(('//', '\\\\')):
        raise DecodeError('invalid_video_path')
    return path


def _video_path(value):
    path = _local_path(value)
    if path.suffix.lower() != '.mp4' or not path.is_file():
        raise DecodeError('invalid_video_path')
    if not 0 < path.stat().st_size < MAX_VIDEO_BYTES:
        raise DecodeError('invalid_video_size')
    return path


def _valid_dimensions(width, height):
    return (math.isfinite(width) and math.isfinite(height)
            and 0 < width <= MAX_FRAME_SIDE and 0 < height <= MAX_FRAME_SIDE
            and width * height <= MAX_FRAME_PIXELS)


def _decode_first_frame(source, target):
    """Keep native codec failures and hangs outside the service process."""
    env = {**os.environ, 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    try:
        subprocess.run([sys.executable, '-m', 'idi.video_watermark_time', '--decode',
                        str(source), str(target)], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=DECODE_TIMEOUT_SECONDS,
                       cwd=Path(__file__).resolve().parent.parent, env=env, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise DecodeError('decode_timeout') from error
    except (OSError, subprocess.CalledProcessError) as error:
        raise DecodeError('decode_failed') from error


def _decode_cli(source, target):
    """Decode exactly the first output frame; do not seek or sample later frames."""
    source = _video_path(source)
    target = Path(target).resolve()
    if target.suffix != '.png' or target.exists() or not target.parent.is_dir():
        raise DecodeError('invalid_frame_path')
    import cv2
    cv2.setNumThreads(1)
    capture = cv2.VideoCapture(str(source))
    try:
        if not capture.isOpened():
            raise DecodeError('decode_failed')
        width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if not _valid_dimensions(width, height):
            raise DecodeError('frame_dimensions_exceeded')
        ok, frame = capture.read()
        if not ok or frame is None or not _valid_dimensions(frame.shape[1], frame.shape[0]):
            raise DecodeError('decode_failed')
        if not cv2.imwrite(str(target), frame, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
            raise DecodeError('decode_failed')
        if target.stat().st_size > MAX_FRAME_BYTES:
            target.unlink()
            raise DecodeError('frame_size_exceeded')
    finally:
        capture.release()


def _ocr_lines(lines):
    clean = []
    for index, line in enumerate(lines[:200]):
        if not isinstance(line, dict) or not isinstance(line.get('text'), str):
            continue
        try:
            confidence = float(line['confidence'])
            box = [[float(x), float(y)] for x, y in line['box']]
            if (not math.isfinite(confidence) or not 0 <= confidence <= 1 or len(box) != 4
                    or not all(math.isfinite(v) and 0 <= v <= MAX_FRAME_SIDE for p in box for v in p)):
                continue
            left, right = min(p[0] for p in box), max(p[0] for p in box)
            top, bottom = min(p[1] for p in box), max(p[1] for p in box)
            if right <= left or bottom <= top:
                continue
        except (TypeError, ValueError, KeyError):
            continue
        text = line['text'][:1000]
        clean.append({'index': index, 'text': text, 'confidence': confidence,
                      'box': box, '_text': unicodedata.normalize('NFKC', text),
                      '_rect': (left, top, right, bottom)})
    return clean


def _same_row_right(previous, other):
    left, top, right, bottom = previous['_rect']
    x1, y1, x2, y2 = other['_rect']
    height = min(bottom - top, y2 - y1)
    overlap = min(bottom, y2) - max(top, y1)
    return x1 >= right - height * .2 and x1 - right <= height * 2.5 and overlap >= height * .6


def _watermark_anchors(candidate, lines):
    """Separate an SDK field block from dates on signs elsewhere in the scene."""
    left, top, _, bottom = candidate['_rect']
    height = bottom - top
    found = []
    for line in lines:
        x1, y1, _, y2 = line['_rect']
        if (line['confidence'] < MIN_CONFIDENCE or abs(x1 - left) > height * 3
                or not height * .5 <= y1 - top <= height * 8
                or not height * .4 <= y2 - y1 <= height * 2):
            continue
        for kind, pattern in _ANCHORS.items():
            if pattern.match(line['_text']):
                found.append((kind, line))
                break
    # Two distinct field types on distinct rows must support the candidate.
    if not any(kind != other_kind and abs(line['_rect'][1] - other['_rect'][1]) >= height * .5
               for kind, line in found for other_kind, other in found):
        return []
    return [line for _, line in found]


def parse_watermark_time(raw_text, lines):
    """Require complete date/time digits and corroborating watermark field geometry."""
    if not isinstance(raw_text, str) or not isinstance(lines, list) or len(lines) > 200:
        return _result('needs_review', 'invalid_ocr_evidence')
    clean = _ocr_lines(lines)
    evidence = {'raw_text': raw_text[:20000],
                'lines': [{k: v for k, v in line.items() if not k.startswith('_')} for line in clean],
                'matches': []}
    labels = [line for line in clean if _LABEL.match(line['_text'])]
    if not labels:
        return _result('needs_review', 'time_label_missing', evidence=evidence)
    invalid = False
    supported = False
    for label in labels:
        anchors = _watermark_anchors(label, clean)
        if not anchors:
            continue
        supported = True
        group = [label]
        # OCR can split a single visual row into label, date, and time boxes.
        for other in sorted(clean, key=lambda line: line['_rect'][0]):
            if other is not label and _same_row_right(group[-1], other):
                group.append(other)
        text = ' '.join(line['_text'] for line in group)
        label_end = _LABEL.match(text).end()
        nearby = text[label_end:]
        matches = list(_TIME.finditer(nearby))
        if not matches or nearby[:matches[0].start()].strip():
            continue
        confidence = min(line['confidence'] for line in [*group, *anchors])
        for match in matches:
            try:
                value = datetime(*(int(part) for part in match.groups())).isoformat(' ')
            except ValueError:
                invalid = True
                continue
            evidence['matches'].append({'watermark_time': value, 'confidence': confidence,
                                        'line_indexes': [line['index'] for line in group],
                                        'anchor_indexes': [line['index'] for line in anchors]})
    matches = evidence['matches']
    values = {match['watermark_time'] for match in matches}
    confidence = min((match['confidence'] for match in matches), default=None)
    if not supported:
        return _result('needs_review', 'watermark_context_missing', evidence=evidence)
    if len(values) > 1:
        return _result('needs_review', 'conflicting_times', confidence=confidence, evidence=evidence)
    if invalid:
        return _result('needs_review', 'invalid_calendar_time', confidence=confidence, evidence=evidence)
    if not matches:
        return _result('needs_review', 'time_not_complete', evidence=evidence)
    if confidence < MIN_CONFIDENCE:
        return _result('needs_review', 'low_confidence', confidence=confidence, evidence=evidence)
    return _result('recognized', '', watermark_time=matches[0]['watermark_time'],
                   confidence=confidence, evidence=evidence)


def extract_video_watermark_time(path, local_ocr, *, decoder=None):
    """Use only the supplied local OCR callable; original video bytes remain untouched."""
    try:
        source = _video_path(path)
    except (OSError, TypeError, ValueError) as error:
        reason = str(error) if isinstance(error, DecodeError) else 'invalid_video_path'
        return _result('failed', reason)
    try:
        with tempfile.TemporaryDirectory(prefix='idi-video-first-frame-') as folder:
            frame_path = Path(folder) / 'first-frame.png'
            (decoder or _decode_first_frame)(source, frame_path)
            if not frame_path.is_file() or not 0 < frame_path.stat().st_size <= MAX_FRAME_BYTES:
                raise DecodeError('invalid_frame_file')
            from PIL import Image
            with Image.open(frame_path) as picture:
                if picture.format != 'PNG' or not _valid_dimensions(*picture.size):
                    raise DecodeError('frame_dimensions_exceeded')
                picture.verify()
            try:
                raw_text, lines = local_ocr(frame_path)
            except Exception:
                return _result('failed', 'ocr_failed')
            return parse_watermark_time(raw_text, lines)
    except DecodeError as error:
        return _result('failed', str(error))
    except Exception:
        return _result('failed', 'decode_failed')


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--decode':
        raise SystemExit(2)
    try:
        _decode_cli(sys.argv[2], sys.argv[3])
    except Exception:
        raise SystemExit(1)
