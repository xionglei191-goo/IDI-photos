import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from idi import video_watermark_time as video_time


def line(text, confidence=.99, x=20, y=900, width=350):
    return {'text': text, 'confidence': confidence,
            'box': [[x, y], [x + width, y], [x + width, y + 25], [x, y + 25]]}


def parse(*lines):
    evidence = [*lines, *anchors()]
    return video_time.parse_watermark_time('\n'.join(item['text'] for item in evidence), evidence)


def anchors():
    return [line('地点：广州市', y=1000), line('案件号：ZLBH202610221040480154', y=1040)]


@pytest.mark.parametrize('text', ['时间：2026-10-26 10:52:14', '时间：2026-10-2610:52:14',
                                  '拍摄时间: 2026-10-26 10:52:14', '时间：２０２６-１０-２６ １０：５２：１４',
                                  '时 间：2026-10-26 10:52:14', '间：2026-10-26 10:52:14'])
def test_complete_labelled_time_preserves_ocr_provenance(text):
    result = parse(line(text, .9585))
    assert result['state'] == 'recognized'
    assert result['watermark_time'] == '2026-10-26 10:52:14'
    assert result['time_basis'] == 'video_watermark_ocr' and result['method'] == 'first_frame'
    assert result['confidence'] == .9585
    assert result['evidence']['lines'][0]['text'] == text
    assert result['evidence']['lines'][0]['box'] == line(text)['box']


def test_split_visual_row_is_joined_without_borrowing_distant_time():
    result = parse(line('时间：', x=20, width=45), line('2026-10-26', x=70, width=140),
                   line('10:52:14', x=215, width=100))
    assert result['watermark_time'] == '2026-10-26 10:52:14'
    assert result['evidence']['matches'][0]['line_indexes'] == [0, 1, 2]
    assert parse(line('时间：', x=20, width=45), line('2026-10-26 10:52:14', y=100))['state'] == 'needs_review'
    assert parse(line('时间：', x=20, width=45), line('2026-10-26 10:52:14', x=600))['state'] == 'needs_review'


@pytest.mark.parametrize('context', [[], [line('地点：广州市', y=930)],
                                    [line('地点：广州市', y=930), line('地点：广州市', y=960)],
                                    [line('地点：广州市', x=600, y=930), line('小区：某小区', x=600, y=960)],
                                    [line('地点：广州市', y=1200), line('小区：某小区', y=1230)],
                                    [line('地点：广州市', y=930), line('小区：某小区', .89, y=960)]])
def test_scene_sign_or_unrelated_fields_are_not_treated_as_watermark(context):
    evidence = [line('时间：2026-10-26 10:52:14'), *context]
    result = video_time.parse_watermark_time('\n'.join(item['text'] for item in evidence), evidence)
    assert result['state'] == 'needs_review' and result['reason'] == 'watermark_context_missing'


def test_partial_time_label_is_supported_by_two_aligned_sdk_fields():
    evidence = [line('间：2026-10-2516:12:34', .9766, x=67, y=1050, width=222),
                line('地点：广州市·产业园', .9912, x=32, y=1078),
                line('案件号：ZLBH202610221041350155', .9945, x=34, y=1112)]
    result = video_time.parse_watermark_time('\n'.join(item['text'] for item in evidence), evidence)
    assert result['state'] == 'recognized' and result['watermark_time'] == '2026-10-25 16:12:34'
    assert result['evidence']['matches'][0]['anchor_indexes'] == [1, 2]


@pytest.mark.parametrize('text,reason', [
    ('2026-10-26 10:52:14', 'time_label_missing'),
    ('上传时间：2026-10-26 10:52:14', 'time_label_missing'),
    ('时间：2026-10-26 10:52', 'time_not_complete'),
    ('时间：2026-1O-26 10:52:14', 'time_not_complete'),
    ('时间：2026-02-30 10:52:14', 'invalid_calendar_time'),
    ('时间：2026-10-26 24:52:14', 'invalid_calendar_time'),
    ('时间：2026-10-26 10:52:60', 'invalid_calendar_time'),
])
def test_missing_incomplete_or_invalid_time_is_not_guessed(text, reason):
    result = parse(line(text))
    assert result['state'] == 'needs_review' and result['watermark_time'] is None
    assert result['reason'] == reason


def test_low_confidence_or_conflicting_labelled_times_need_review():
    assert parse(line('时间：2026-10-26 10:52:14', .8999))['reason'] == 'low_confidence'
    result = parse(line('时间：2026-10-26 10:52:14'), line('时间：2026-10-26 10:52:15', .5, y=950))
    assert result['reason'] == 'conflicting_times' and result['watermark_time'] is None
    result = parse(line('时间：2026-10-26 10:52:14 2026-10-26 10:52:15'))
    assert result['reason'] == 'conflicting_times'


@pytest.mark.parametrize('changes', [{'confidence': float('nan')}, {'confidence': float('inf')},
                                    {'confidence': 1.1}, {'box': []}, {'box': [[0, 0]] * 4},
                                    {'box': [[float('nan'), 1]] * 4}])
def test_evidence_requires_finite_confidence_and_real_geometry(changes):
    item = {**line('时间：2026-10-26 10:52:14'), **changes}
    assert parse(item)['state'] == 'needs_review'


def test_extraction_uses_temporary_first_frame_and_preserves_original(tmp_path):
    source = tmp_path / 'original.mp4'
    original = b'original bytes retained'
    source.write_bytes(original)
    temporary = []

    def decode(path, target):
        assert path == source
        Image.new('RGB', (720, 1280)).save(target)
        temporary.append(target)

    def ocr(path):
        assert path == temporary[0] and path.exists()
        evidence = [line('时间：2026-10-26 10:52:14'), *anchors()]
        return '\n'.join(item['text'] for item in evidence), evidence

    result = video_time.extract_video_watermark_time(source, ocr, decoder=decode)
    assert result['state'] == 'recognized'
    assert source.read_bytes() == original and not temporary[0].exists()


@pytest.mark.parametrize('failure,reason', [(video_time.DecodeError('decode_timeout'), 'decode_timeout'),
                                         (RuntimeError(), 'decode_failed')])
def test_decode_failures_do_not_call_ocr(tmp_path, failure, reason):
    source = tmp_path / 'original.mp4'
    source.write_bytes(b'video')

    def decode(*_):
        raise failure

    def ocr(_):
        pytest.fail('OCR must not run after decoder failure')

    assert video_time.extract_video_watermark_time(source, ocr, decoder=decode)['reason'] == reason


def test_decoder_subprocess_has_deadline_and_no_shell(monkeypatch, tmp_path):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])

    monkeypatch.setattr(video_time.subprocess, 'run', run)
    with pytest.raises(video_time.DecodeError, match='decode_timeout'):
        video_time._decode_first_frame(tmp_path / 'original.mp4', tmp_path / 'frame.png')
    assert calls[0][1]['timeout'] == 15 and 'shell' not in calls[0][1]
    assert calls[0][0][-3] == '--decode'


@pytest.mark.parametrize('path', ['https://example.invalid/movie.mp4', '//server/share/movie.mp4',
                                '\\\\server\\share\\movie.mp4', 'file:///tmp/movie.mp4'])
def test_only_local_files_are_accepted(path):
    result = video_time.extract_video_watermark_time(path, lambda _: pytest.fail('No OCR expected'))
    assert result['state'] == 'failed' and result['reason'] == 'invalid_video_path'


def test_ocr_failure_cleans_up_temporary_frame(tmp_path):
    source = tmp_path / 'original.mp4'
    source.write_bytes(b'video')
    frames = []

    def decode(_, path):
        frames.append(path)
        Image.new('RGB', (720, 1280)).save(path)

    def ocr(_):
        raise RuntimeError('local OCR unavailable')

    result = video_time.extract_video_watermark_time(source, ocr, decoder=decode)
    assert result['reason'] == 'ocr_failed' and not frames[0].exists()


def test_oversized_frame_is_rejected_before_ocr(tmp_path):
    source = tmp_path / 'original.mp4'
    source.write_bytes(b'video')

    def decode(_, path):
        Image.new('RGB', (4097, 1)).save(path)

    result = video_time.extract_video_watermark_time(
        source, lambda _: pytest.fail('No OCR expected'), decoder=decode)
    assert result['reason'] == 'frame_dimensions_exceeded'


@pytest.mark.parametrize('width,decode_ok,expected_reads', [(720, True, 1), (720, False, 1), (5000, True, 0)])
def test_native_decode_reads_only_first_frame_and_checks_dimensions(tmp_path, monkeypatch, width,
                                                                  decode_ok, expected_reads):
    source = tmp_path / 'original.mp4'
    source.write_bytes(b'video')
    target = tmp_path / 'frame.png'
    reads, released = [], []

    class Capture:
        def isOpened(self):
            return True

        def get(self, property_id):
            return width if property_id == 3 else 1280

        def read(self):
            reads.append(True)
            return decode_ok, SimpleNamespace(shape=(1280, 720, 3)) if decode_ok else None

        def release(self):
            released.append(True)

    def imwrite(path, frame, options):
        Image.new('RGB', (720, 1280)).save(path)
        return True

    cv2 = SimpleNamespace(setNumThreads=lambda _: None, VideoCapture=lambda _: Capture(),
                          CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
                          IMWRITE_PNG_COMPRESSION=16, imwrite=imwrite)
    monkeypatch.setitem(sys.modules, 'cv2', cv2)
    if decode_ok and width == 720:
        video_time._decode_cli(source, target)
        assert target.is_file()
    else:
        with pytest.raises(video_time.DecodeError):
            video_time._decode_cli(source, target)
        assert not target.exists()
    assert len(reads) == expected_reads and released == [True]
