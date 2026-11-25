import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
import fitz
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QListWidgetItem
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from app import MainWindow
from shortcuts import ShortcutStore
from core import crop_first, OcrEngine, OcrCandidate
from ocr_processing import OcrOptions, OcrOptionsStore, prepare_crop, estimate_skew, filter_leading_symbol


@pytest.mark.parametrize('text, expected', [
    ('*合同','合同'),('，合同','合同'),('￥100','100'),('-123','123'),
    ('##合同','#合同'),('A-123','A-123'),('123','123'),('合同#1','合同#1'),
    ('',''),('*',''),('★编号','编号'),('_ABC','ABC'),
])
def test_only_one_leading_symbol_is_filtered(text, expected):
    assert filter_leading_symbol(text,True) == expected
    assert filter_leading_symbol(text,False) == text


def test_old_ocr_settings_load_with_symbol_filter_off(tmp_path):
    import json
    path = tmp_path/'ocr.json'
    path.write_text(json.dumps(dict(model='compare',dpi=600,enhancement='contrast',deskew=False)),encoding='utf-8')
    options = OcrOptionsStore(path).load()
    assert options.model == 'compare' and options.dpi == 600
    assert not options.drop_leading_symbol


def test_large_page_renders_only_selection_at_requested_dpi(tmp_path):
    path = tmp_path/'large.pdf'
    with fitz.open() as doc:
        page = doc.new_page(width=4000,height=6000)
        page.draw_rect(fitz.Rect(2000,3000,2100,3100),color=None,fill=(1,0,0))
        doc.save(path)
    crop = crop_first(path,(.5,.5,.525,3100/6000),600)
    assert crop.shape[0] == pytest.approx(100*600/72,abs=2)
    assert crop.shape[1] == pytest.approx(100*600/72,abs=2)
    assert crop[:,:,0].mean() > 250 and crop[:,:,1].mean() < 3
    full = crop_first(path,(0,0,1,1),600)
    assert full.shape[0]*full.shape[1] <= 12_020_000


def test_dpi_controls_actual_selection_pixels(tmp_path):
    path = tmp_path/'resolution.pdf'
    with fitz.open() as doc:
        doc.new_page(width=600,height=800)
        doc.save(path)
    a = crop_first(path,(.1,.1,.3,.2),300)
    b = crop_first(path,(.1,.1,.3,.2),600)
    assert b.shape[0] == pytest.approx(a.shape[0]*2,abs=2)
    assert b.shape[1] == pytest.approx(a.shape[1]*2,abs=2)


def test_enhancement_and_original_preserve_text_area():
    image = np.full((200,800,3),240,np.uint8)
    cv2.putText(image,'HT20261125',(20,130),cv2.FONT_HERSHEY_SIMPLEX,2,(190,190,190),3)
    original, angle = prepare_crop(image,'original',False)
    assert np.array_equal(original[16:-16,16:-16],image)
    assert angle == 0
    contrast, _ = prepare_crop(image,'contrast',False)
    binary, _ = prepare_crop(image,'binary',False)
    assert contrast.shape == binary.shape == original.shape
    assert not np.array_equal(contrast,original)
    assert set(np.unique(binary)) == {0,255}


@pytest.mark.parametrize('angle', [-5,4])
def test_deskew_corrects_tilt_and_keeps_canvas(angle):
    image = np.full((360,1000,3),255,np.uint8)
    for y in (90,180,270):
        cv2.putText(image,'CONTRACT HT20261125',(80,y),cv2.FONT_HERSHEY_SIMPLEX,1.5,(0,0,0),3)
    matrix = cv2.getRotationMatrix2D((500,180),angle,1)
    tilted = cv2.warpAffine(image,matrix,(1000,360),borderValue=(255,255,255))
    corrected, found = prepare_crop(tilted,'original',True)
    assert found == pytest.approx(-angle,abs=.8)
    assert abs(estimate_skew(cv2.cvtColor(corrected,cv2.COLOR_RGB2GRAY))) < 1
    assert corrected.shape[0] > image.shape[0]


def test_options_persist_separately_from_shortcuts(tmp_path):
    shortcut_store = ShortcutStore(tmp_path/'settings.json')
    from shortcuts import DEFAULTS
    shortcut_store.save(DEFAULTS)
    before = shortcut_store.path.read_bytes()
    store = OcrOptionsStore(tmp_path/'ocr.json')
    options = OcrOptions('compare',600,'contrast',False)
    store.save(options)
    assert OcrOptionsStore(store.path).load() == options
    assert shortcut_store.path.read_bytes() == before


def test_model_failure_does_not_discard_other_model(monkeypatch):
    monkeypatch.setattr('core.crop_first',lambda *args: np.full((100,300,3),255,np.uint8))
    engine = OcrEngine()
    def fail(*args):
        raise RuntimeError('model failed')
    monkeypatch.setattr(engine,'_new',fail)
    monkeypatch.setattr(engine,'_standard',lambda *args: ('合同编号123',.99))
    candidates = engine.recognize_candidates(None,None,OcrOptions(model='compare'))
    assert candidates[0].error and candidates[1].text == '合同编号123'


def test_real_models_offline_chinese_and_no_spaces(monkeypatch):
    # Any accidental font/model download is a test failure, even with network available.
    import requests
    monkeypatch.setattr(requests.Session,'request',lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden')))
    path = Path(__file__).resolve().parents[2]/'test_documents'/'01-中文扫描件.pdf'
    candidates = OcrEngine().recognize_candidates(path,(.08,.15,.92,.35),OcrOptions(model='compare'))
    assert {c.model for c in candidates} == {'v6','v4'}
    for candidate in candidates:
        assert not candidate.error
        assert '采购合同' in candidate.text and 'HT20261125' in candidate.text
        assert not any(c.isspace() for c in candidate.text)


def test_candidates_switch_and_option_change_invalidates_stale_results(tmp_path):
    app = QApplication.instance() or QApplication([])
    w = MainWindow(ShortcutStore(tmp_path/'settings.json'))
    pending = []
    w.submit = lambda fn,done,failed: pending.append((fn,done,failed))
    item = QListWidgetItem('a.pdf')
    item.setData(Qt.ItemDataRole.UserRole,tmp_path/'a.pdf')
    w.files.addItem(item)
    w.files.setCurrentRow(0)
    w.on_region((0,0,.5,.5))
    done = pending[-1][1]
    candidates = [OcrCandidate('v6','候选一',.99),OcrCandidate('v4','候选二',.98)]
    done(candidates)
    assert w.name_edit.text() == '候选一'
    w.candidate_combo.setCurrentIndex(1)
    assert w.name_edit.text() == '候选二'
    w.dpi_combo.setCurrentIndex(w.dpi_combo.findData(600))
    assert not w.name_edit.text() and not w.rename_button.isEnabled()
    assert w.candidate_combo.count() == 0
    done(candidates)
    assert not w.name_edit.text()
    assert OcrOptionsStore(tmp_path/'ocr.json').load().dpi == 600
    w.close()


def test_combobox_arrow_changes_option_without_navigating_files(tmp_path):
    app = QApplication.instance() or QApplication([])
    w = MainWindow(ShortcutStore(tmp_path/'settings.json'))
    w.submit = lambda *args: None
    for name in ('a.pdf','b.pdf'):
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole,tmp_path/name)
        w.files.addItem(item)
    w.files.setCurrentRow(0)
    w.show()
    w.activateWindow()
    w.model_combo.setFocus()
    app.processEvents()
    QTest.keyClick(w.model_combo,Qt.Key.Key_Down)
    assert w.current.name == 'a.pdf'
    assert w.model_combo.currentData() == 'v4'
    w.close()
