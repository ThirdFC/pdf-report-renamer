import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fitz
import pytest
from core import *


@pytest.mark.parametrize('text, expected', [
    ('  合同编号：A/2026:*?  ', '合同编号：A 2026'),
    ('A\nB\tC', 'A B C'), ('报告.pdf', '报告'),
    ('CON', '_CON'), ('aux.txt', '_aux.txt'), ('LPT1', '_LPT1'),
    ('...', ''), (' A. ', 'A'), ('NUL.pdf', '_NUL'),
    ('测试中文 ABC 0123', '测试中文 ABC 0123'),
])
def test_names(text, expected):
    assert clean_name(text) == expected


def test_utf16_length():
    assert len(clean_name('😀'*300).encode('utf-16-le')) <= 360


def test_scan(tmp_path):
    (tmp_path/'a.PDF').touch()
    (tmp_path/'b.txt').touch()
    sub = tmp_path/'子目录'
    sub.mkdir()
    (sub/'c.pdf').touch()
    assert len(scan_pdfs(tmp_path, True)[0]) == 2
    assert len(scan_pdfs(tmp_path, False)[0]) == 1


def test_rename_undo_and_bytes(tmp_path):
    p = tmp_path/'原始.pdf'
    p.write_bytes(b'unchanged PDF data')
    record = rename_pdf(p, '识别结果.pdf')
    assert record.renamed.name == '识别结果.pdf'
    assert record.renamed.read_bytes() == b'unchanged PDF data'
    assert not p.exists()
    assert undo_rename(record) == p
    assert p.read_bytes() == b'unchanged PDF data'


def test_existing_never_overwritten(tmp_path):
    a, b = tmp_path/'a.pdf', tmp_path/'b.pdf'
    a.write_bytes(b'a')
    b.write_bytes(b'b')
    with pytest.raises(FileExistsError):
        rename_pdf(a, 'b')
    assert a.read_bytes() == b'a' and b.read_bytes() == b'b'


def test_undo_conflict_and_modified(tmp_path):
    p = tmp_path/'a.pdf'
    p.write_bytes(b'a')
    r = rename_pdf(p, 'b')
    p.write_bytes(b'new file')
    with pytest.raises(FileExistsError):
        undo_rename(r)
    p.unlink()
    r.renamed.write_bytes(b'modified')
    with pytest.raises(ValueError):
        undo_rename(r)


def test_unchanged_empty(tmp_path):
    p = tmp_path/'a.pdf'
    p.touch()
    with pytest.raises(ValueError):
        rename_pdf(p, 'a')
    with pytest.raises(ValueError):
        rename_pdf(p, ' : / ')


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
def test_rotated_crop_matches_display(tmp_path, rotation):
    p = tmp_path/'rotation.pdf'
    d = fitz.open()
    page = d.new_page(width=400, height=200)
    page.draw_rect(fitz.Rect(0, 0, 200, 100), color=None, fill=(1,0,0))
    page.set_rotation(rotation)
    d.save(p)
    d.close()
    low, _ = render_first(p)
    a = np.frombuffer(low.samples, np.uint8).reshape(low.height, low.width, 3)
    crop = crop_first(p, (0,0,.5,.5))
    assert np.abs(a[:a.shape[0]//2, :a.shape[1]//2].mean((0,1)) - crop.mean((0,1))).max() < 3


def test_encrypted_and_corrupt(tmp_path):
    p = tmp_path/'locked.pdf'
    d = fitz.open()
    d.new_page()
    d.save(p, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw='secret', owner_pw='owner')
    d.close()
    with pytest.raises(ValueError, match='密码'):
        render_first(p)
    p = tmp_path/'bad.pdf'
    p.write_bytes(b'not a PDF')
    with pytest.raises(Exception):
        render_first(p)


def test_real_chinese_scanned_ocr():
    folder = Path(__file__).resolve().parents[2] / 'test_documents'
    text, confidence = OcrEngine().recognize(folder/'01-中文扫描件.pdf', (.08,.15,.92,.35))
    assert '采购合同' in text
    assert 'HT20261125' in text.replace(' ', '')
    assert confidence > .7
    assert not any(c.isspace() for c in text)


def test_ocr_removes_spaces_across_all_lines(monkeypatch):
    monkeypatch.setattr('core.crop_first', lambda *args: np.zeros((10,10,3), dtype=np.uint8))
    engine = OcrEngine()
    engine.engine = lambda *args: ([[[], ' 合 同\u3000编号 ', .99], [[], 'AB\t12\u00a034\n', .98]], 0)
    text, _ = engine.recognize(None, None)
    assert text == '合同编号AB1234'
