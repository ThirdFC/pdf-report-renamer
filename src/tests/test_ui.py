import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from PySide6.QtWidgets import QApplication, QListWidgetItem
from PySide6.QtCore import Qt, QMimeData, QUrl, QPoint, QPointF, QBuffer, QIODevice
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage
from PySide6.QtTest import QTest
from app import MainWindow
from shortcuts import ShortcutStore
from core import render_first, OcrCandidate


@pytest.fixture(scope='module')
def qt():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt, tmp_path):
    w = MainWindow(ShortcutStore(tmp_path/'settings.json'))
    w.pending = []
    w.submit = lambda fn, done, failed: w.pending.append((fn, done, failed))
    w.show()
    qt.processEvents()
    yield w
    w.close()
    w.deleteLater()
    qt.processEvents()


def add_file(w, path):
    i = QListWidgetItem(path.name)
    i.setData(Qt.ItemDataRole.UserRole, path)
    w.files.addItem(i)
    return i


def test_stale_ocr_never_fills_other_file(window, tmp_path):
    w = window
    a, b = add_file(w, tmp_path/'a.pdf'), add_file(w, tmp_path/'b.pdf')
    w.files.setCurrentItem(a)
    w.on_region((0,0,.5,.5))
    old_done = w.pending[-1][1]
    w.files.setCurrentItem(b)
    old_done([OcrCandidate('v6','wrong file OCR',.99)])
    assert w.result.toPlainText() == '' and w.name_edit.text() == ''
    assert w.current.name == 'b.pdf'


def test_stale_region_does_not_replace_new_ocr(window, tmp_path):
    w = window
    w.files.setCurrentItem(add_file(w, tmp_path/'a.pdf'))
    w.on_region((0,0,.5,.5))
    old_done = w.pending[-1][1]
    w.on_region((.5,.5,1,1))
    w.pending[-1][1]([OcrCandidate('v6','new result',.99)])
    old_done([OcrCandidate('v6','stale result',.99)])

    assert w.name_edit.text() == 'newresult'


def test_gui_rename_undo_next(window, tmp_path):
    w = window
    source = tmp_path/'a.pdf'
    source.write_bytes(b'PDF content untouched')
    add_file(w, source)
    add_file(w, tmp_path/'b.pdf')
    w.files.setCurrentRow(0)
    w.result.setPlainText('测试\n编号001')
    assert w.name_edit.text() == '测试编号001'
    w.rename_button.click()
    assert w.current.name == 'b.pdf'
    assert (tmp_path/'测试编号001.pdf').read_bytes() == b'PDF content untouched'
    w.undo_button.click()
    assert source.read_bytes() == b'PDF content untouched'
    assert w.files.item(0).text() == 'a.pdf'
    assert not w.history


def test_folder_drag_and_drop(window, tmp_path, qt):
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    enter = QDragEnterEvent(QPoint(40,40), Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window, enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(40,40), Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window, drop)
    assert window.folder == tmp_path
    assert drop.isAccepted()


def test_actual_selection_at_fit_and_zoom(window, qt):
    path = Path(__file__).resolve().parents[2]/'test_documents'/'01-中文扫描件.pdf'
    pix, _ = render_first(path)
    w = window
    w.view.show_page(pix.tobytes('png'))
    got = []
    w.view.selected.disconnect()
    w.view.selected.connect(got.append)
    for factor in [1, 1.5]:
        w.view.fit_page()
        if factor != 1:
            w.view.zoom(factor)
        qt.processEvents()
        bounds = w.view.bounds
        # Center stays visible under both fit and zoom.
        p1 = w.view.mapFromScene(bounds.width()*.40, bounds.height()*.40)
        p2 = w.view.mapFromScene(bounds.width()*.60, bounds.height()*.60)
        QTest.mousePress(w.view.viewport(), Qt.MouseButton.LeftButton, pos=p1)
        QTest.mouseMove(w.view.viewport(), p2)
        QTest.mouseRelease(w.view.viewport(), Qt.MouseButton.LeftButton, pos=p2)
        assert got[-1] == pytest.approx((.4,.4,.6,.6), abs=.005)


def png_page(width=1000, height=1400):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


@pytest.mark.parametrize('size', [(1000,1400), (1600,2000)])
def test_file_switch_preserves_zoom_and_page_position(window, qt, tmp_path, size):
    w = window
    for name in ['a.pdf', 'b.pdf', 'c.pdf']:
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, tmp_path/name)
        w.files.addItem(item)
    w.files.setCurrentRow(0)
    w.pending[-1][1]((png_page(), 1))
    w.view.zoom(4)
    w.view.centerOn(660,1022)
    qt.processEvents()
    scale = w.view.transform().m11()
    center = w.view.mapToScene(w.view.viewport().rect().center())
    x, y = center.x()/1000, center.y()/1400
    # A rapid switch clears the scene twice before either render completes.
    w.files.setCurrentRow(1)
    stale = w.pending[-1][1]
    w.files.setCurrentRow(2)
    stale((png_page(600,400), 1))
    w.pending[-1][1]((png_page(*size), 1))
    qt.processEvents()
    new_center = w.view.mapToScene(w.view.viewport().rect().center())
    assert w.view.transform().m11() == pytest.approx(scale)
    assert new_center.x()/size[0] == pytest.approx(x, abs=.003)
    assert new_center.y()/size[1] == pytest.approx(y, abs=.003)
    assert not w.view.fit_mode
    assert w.view.box is None


def test_fit_button_resets_remembered_zoom(window, qt):
    view = window.view
    view.show_page(png_page())
    view.zoom(4)
    view.centerOn(660,1022)
    view.clear()
    view.clear()  # Failed or loading document does not erase remembered zoom.
    view.show_page(png_page())
    assert not view.fit_mode
    view.fit_page()
    scale = view.transform().m11()
    view.clear()
    view.show_page(png_page())
    assert view.fit_mode
    assert view.transform().m11() == pytest.approx(scale)


def test_automatic_filename_has_no_whitespace(window):
    window.result.setPlainText('合 同\u3000编号: A / 123\nB')
    assert window.name_edit.text() == '合同编号A123B'
    window.name_edit.setText('手动 空格')
    assert window.name_edit.text() == '手动 空格'


@pytest.mark.parametrize('automatic', [False,True])
def test_switch_clears_selection_and_retains_zoom(window, qt, tmp_path, automatic):
    w = window
    source = tmp_path/'a.pdf'
    source.write_bytes(b'PDF content')
    add_file(w,source)
    add_file(w,tmp_path/'b.pdf')
    w.files.setCurrentRow(0)
    w.pending[-1][1]((png_page(),1))
    start = w.view.mapFromScene(250,490)
    end = w.view.mapFromScene(700,630)
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,pos=start)
    QTest.mouseMove(w.view.viewport(),end)
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,pos=end)
    assert w.region is not None and w.view.box is not None
    w.pending[-1][1]([OcrCandidate('v6','已识别',.99)])
    w.view.zoom(2)
    scale = w.view.transform().m11()
    if automatic:
        w.rename_current()
    else:
        w.navigate_file(1)
    w.pending[-1][1]((png_page(),1))
    assert w.current.name == 'b.pdf'
    assert w.region is None and w.view.box is None
    assert not w.ocr_button.isEnabled()
    assert w.result.toPlainText() == ''
    assert w.view.transform().m11() == pytest.approx(scale)


def test_symbol_toggle_updates_candidates_once_and_persists(window):
    w = window
    w.current = Path('sample.pdf')
    w.region = (0,0,.5,.5)
    w.recognize()
    done = w.pending[-1][1]
    done([OcrCandidate('v6','##合同',.99),OcrCandidate('v4','￥123',.98)])
    assert w.result.toPlainText() == '##合同'
    pending_count = len(w.pending)
    w.drop_symbol_check.setChecked(True)
    assert len(w.pending) == pending_count  # Changing this filter requires no OCR rerun.
    assert w.result.toPlainText() == '#合同' and w.name_edit.text() == '#合同'
    w.candidate_combo.setCurrentIndex(1)
    assert w.result.toPlainText() == '123'
    w.candidate_combo.setCurrentIndex(0)
    assert w.result.toPlainText() == '#合同'  # Never repeatedly strip the same result.
    w.drop_symbol_check.setChecked(False)
    assert w.result.toPlainText() == '##合同'
    w.drop_symbol_check.setChecked(True)
    assert w.ocr_options_store.load().drop_leading_symbol


def test_symbol_toggle_during_recognition_uses_latest_setting(window):
    w = window
    w.current = Path('sample.pdf')
    w.region = (0,0,.5,.5)
    w.recognize()
    done = w.pending[-1][1]
    w.drop_symbol_check.setChecked(True)
    done([OcrCandidate('v6','*合同',.99)])
    assert w.result.toPlainText() == '合同'
