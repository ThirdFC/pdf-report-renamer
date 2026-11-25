import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidgetItem, QDialog
from app import MainWindow
from shortcuts import DEFAULTS, ShortcutStore, SettingsDialog, validate_shortcuts


@pytest.fixture(scope='module')
def qt():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def w(qt, tmp_path):
    window = MainWindow(ShortcutStore(tmp_path/'settings.json'))
    window.submit = lambda *args: None
    window.show()
    window.activateWindow()
    qt.processEvents()
    yield window
    window.close()
    window.deleteLater()
    qt.processEvents()


def add(w, path):
    item = QListWidgetItem(path.name)
    item.setData(Qt.ItemDataRole.UserRole, path)
    w.files.addItem(item)
    return item


def test_store_persists_and_clear_disables(tmp_path):
    store = ShortcutStore(tmp_path/'nested'/'settings.json')
    assert store.load() == DEFAULTS
    values = dict(DEFAULTS, previous='Alt+Up', next='Alt+Down', rename='F8', undo='')
    store.save(values)
    assert ShortcutStore(store.path).load() == values


@pytest.mark.parametrize('changes', [
    {'rename':'Down'}, {'rename':'Ctrl+R'}, {'rename':'Ctrl+K, Ctrl+C'},
    {'rename':'Esc'}, {'next':'Alt+F4'}, {'rename':'not a valid key'},
])
def test_invalid_or_duplicate(changes):
    with pytest.raises(ValueError):
        validate_shortcuts(dict(DEFAULTS, **changes))


def test_two_disabled_are_valid():
    values = dict(DEFAULTS, previous='', next='')
    assert validate_shortcuts(values) == values


def test_corrupt_file_uses_defaults(tmp_path):
    path = tmp_path/'settings.json'
    path.write_text('{invalid', encoding='utf-8')
    store = ShortcutStore(path)
    assert store.load() == DEFAULTS
    assert store.warning


def test_failed_save_preserves_previous_settings(tmp_path, monkeypatch):
    store = ShortcutStore(tmp_path/'settings.json')
    store.save(DEFAULTS)
    before = store.path.read_bytes()
    def fail(*args):
        raise OSError('模拟无法写入')
    monkeypatch.setattr('shortcuts.os.replace', fail)
    with pytest.raises(OSError):
        store.save(dict(DEFAULTS, rename='F8'))
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))


def test_dialog_conflict_cancel_defaults_and_save(w, qt):
    d = SettingsDialog(w, w.shortcut_store, DEFAULTS)
    d.show()
    d.editors['rename'].setKeySequence(QKeySequence('Down'))
    d.save_button.click()
    assert '冲突' in d.error.text()
    assert d.result() != QDialog.DialogCode.Accepted
    assert not w.shortcut_store.path.exists()
    d.restore_defaults()
    assert d.editors['rename'].keySequence() == QKeySequence(DEFAULTS['rename'])
    d.editors['rename'].setKeySequence(QKeySequence('F8'))
    d.cancel_button.click()
    assert not w.shortcut_store.path.exists()
    d = SettingsDialog(w, w.shortcut_store, DEFAULTS)
    d.editors['rename'].setKeySequence(QKeySequence('F8'))
    d.save_button.click()
    assert d.result() == QDialog.DialogCode.Accepted
    assert ShortcutStore(w.shortcut_store.path).load()['rename'] == 'F8'


def test_navigation_skips_hidden_and_stops_at_edges(w, tmp_path):
    for name in ['a.pdf', 'hidden.pdf', 'c.pdf']:
        add(w, tmp_path/name)
    w.files.item(1).setHidden(True)
    w.files.setCurrentRow(0)
    w.navigate_file(1)
    assert w.current.name == 'c.pdf'
    w.navigate_file(1)
    assert w.current.name == 'c.pdf'
    w.navigate_file(-1)
    assert w.current.name == 'a.pdf'
    w.navigate_file(-1)
    assert w.current.name == 'a.pdf'


def test_actual_navigation_keys_and_editing(w, qt, tmp_path):
    add(w, tmp_path/'a.pdf')
    add(w, tmp_path/'b.pdf')
    w.files.setCurrentRow(0)
    w.view.setFocus()
    qt.processEvents()
    QTest.keyClick(w.view, Qt.Key.Key_Down)
    assert w.current.name == 'b.pdf'
    QTest.keyClick(w.view, Qt.Key.Key_Up)
    assert w.current.name == 'a.pdf'
    w.result.setPlainText('第一行\n第二行')
    w.result.setFocus()
    qt.processEvents()
    QTest.keyClick(w.result, Qt.Key.Key_Down)
    assert w.current.name == 'a.pdf'
    w.apply_shortcuts(dict(DEFAULTS, next='Alt+Down', previous='Alt+Up'))
    QTest.keyClick(w.result, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert w.current.name == 'b.pdf'


def test_custom_rename_key_and_rebinding(w, qt, tmp_path):
    source = tmp_path/'a.pdf'
    source.write_bytes(b'PDF unchanged')
    add(w, source)
    w.files.setCurrentRow(0)
    w.name_edit.setText('新名称')
    w.apply_shortcuts(dict(DEFAULTS, rename='F8'))
    w.view.setFocus()
    qt.processEvents()
    # Previous binding is inactive immediately, even before deleteLater runs.
    QTest.keyClick(w.view, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert source.exists()
    QTest.keyClick(w.view, Qt.Key.Key_F8)
    assert (tmp_path/'新名称.pdf').read_bytes() == b'PDF unchanged'
    assert not source.exists()
    QTest.keyClick(w.view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert source.read_bytes() == b'PDF unchanged'


def test_default_confirmation_from_name_editor(w, qt, tmp_path):
    source = tmp_path/'a.pdf'
    source.write_bytes(b'PDF unchanged')
    add(w, source)
    w.files.setCurrentRow(0)
    w.name_edit.setText('默认快捷键')
    w.name_edit.setFocus()
    qt.processEvents()
    QTest.keyClick(w.name_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert (tmp_path/'默认快捷键.pdf').exists()


def test_enter_binding_from_name_editor(w, qt, tmp_path):
    source = tmp_path/'a.pdf'
    source.write_bytes(b'a')
    add(w, source)
    w.files.setCurrentRow(0)
    w.name_edit.setText('Enter确认')
    w.apply_shortcuts(dict(DEFAULTS, rename='Return'))
    w.name_edit.setFocus()
    qt.processEvents()
    QTest.keyClick(w.name_edit, Qt.Key.Key_Return)
    assert (tmp_path/'Enter确认.pdf').exists()


def test_ctrl_enter_from_ocr_editor(w, qt, tmp_path):
    source = tmp_path/'a.pdf'
    source.write_bytes(b'a')
    add(w, source)
    w.files.setCurrentRow(0)
    w.result.setPlainText('OCR确认')
    w.result.setFocus()
    qt.processEvents()
    QTest.keyClick(w.result, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert (tmp_path/'OCR确认.pdf').exists()


def test_restart_loads_applied_values(w, qt):
    values = dict(DEFAULTS, rename='F8', next='Alt+Down')
    w.shortcut_store.save(values)
    second = MainWindow(ShortcutStore(w.shortcut_store.path))
    assert second.shortcut_values == values
    assert second.shortcuts['rename'].key() == QKeySequence('F8')
    assert 'F8' in second.rename_button.text()
    second.close()


def test_record_shortcut_in_modal_does_not_rename(w, qt, tmp_path):
    source = tmp_path/'a.pdf'
    source.write_bytes(b'a')
    add(w, source)
    w.files.setCurrentRow(0)
    w.name_edit.setText('should not be renamed')
    d = SettingsDialog(w, w.shortcut_store, DEFAULTS)
    d.show()
    d.activateWindow()
    editor = d.editors['rename']
    editor.setFocus()
    qt.processEvents()
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert source.exists()
    assert d.isVisible()
    assert editor.keySequence() == QKeySequence('Ctrl+Return')
    d.reject()
