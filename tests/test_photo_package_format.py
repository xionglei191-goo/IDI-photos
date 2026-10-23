import hashlib
import io
import json
import zipfile
import random
from pathlib import PurePosixPath
from urllib.parse import unquote

import pytest
from openpyxl import load_workbook
from PIL import Image

from idi.db import dump, uid
from idi.photo_package import PHOTO_DIRECTORIES, PHOTO_PACKAGE_FORMAT, PHOTO_MAX_BYTES, photo_directory


REPORT_CODE = 'ZLBH202610132005010577'


def number_case(services, case):
    return services['domain'].save_case({'platform_report_code': REPORT_CODE, 'report_number': 'INSURER-OTHER-NUMBER'}, case['id'])


def test_example_directories_images_videos_and_original_hashes(client, services, case, photo):
    case = number_case(services, case)
    expected = [
        ('red', '查勘问题.png', 'survey', 'problem', '查勘照片'),
        ('green', '查勘楼栋.png', 'survey', 'building_checkin', '查勘照片/查勘楼栋房号打卡照片'),
        ('blue', '查勘房号.png', 'survey', 'door_checkin', '查勘照片/查勘楼栋房号打卡照片'),
        ('yellow', '施工过程.png', 'work', 'construction', '施工照片'),
        ('purple', '施工楼栋.png', 'work', 'building_checkin', '施工照片/施工楼栋房号打卡照片'),
        ('orange', '施工房号.png', 'work', 'door_checkin', '施工照片/施工楼栋房号打卡照片'),
        ('white', '验收照片.png', 'acceptance', 'finished', '验收照片'),
    ]
    photos = [photo(color, name=name, stage=stage, role=role) for color, name, stage, role, _ in expected]
    pdf = services['importer'].add_file(b'%PDF-1.4\nfixture\n%%EOF', '业主验收.pdf', case_id=case['id'])['photo_id']
    video = services['importer'].add_file(b'isolated-video-fixture' * 30000, '施工视频.mp4', case_id=case['id'], metadata={'stage': 'work'})['photo_id']
    visit = services['domain'].save_visit(case['id'], {'stage': 'work', 'visit_date': '2026-10-23', 'notes': '无照片进场仍需核对'})
    ids = [p['id'] for p in photos] + [pdf, video]
    preview = services['reporter'].preview(ids, 'insurer-v2')
    root = f"从化铂悦府-1-201-{case['title']}-{REPORT_CODE}"
    assert preview['photo_count'] == 7 and preview['video_count'] == 1 and preview['excluded_file_count'] == 1
    assert preview['cases'][0]['root_directory'] == root
    assert any(row['scope_key'] == 'visit:' + visit['id'] and row['missing'] for row in preview['cases'][0]['completeness']['items'])
    package = services['reporter'].build(uid(), case['id'], ids, 'insurer-v2')
    manifest = package['manifest']
    assert manifest['format'] == PHOTO_PACKAGE_FORMAT and len(manifest['photos']) == 8
    assert any(v['id'] == visit['id'] for v in manifest['visits'])
    response = client.get(package['download_url'])
    assert unquote(response.headers['content-disposition']).endswith(root + '-v1.zip')
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {e.filename for e in archive.infolist() if e.is_dir()} == {root + '/', *(root + '/' + d + '/' for d in PHOTO_DIRECTORIES)}
        assert {e.filename for e in archive.infolist() if not e.is_dir()} == {root + '/' + row[4] + '/' + row[1] for row in expected} | {root + '/施工照片/施工视频.mp4'}
        for p in manifest['photos']:
            assert hashlib.sha256(archive.read(p['package_path'])).hexdigest() == p['blob_hash']
        assert case['short_code'] not in '\n'.join(archive.namelist())
        assert case['report_number'] not in '\n'.join(archive.namelist())
    assert (services['storage'].resolve(package['zip_path']).parent / 'snapshot.json').is_file()
    assert services['domain'].photos(case_id=case['id'])['total'] == 9


def test_missing_report_code_never_falls_back_to_other_numbers(client, services, case, photo):
    services['domain'].save_case({'report_number': 'INSURER-123'}, case['id'])
    assert case['short_code']
    p = photo(stage='survey', role='problem')
    response = client.post('/api/packages', json={'photo_ids': [p['id']]})
    assert response.status_code == 400 and '报修编号' in response.json()['detail']
    assert services['reporter'].packages() == []
    assert services['db'].all("SELECT * FROM jobs WHERE kind='export'") == []


def test_unknown_classification_and_documents_only_are_rejected(client, services, case, photo):
    number_case(services, case)
    p = photo()
    response = client.post('/api/packages', json={'photo_ids': [p['id']]})
    assert response.status_code == 400 and '先分类' in response.json()['detail']
    pdf = services['importer'].add_file(b'%PDF-1.4\nfixture', '验收.pdf', case_id=case['id'])
    response = client.post('/api/packages', json={'photo_ids': [pdf['photo_id']]})
    assert response.status_code == 400 and '没有照片' in response.json()['detail']
    assert services['reporter'].packages() == []


def test_safe_names_collisions_and_empty_example_folders(services, case, photo):
    number_case(services, case)
    photos = [photo(color, name=name, stage='survey') for color, name in
              [('red', '重复.PNG'), ('green', '重复.png'), ('blue', 'CON.png'), ('white', '普通.png')]]
    with services['db'].tx() as con:
        con.execute("UPDATE photo_sources SET original_name='../../越界.png' WHERE photo_id=?", (photos[-1]['id'],))
    package = services['reporter'].build(uid(), case['id'], [p['id'] for p in photos])
    root = package['manifest']['root_directory']
    with zipfile.ZipFile(services['storage'].resolve(package['zip_path'])) as archive:
        files = [e.filename for e in archive.infolist() if not e.is_dir()]
        assert len(files) == len(set(name.casefold() for name in files)) == 4
        assert all(name.startswith(root + '/查勘照片/') and '..' not in PurePosixPath(name).parts for name in files)
        assert root + '/施工照片/施工楼栋房号打卡照片/' in archive.namelist()
        assert root + '/验收照片/' in archive.namelist()
        for p in package['manifest']['photos']:
            assert hashlib.sha256(archive.read(p['package_path'])).hexdigest() == p['blob_hash']


def test_new_format_creates_one_new_version_without_rewriting_historical_zip(client, services, case, photo):
    number_case(services, case)
    p = photo(stage='survey', role='problem', process='=HYPERLINK("unsafe")')
    reporter = services['reporter']
    old_id = uid()
    legacy = reporter._snapshot(old_id, case['id'], [p['id']], 'general', None, '')
    legacy.pop('format')
    legacy.pop('root_directory')
    legacy['photos'][0]['package_path'] = '资料/查勘/001-照片.png'
    with services['db'].tx() as con:
        con.execute('UPDATE packages SET manifest=? WHERE id=?', (dump(legacy), old_id))
    old = reporter.build(old_id, case['id'], [p['id']])
    before = client.get(old['download_url']).content
    with zipfile.ZipFile(io.BytesIO(before)) as archive:
        assert 'snapshot.json' in archive.namelist()
        book = load_workbook(io.BytesIO(archive.read('照片资料索引.xlsx')), data_only=False)
        assert book['照片索引']['I2'].data_type == 's'
    new = reporter.build(uid(), case['id'], [p['id']])
    assert new['version'] == 2 and new['manifest']['format'] == PHOTO_PACKAGE_FORMAT
    repeat = reporter.build(uid(), case['id'], [p['id']])
    assert repeat['id'] == new['id'] and repeat['reused']
    assert client.get(old['download_url']).content == before
    assert reporter.package(old_id)['manifest'] == legacy
    assert len(reporter.packages()) == 2


@pytest.mark.parametrize('extension', ['mp4', 'mov', 'avi', 'm4v', 'webm', 'mkv'])
def test_video_only_exports_keep_bytes_and_stage(services, case, extension):
    number_case(services, case)
    content = b'isolated-video-test-' * 30000
    item = services['importer'].add_file(content, '验收视频.' + extension, case_id=case['id'], metadata={'stage': 'acceptance'})
    preview = services['reporter'].preview([item['photo_id']])
    assert preview['photo_count'] == 0 and preview['video_count'] == 1
    package = services['reporter'].build(uid(), case['id'], [item['photo_id']])
    p = package['manifest']['photos'][0]
    assert '/验收照片/验收视频.' + extension in p['package_path']
    with zipfile.ZipFile(services['storage'].resolve(package['zip_path'])) as archive:
        assert archive.read(p['package_path']) == content
    assert p['export']['size_bytes'] > PHOTO_MAX_BYTES and not p['export']['compressed']


@pytest.mark.parametrize('kind,extension', [('PNG', 'png'), ('JPEG', 'jpeg')])
def test_large_photos_compress_below_400kb_without_changing_originals(services, case, photo, kind, extension):
    number_case(services, case)
    picture = Image.frombytes('RGB', (1024, 768), random.Random(42).randbytes(1024 * 768 * 3))
    exif = Image.Exif()
    exif[274] = 6
    stream = io.BytesIO()
    picture.save(stream, kind, quality=99, exif=exif)
    original = stream.getvalue()
    assert len(original) > PHOTO_MAX_BYTES
    imported = services['importer'].add_file(original, '大图.' + extension, case_id=case['id'], metadata={'stage': 'survey'})
    small = photo(stage='survey')
    ids = [imported['photo_id'], small['id']]
    reporter = services['reporter']
    package = reporter.build(uid(), case['id'], ids)
    p = next(p for p in package['manifest']['photos'] if p['id'] == imported['photo_id'])
    with zipfile.ZipFile(services['storage'].resolve(package['zip_path'])) as archive:
        data = archive.read(p['package_path'])
        assert len(data) <= PHOTO_MAX_BYTES
        assert p['export']['sha256'] == hashlib.sha256(data).hexdigest()
        assert p['export']['size_bytes'] == len(data) and p['export']['compressed']
        with Image.open(io.BytesIO(data)) as result:
            assert result.format == 'JPEG' and result.height > result.width
            result.verify()
        for item in package['manifest']['photos']:
            assert len(archive.read(item['package_path'])) <= PHOTO_MAX_BYTES
    blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (p['blob_hash'],))
    assert services['storage'].resolve(blob['stored_path']).read_bytes() == original
    before = services['storage'].resolve(package['zip_path']).read_bytes()
    repeat = reporter.build(uid(), case['id'], ids)
    assert repeat['id'] == package['id'] and repeat['reused']
    assert services['storage'].resolve(package['zip_path']).read_bytes() == before
    assert p['package_path'].endswith('.jpg' if kind == 'PNG' else '.jpeg')
    assert next(p for p in package['manifest']['photos'] if p['id'] == small['id'])['export']['compressed'] is False


@pytest.mark.parametrize('stage,role,directory', [
    ('survey', 'unknown', '查勘照片'), ('work', 'unknown', '施工照片'),
    ('survey', 'door_checkin', '查勘照片/查勘楼栋房号打卡照片'),
    ('work', 'building_checkin', '施工照片/施工楼栋房号打卡照片'),
    ('acceptance', 'unknown', '验收照片'),
])
def test_historical_stage_classifications_remain_usable(stage, role, directory):
    assert photo_directory({'stage': stage, 'role': role, 'id': 'photo'}) == directory
