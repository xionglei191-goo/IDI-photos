"""Bounded structural validation of native SDK MP4 originals."""
from __future__ import annotations

import struct

from .domain import DomainError

MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_RECORD_SECONDS = 60


def inspect_mp4(data: bytes) -> dict:
    """Check the container and tracks without rewriting any source bytes.

    This validates format, duration and dimensions, not authenticity or every
    encoded frame. The Android client also decodes a preview before uploading.
    """
    if not data or len(data) >= MAX_VIDEO_BYTES:
        raise DomainError('每段视频须小于64 MiB，请保留手机原片', 413)
    view = memoryview(data)
    budget = [0]

    def boxes(start, end):
        result = []
        while start < end:
            budget[0] += 1
            if budget[0] > 10000 or end - start < 8:
                raise ValueError('invalid box')
            size, kind = struct.unpack_from('>I4s', view, start)
            header = 8
            if size == 1:
                if end - start < 16:
                    raise ValueError('invalid extended box')
                size, header = struct.unpack_from('>Q', view, start + 8)[0], 16
            elif size == 0:
                size = end - start
            if size < header or size > end - start:
                raise ValueError('truncated box')
            result.append((kind, start + header, start + size))
            start += size
        return result

    def one(items, kind):
        found = [item for item in items if item[0] == kind]
        if len(found) != 1:
            raise ValueError('missing or duplicate box')
        return found[0]

    def children(item):
        return boxes(item[1], item[2])

    try:
        top = boxes(0, len(view))
        _, start, end = one(top, b'ftyp')
        if end - start < 8:
            raise ValueError('invalid file type')
        brands = {bytes(view[start:start + 4])}
        brands.update(bytes(view[pos:pos + 4]) for pos in range(start + 8, end, 4))
        if not brands & {b'isom', b'iso2', b'mp41', b'mp42', b'avc1'}:
            raise ValueError('not MP4')
        if not any(kind == b'mdat' and end > start for kind, start, end in top):
            raise ValueError('no media data')
        movie = children(one(top, b'moov'))
        _, start, end = one(movie, b'mvhd')
        version = view[start]
        if version == 0 and end - start >= 20:
            scale, duration = struct.unpack_from('>II', view, start + 12)
        elif version == 1 and end - start >= 32:
            scale = struct.unpack_from('>I', view, start + 20)[0]
            duration = struct.unpack_from('>Q', view, start + 24)[0]
        else:
            raise ValueError('invalid movie duration')
        if not scale or duration <= 0 or duration / scale > MAX_RECORD_SECONDS + 5:
            raise ValueError('unsupported duration')
        videos, audio = [], False
        for track in (item for item in movie if item[0] == b'trak'):
            entries = children(track)
            media = children(one(entries, b'mdia'))
            _, start, end = one(media, b'hdlr')
            if end - start < 12:
                raise ValueError('invalid handler')
            kind = bytes(view[start + 8:start + 12])
            audio = audio or kind == b'soun'
            if kind != b'vide':
                continue
            _, start, end = one(entries, b'tkhd')
            if end - start < 84:
                raise ValueError('invalid video header')
            width, height = (v >> 16 for v in struct.unpack_from('>II', view, end - 8))
            if not 1 <= width <= 3840 or not 1 <= height <= 3840:
                raise ValueError('invalid dimensions')
            table = children(one(children(one(media, b'minf')), b'stbl'))
            _, start, end = one(table, b'stsd')
            if end - start < 16:
                raise ValueError('invalid sample description')
            samples = boxes(start + 8, end)
            if struct.unpack_from('>I', view, start + 4)[0] != len(samples):
                raise ValueError('invalid sample count')
            if not samples or any(item[0] not in (b'avc1', b'avc3') for item in samples):
                raise ValueError('unsupported video codec')
            videos.append({'width': width, 'height': height})
        if len(videos) != 1:
            raise ValueError('expected one video track')
        return {**videos[0], 'duration_ms': round(duration * 1000 / scale),
                'has_audio': audio, 'mime_type': 'video/mp4'}
    except (ValueError, IndexError, struct.error, OverflowError) as error:
        raise DomainError('视频不是完整的 H.264 MP4 或超出60秒录制范围，原片请保留在手机') from error
