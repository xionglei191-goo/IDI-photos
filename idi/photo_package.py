"""Photo and video delivery using the supplied insurer folder example."""
from collections import Counter
import hashlib
import io
import shutil
from pathlib import PurePosixPath

from PIL import Image, ImageOps

from .domain import DomainError
from .storage import IMAGE_EXTS, VIDEO_EXTS, safe_name

PHOTO_PACKAGE_FORMAT = 'media-by-stage-400kb-v1'
PHOTO_MAX_BYTES = 400_000
PHOTO_DIRECTORIES = (
    '查勘照片', '查勘照片/查勘楼栋房号打卡照片',
    '施工照片', '施工照片/施工楼栋房号打卡照片', '验收照片',
)
GROUP_DIRECTORIES = {
    'survey_checkin': PHOTO_DIRECTORIES[1], 'survey_problem': PHOTO_DIRECTORIES[0],
    'work_checkin': PHOTO_DIRECTORIES[3], 'construction': PHOTO_DIRECTORIES[2],
    'completion': PHOTO_DIRECTORIES[4],
}


def case_folder(case):
    code = case.get('platform_report_code')
    if not code:
        raise DomainError('案件尚无报修编号，请先补充报修编号后打包：' + case['title'])
    code = safe_name(str(code), 80)
    parts = (case['project_name'], case.get('building'),
             str(case['unit']) + '单元' if case.get('unit') else '',
             case.get('room') or case.get('location_detail'), case['title'])
    prefix = '-'.join(str(value).strip() for value in parts if value and str(value).strip())
    return safe_name(prefix, 109 - len(code)) + '-' + code


def photo_directory(photo):
    group = photo.get('photo_group')
    if group in GROUP_DIRECTORIES:
        return GROUP_DIRECTORIES[group]
    stage = photo.get('stage')
    if stage in ('survey', 'work'):
        checkin = photo.get('role') in ('building_checkin', 'door_checkin')
        return PHOTO_DIRECTORIES[(0 if stage == 'survey' else 2) + int(checkin)]
    if stage == 'acceptance':
        return PHOTO_DIRECTORIES[4]
    name = photo.get('original_name') or photo['id']
    raise DomainError('照片或视频尚未确定查勘、施工或验收分类，请先分类后打包：' + name)


def photo_paths(case, photos):
    root = case_folder(case)
    candidates = []
    for photo in photos:
        source = PurePosixPath((photo.get('original_name') or photo['id']).replace('\\', '/'))
        extensions = VIDEO_EXTS if photo['media_type'] == 'video' else IMAGE_EXTS
        extension = source.suffix if source.suffix.lower() in extensions else photo.get('extension', '')
        if extension.lower() not in extensions:
            raise DomainError('照片或视频文件类型无法确认：' + str(source))
        if photo['media_type'] == 'image' and photo['size_bytes'] > PHOTO_MAX_BYTES and extension.lower() not in ('.jpg', '.jpeg'):
            extension = '.jpg'
        stem = source.stem if source.suffix else source.name
        filename = safe_name(stem, 100 - len(extension)) + extension
        candidates.append((photo['id'], photo_directory(photo), filename, extension))
    counts = Counter((directory + '/' + filename).casefold() for _, directory, filename, _ in candidates)
    used, paths = set(), {}
    for photo_id, directory, filename, extension in sorted(candidates):
        if counts[(directory + '/' + filename).casefold()] > 1:
            filename = safe_name(filename[:-len(extension)], 85 - len(extension)) + '__' + photo_id[:12] + extension
        candidate = directory + '/' + filename
        number = 1
        while candidate.casefold() in used:
            number += 1
            candidate = directory + '/' + safe_name(filename[:-len(extension)], 90 - len(extension)) + f'__{number}' + extension
        used.add(candidate.casefold())
        paths[photo_id] = root + '/' + candidate
    return root, paths


def write_media(source, destination, photo, *, force_jpeg=False):
    """Compress only the exported image copy; preserve stored originals and videos."""
    compressed = photo['media_type'] == 'image' and (force_jpeg or source.stat().st_size > PHOTO_MAX_BYTES)
    width, height = photo.get('width'), photo.get('height')
    if compressed:
        with Image.open(source) as original:
            if getattr(original, 'n_frames', 1) > 1:
                raise DomainError('多帧图片请先拆分为照片或转为视频：' + photo['original_name'])
            oriented = ImageOps.exif_transpose(original)
            if oriented.mode in ('RGBA', 'LA') or 'transparency' in oriented.info:
                rgba = oriented.convert('RGBA')
                picture = Image.new('RGB', rgba.size, 'white')
                picture.paste(rgba, mask=rgba.getchannel('A'))
            else:
                picture = oriented.convert('RGB')
            picture.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
            while True:
                for quality in (88, 80, 72, 64, 56):
                    stream = io.BytesIO()
                    picture.save(stream, 'JPEG', quality=quality, optimize=True, progressive=True)
                    data = stream.getvalue()
                    if len(data) <= PHOTO_MAX_BYTES:
                        break
                if len(data) <= PHOTO_MAX_BYTES:
                    break
                picture = picture.resize((max(1, int(picture.width * .85)), max(1, int(picture.height * .85))), Image.Resampling.LANCZOS)
            width, height = picture.size
            destination.write_bytes(data)
    else:
        shutil.copyfile(source, destination)
    size = destination.stat().st_size
    if photo['media_type'] == 'image' and size > PHOTO_MAX_BYTES:
        raise DomainError('照片压缩未达到400 KB要求：' + photo['original_name'])
    with destination.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'sha256': digest, 'size_bytes': size, 'compressed': compressed, 'width': width, 'height': height}
