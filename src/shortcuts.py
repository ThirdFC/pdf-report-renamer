"""Application shortcuts, persistent per-user settings. AGPL-3.0-or-later."""
import json
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QFormLayout, QKeySequenceEdit, QPushButton, QDialogButtonBox)


# IDs remain stable when UI labels or application versions change.
ACTIONS = {
    'previous': ('上一个文件', 'Up'),
    'next': ('下一个文件', 'Down'),
    'rename': ('确认重命名', 'Ctrl+Return'),
    'recognize': ('重新识别选区', 'Ctrl+R'),
    'undo': ('撤销上次重命名', 'Ctrl+Z'),
    'choose_folder': ('选择文件夹', 'Ctrl+O'),
    'focus_name': ('编辑新文件名', 'F2'),
    'fit_page': ('预览适合页面', 'Ctrl+0'),
    'settings': ('打开设置', 'Ctrl+,'),
}
DEFAULTS = {action: value[1] for action, value in ACTIONS.items()}


def display_key(value):
    return QKeySequence(value).toString(QKeySequence.SequenceFormat.NativeText).replace('Return', 'Enter')


def validate_shortcuts(values):
    """Return normalized complete map, or reject before saving/applying."""
    result = {}
    used = {}
    for action, (name, _) in ACTIONS.items():
        value = values.get(action, '')
        if not isinstance(value, str):
            raise ValueError(f'“{name}”的快捷键格式无效。')
        sequence = QKeySequence.fromString(value, QKeySequence.SequenceFormat.PortableText)
        if value and (sequence.isEmpty() or sequence.count() != 1
                      or sequence[0].key().value in (0, 0x1ffffff)):
            raise ValueError(f'“{name}”请设置一个按键或组合键，不支持连续多组按键。')
        canonical = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if canonical in ('Esc', 'Tab', 'Backtab', 'Alt+F4'):
            raise ValueError(f'{display_key(canonical)} 用于关闭窗口或移动焦点，请选择其他快捷键。')
        if canonical:
            for other, other_sequence in used.items():
                if sequence.matches(other_sequence) != QKeySequence.SequenceMatch.NoMatch:
                    raise ValueError(f'快捷键冲突：“{name}”和“{ACTIONS[other][0]}”均使用 {display_key(canonical)}。')
            used[action] = sequence
        result[action] = canonical
    return result


class ShortcutStore:
    def __init__(self, path=None):
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
        self.path = Path(path) if path else Path(base)/'LocalTools'/'PdfRegionRenamer'/'settings.json'
        self.warning = ''

    def load(self):
        if not self.path.exists():
            return dict(DEFAULTS)
        try:
            content = json.loads(self.path.read_text(encoding='utf-8'))
            if content.get('version') != 1 or not isinstance(content.get('shortcuts'), dict):
                raise ValueError('设置文件格式无效')
            return validate_shortcuts(dict(DEFAULTS, **content['shortcuts']))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.warning = f'快捷键设置读取失败，已使用默认值：{exc}'
            return dict(DEFAULTS)

    def save(self, values):
        values = validate_shortcuts(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                                             prefix='shortcuts_', suffix='.tmp', delete=False) as stream:
                temp = Path(stream.name)
                json.dump({'version': 1, 'shortcuts': values}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            if temp is not None and temp.exists():
                temp.unlink()
        return values


class SettingsDialog(QDialog):
    def __init__(self, parent, store, values):
        super().__init__(parent)
        self.store = store
        self.values = dict(values)
        self.setWindowTitle('设置 · 快捷键')
        self.setModal(True)
        self.setMinimumWidth(570)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel('快捷键设置')
        title.setObjectName('section')
        layout.addWidget(title)
        hint = QLabel('点击输入框后，直接按下想使用的按键或组合键。\n点击右侧 × 可清除快捷键；保存后立即生效，并在下次启动时保留。')
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(10)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.editors = {}
        for action, (name, _) in ACTIONS.items():
            editor = QKeySequenceEdit(QKeySequence(values[action]))
            editor.setMaximumSequenceLength(1)
            editor.setClearButtonEnabled(True)
            editor.setMinimumWidth(260)
            editor.setAccessibleName(name + '快捷键')
            self.editors[action] = editor
            form.addRow(name, editor)
        layout.addLayout(form)
        note = QLabel('快捷键仅在本软件内生效。文字输入框中的方向键和常用编辑快捷键用于编辑。\n如需在输入文字时切换文件，建议设置 Alt+↑ / Alt+↓。')
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addSpacing(8)
        layout.addWidget(note)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: #bd2638;')
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        self.reset_button = QPushButton('恢复默认')
        self.reset_button.clicked.connect(self.restore_defaults)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton('保存设置')
        self.save_button.setObjectName('primary')
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        # Capturing Enter in an editor must never implicitly save the dialog.
        for button in (self.reset_button, self.cancel_button, self.save_button):
            button.setAutoDefault(False)
            button.setDefault(False)

    def restore_defaults(self):
        for action, editor in self.editors.items():
            editor.setKeySequence(QKeySequence(DEFAULTS[action]))
        self.error.clear()

    def save(self):
        values = {action: editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                  for action, editor in self.editors.items()}
        try:
            self.values = self.store.save(values)
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()
