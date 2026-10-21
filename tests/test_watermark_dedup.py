import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image, ImageDraw

from idi.db import dump, now, uid
from idi.photo_dedup import normalize_watermark_code, same_picture, watermark_from_ocr
from test_official_camera import official, upload
from test_feishu_capture import connector


CODE = 'Y62GPC2CEUR6X4'


def image_pair():
    image = Image.new('RGB', (480, 640), '#bbb9ad')
    draw = ImageDraw.Draw(image)
    for y in range(40, 500, 70):
        for x in range(35, 400, 75):
            draw.rectangle((x, y, x + 38, y + 49), fill=(x % 255, y % 255, 95))
    draw.line((0, 450, 480, 200), fill='#374c69', width=10)
    draw.rectangle((10, 530, 470, 620), fill='white')
    draw.text((22, 550), CODE, fill='black')
    full, small = io.BytesIO(), io.BytesIO()
    image.save(full, 'JPEG', quality=96)
    image.resize((240, 320)).save(small, 'JPEG', quality=45)
    return full.getvalue(), small.getvalue()


@pytest.fixture
def setup(services, official, connector, monkeypatch):
    full, compressed = image_pair()
    official['image'] = full
    official['meta'].update(sdk_anti_fake_code=CODE, sdk_photo_time='2026:10:20 08:00:00')
    official['service'].settings.save({'auto_verify': False})
    calls = []
    evidence = {'confidence': 0.999, 'code': CODE, 'case_number': official['meta']['case_number']}

    def ocr(path):
        calls.append(str(path))
        lines = ['案件编号：' + evidence['case_number'], '时间：2026-10-20 08:00',
                 '打卡内容：维修', '施工内容：修补墙面', '防伪码：' + evidence['code']]
        return '\n'.join(lines), [{'text': line, 'confidence': evidence['confidence'],
                                  'box': [[0, i * 20], [200, i * 20], [200, i * 20 + 19], [0, i * 20 + 19]]}
                                 for i, line in enumerate(lines)]
    monkeypatch.setattr(services['recognizer'], 'local_ocr', ocr)

    def incoming(data=compressed, mid=None):
        mid = mid or uid()
        folder = services['settings'].data_dir / 'inbox' / connector['id']
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (mid + '.jpg')
        path.write_bytes(data)
        event = {'source_key': mid + ':image', 'chat_id': connector['config']['chat_id'],
                 'group_name': '照片收集群', 'sender': 'worker', 'message_type': 'image',
                 'file_path': str(path), 'original_name': '群照片.jpg', 'feishu_message_id': mid}
        return services['connectors'].ingest(connector['id'], event), path, event

    return {'incoming': incoming, 'full': full, 'compressed': compressed,
            'calls': calls, 'evidence': evidence}


def test_phone_first_discards_chat_file_and_retains_source(client, services, official, setup):
    receipt = upload(client, official).json()
    result, path, event = setup['incoming']()
    assert result['media_state'] == 'saved' and result['duplicate']
    assert result['photo_id'] == receipt['photo_id']
    assert not path.exists()
    assert services['db'].one('SELECT COUNT(*) n FROM blobs')['n'] == 1
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 1
    photo = services['domain'].photo(receipt['photo_id'])
    assert photo['source_type'] == 'xhey_camera' and photo['source_count'] == 2
    assert photo['watermark_code'] == CODE
    assert client.get(photo['original_url']).content == setup['full']
    assert not services['db'].all('SELECT * FROM photo_file_versions')
    assert services['connectors'].ingest(event=event, connector_id=services['db'].one('SELECT id FROM connectors')['id'])['duplicate']
    assert len(setup['calls']) == 1


def test_chat_first_promotes_in_place_and_preserves_report_and_annotations(client, services, official, setup, case):
    imported, _, _ = setup['incoming']()
    photo_id = imported['photo_id']
    services['recognizer'].recognize(photo_id)
    assert len(setup['calls']) == 1  # preflight OCR is reused
    photo = services['domain'].photo(photo_id)
    photo = services['domain'].update_photo(photo_id, {
        'revision': photo['revision'], 'case_id': case['id'], 'photo_group': 'construction',
        'part': '人工指定部位', 'process': '人工核对内容', 'notes': '保留人工备注', 'confirm': True})
    package = services['reporter'].build(uid(), case['id'], [photo_id])
    exported = client.get(package['download_url']).content
    with services['db'].tx() as con:
        con.execute("""INSERT INTO platform_drafts(case_id,stage,files_json,updated_at)
            VALUES(?,'repair',?,?)""", (case['id'], dump({'images': [photo_id]}), now()))
        con.execute("INSERT INTO platform_manual_file_selections VALUES(?,'repair','images',?,?,?,'remote',?)",
                    (case['id'], photo_id, photo['revision'], photo['blob_hash'], now()))
        con.execute("""INSERT INTO platform_review_batches(id,case_id,stage,state,fingerprint,snapshot,created_at)
            VALUES('review',?,'repair','reviewed','fingerprint','{}',?)""", (case['id'], now()))
    result = upload(client, official)
    assert result.status_code == 200, result.text
    assert result.json()['photo_id'] == photo_id
    updated = services['domain'].photo(photo_id)
    for field in ('case_id', 'photo_group', 'part', 'process', 'notes', 'confirmed_fields', 'recognition_state'):
        assert updated[field] == photo[field]
    assert updated['blob_hash'] == hashlib.sha256(setup['full']).hexdigest()
    assert updated['revision'] == photo['revision'] + 1
    assert updated['thumbnail_url'] != photo['thumbnail_url']
    assert updated['original_url'] != photo['original_url']
    assert updated['source_count'] == 2 and updated['source_type'] == 'xhey_camera'
    assert services['db'].one('SELECT state FROM platform_review_batches')['state'] == 'stale'
    assert json.loads(services['db'].one('SELECT files_json FROM platform_drafts')['files_json']) == {'images': [photo_id]}
    selection = services['db'].one('SELECT * FROM platform_manual_file_selections')
    assert selection['blob_hash'] == updated['blob_hash'] and selection['photo_revision'] == updated['revision']
    assert services['db'].one('SELECT state FROM recognitions')['state'] == 'superseded'
    assert client.get(package['download_url']).content == exported
    version = services['db'].one('SELECT * FROM photo_file_versions')
    old_blob = services['db'].one('SELECT * FROM blobs WHERE hash=?', (version['blob_hash'],))
    assert services['storage'].resolve(old_blob['stored_path']).read_bytes() == setup['compressed']
    assert upload(client, official).json()['photo_id'] == photo_id
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 1
    assert client.get(updated['original_url']).content == setup['full']


def test_phone_arrives_before_queued_chat_ocr(client, services, official, setup):
    imported, _, _ = setup['incoming']()
    assert not services['db'].all('SELECT * FROM recognitions')
    result = upload(client, official)
    assert result.status_code == 200, result.text
    assert result.json()['photo_id'] == imported['photo_id']
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 1
    assert services['domain'].photo(imported['photo_id'])['case_id'] == official['meta']['case_id']


@pytest.mark.parametrize('conflict', ['confidence', 'content', 'case', 'code', 'ambiguous'])
def test_uncertain_matches_never_replace_or_drop_images(client, services, official, setup, conflict):
    primary = upload(client, official).json()['photo_id']
    data = setup['compressed']
    if conflict == 'confidence':
        setup['evidence']['confidence'] = 0.65
    elif conflict == 'content':
        output = io.BytesIO()
        Image.new('RGB', (240, 320), 'red').save(output, 'JPEG')
        data = output.getvalue()
    elif conflict == 'case':
        setup['evidence']['case_number'] = 'ZLBH202610209999999999'
    elif conflict == 'code':
        setup['evidence']['code'] = 'Y62GPC2CEUR6X5'
    else:
        setup['evidence']['code'] = CODE + '\n防伪码：Y62GPC2CEUR6X5'
    result, path, _ = setup['incoming'](data)
    assert result['media_state'] == 'saved' and not result['duplicate']
    assert result['photo_id'] != primary and path.exists()
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 2
    assert client.get('/api/photos/' + primary + '/original').content == setup['full']
    if conflict != 'code':
        photo = services['domain'].photo(result['photo_id'])
        assert photo['watermark_dedup_reason'] and photo['recognition_state'] == 'conflict'
        services['recognizer'].recognize(photo['id'])
        assert services['domain'].photo(photo['id'])['recognition_state'] == 'conflict'
        assert services['photo_workflow'].process(photo['id'])['state'] == 'needs_check'


def test_ocr_unavailable_preserves_attachment(client, services, official, setup, monkeypatch):
    upload(client, official)
    monkeypatch.setattr(services['recognizer'], 'local_ocr', lambda _: (_ for _ in ()).throw(RuntimeError('unavailable')))
    result, path, _ = setup['incoming']()
    assert result['media_state'] == 'saved' and not result['duplicate'] and path.exists()
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 2


def test_chat_case_conflict_blocks_later_phone_promotion(client, services, official, setup):
    setup['evidence']['case_number'] = 'ZLBH202610209999999999'
    imported, _, _ = setup['incoming']()
    before = services['domain'].photo(imported['photo_id'])
    assert before['watermark_dedup_reason'] and before['recognition_state'] == 'conflict'
    response = upload(client, official)
    assert response.status_code == 200, response.text
    assert response.json()['photo_id'] != imported['photo_id']
    assert services['domain'].photo(imported['photo_id'])['blob_hash'] == before['blob_hash']
    phone = services['domain'].photo(response.json()['photo_id'])
    assert phone['recognition_state'] == 'conflict' and phone['watermark_dedup_reason']
    assert not services['db'].all('SELECT * FROM photo_file_versions')


def test_small_watermark_retry_reuses_improved_ocr(services, setup, monkeypatch):
    code_line = '防伪码：' + CODE
    full = ('案件编号：ZLBH202610200000000001', [
        {'text': '案件编号：ZLBH202610200000000001', 'confidence': .999,
         'box': [[0, 20], [200, 20], [200, 40], [0, 40]]}])
    crop = (code_line, [{'text': code_line, 'confidence': .999,
                        'box': [[0, 10], [200, 10], [200, 30], [0, 30]]}])
    calls = []
    def ocr(path):
        calls.append(path)
        return crop if path.name == 'footer.png' else full
    monkeypatch.setattr(services['recognizer'], 'local_ocr', ocr)
    imported, _, _ = setup['incoming']()
    assert services['domain'].photo(imported['photo_id'])['watermark_code'] == CODE
    services['recognizer'].recognize(imported['photo_id'])
    assert len(calls) == 2
    assert not calls[-1].exists()


def test_forced_ocr_bypasses_cache_and_manual_confirmation_clears_review_reason(services, setup, case):
    setup['evidence']['confidence'] = .65
    imported, _, _ = setup['incoming']()
    photo = services['domain'].photo(imported['photo_id'])
    assert photo['watermark_dedup_reason']
    confirmed = services['domain'].update_photo(photo['id'], {
        'case_id': case['id'], 'revision': photo['revision'], 'confirm': True})
    assert confirmed['watermark_dedup_reason'] == ''
    before = len(setup['calls'])
    services['recognizer'].recognize(photo['id'])
    assert len(setup['calls']) == before
    services['recognizer'].recognize(photo['id'], force=True)
    assert len(setup['calls']) == before + 1
    assert services['domain'].photo(photo['id'])['watermark_dedup_reason'] == ''


def test_parallel_upload_orders_converge_on_one_record(client, services, official, setup):
    with ThreadPoolExecutor(max_workers=2) as executor:
        phone = executor.submit(upload, client, official)
        chat = executor.submit(setup['incoming'])
        receipt, (imported, _, _) = phone.result(), chat.result()
    assert receipt.status_code == 200, receipt.text
    assert imported['media_state'] == 'saved'
    assert receipt.json()['photo_id'] == imported['photo_id']
    assert services['db'].one('SELECT COUNT(*) n FROM photos')['n'] == 1
    photo = services['domain'].photo(imported['photo_id'])
    assert photo['blob_hash'] == hashlib.sha256(setup['full']).hexdigest()


def test_backfill_indexes_saved_sdk_and_ocr_without_processing(client, services, official, setup):
    phone_id = upload(client, official).json()['photo_id']
    with services['db'].tx() as con:
        con.execute("UPDATE photos SET watermark_code='',watermark_code_source='' WHERE id=?", (phone_id,))
    services['photo_dedup'].backfill()
    assert services['domain'].photo(phone_id)['watermark_code'] == CODE
    assert not setup['calls']
    imported, _, _ = setup['incoming']()
    assert imported['duplicate'] and imported['photo_id'] == phone_id


def test_old_ocr_cannot_overwrite_promoted_photo(client, services, official, setup, monkeypatch):
    imported, _, _ = setup['incoming']()
    with services['db'].tx() as con:
        con.execute('DELETE FROM photo_ocr_cache')
    original_ocr = services['recognizer'].local_ocr

    def promote_while_recognizing(path):
        result = original_ocr(path)
        assert upload(client, official).status_code == 200
        return result
    monkeypatch.setattr(services['recognizer'], 'local_ocr', promote_while_recognizing)
    with pytest.raises(Exception, match='原图已更新'):
        services['recognizer'].recognize(imported['photo_id'])
    photo = services['domain'].photo(imported['photo_id'])
    assert photo['recognition_state'] != 'failed'
    assert photo['blob_hash'] == hashlib.sha256(setup['full']).hexdigest()
    assert not services['db'].all('SELECT * FROM recognitions')


def test_code_parsing_is_exact_and_image_check_accepts_only_recompression():
    assert normalize_watermark_code(' y62gpc2ceur6x4 ') == CODE
    assert normalize_watermark_code('Y62GPC2CEUR6X') == ''
    assert normalize_watermark_code('Y62GPC2CEUR6XO') != normalize_watermark_code('Y62GPC2CEUR6X0')
    assert watermark_from_ocr('案件编号：ZLBH202610200000000001')['code'] == ''
    assert watermark_from_ocr('水印码：' + CODE + '\n防伪码：Y62GPC2CEUR6X5')['code'] == ''
    full, compressed = image_pair()
    assert full != compressed and same_picture(full, compressed)
