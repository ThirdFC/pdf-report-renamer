"""PDF 框选重命名 v1.3.2 — AGPL-3.0-or-later."""
import os
import sys
from pathlib import Path
from dataclasses import replace

from PySide6.QtCore import Qt, Signal, QObject, QRunnable, QThreadPool, QRectF, QTimer, QEvent
from PySide6.QtGui import (QColor, QFont, QImage, QPixmap, QPainter, QPen,
                           QBrush, QIcon, QShortcut, QKeySequence)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QFileDialog, QListWidget, QListWidgetItem,
    QLineEdit, QTextEdit, QCheckBox, QSplitter, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QMessageBox, QFrame, QSizePolicy, QComboBox, QScrollArea, QLayout)

from report_ui import ReportReviewMixin
from core import scan_pdfs, render_first, OcrEngine, clean_name, rename_pdf, undo_rename, remove_whitespace
from shortcuts import ShortcutStore, SettingsDialog, ACTIONS, display_key, validate_shortcuts
from ocr_processing import OcrOptions, OcrOptionsStore, MODELS, ENHANCEMENTS, filter_leading_symbol


class Signals(QObject):
    done = Signal(object)
    error = Signal(str)


class Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn, self.signals = fn, Signals()

    def run(self):
        try:
            self.signals.done.emit(self.fn())
        except Exception as exc:
            self.signals.error.emit(str(exc) or type(exc).__name__)


class OcrComboBox(QComboBox):
    def __init__(self):
        super().__init__()
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(9)

    def event(self, event):
        if (event.type() == QEvent.Type.ShortcutOverride
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and event.key() in (Qt.Key.Key_Up,Qt.Key.Key_Down,Qt.Key.Key_Left,Qt.Key.Key_Right,
                                    Qt.Key.Key_Home,Qt.Key.Key_End,Qt.Key.Key_PageUp,Qt.Key.Key_PageDown)):
            event.accept()
            return True
        return super().event(event)


class PageView(QGraphicsView):
    selected = Signal(object)
    def __init__(self):
        super().__init__()
        self.canvas = QGraphicsScene(self)
        self.setScene(self.canvas)
        self.setBackgroundBrush(QColor('#e8edf3'))
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.bounds, self.box, self.start = QRectF(), None, None
        self.fit_mode = True
        self.saved_view = None
        self.setAccessibleName('PDF 第一页预览，按住鼠标左键拖动框选文字')

    def clear(self):
        # Keep the viewport through loading, rapid switches and failed previews.
        if not self.bounds.isEmpty():
            if self.fit_mode:
                self.saved_view = None
            else:
                center = self.mapToScene(self.viewport().rect().center())
                self.saved_view = (self.transform(),
                    max(0., min(1., center.x()/self.bounds.width())),
                    max(0., min(1., center.y()/self.bounds.height())))
        self.canvas.clear()
        self.bounds, self.box, self.start = QRectF(), None, None

    def show_page(self, png):
        self.clear()
        image = QImage.fromData(png)
        item = self.canvas.addPixmap(QPixmap.fromImage(image))
        self.bounds = item.boundingRect()
        self.canvas.setSceneRect(self.bounds.adjusted(-15, -15, 15, 15))
        if self.saved_view is not None:
            transform, x, y = self.saved_view
            self.fit_mode = False
            self.setTransform(transform)
            self.centerOn(x*self.bounds.width(), y*self.bounds.height())
        else:
            self.fit_page()

    def fit_page(self):
        self.fit_mode = True
        self.saved_view = None
        if not self.bounds.isEmpty():
            self.fitInView(self.bounds.adjusted(-15, -15, 15, 15), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, factor):
        if not self.bounds.isEmpty() and 0.08 < self.transform().m11() * factor < 5:
            self.fit_mode = False
            self.scale(factor, factor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit_mode:
            self.fit_page()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.bounds.isEmpty():
            point = self.mapToScene(event.position().toPoint())
            if self.bounds.contains(point):
                self.start = point
                if self.box:
                    self.canvas.removeItem(self.box)
                self.box = QGraphicsRectItem()
                pen = QPen(QColor('#2165e8'), 2)
                pen.setCosmetic(True)
                self.box.setPen(pen)
                self.box.setBrush(QBrush(QColor(33, 101, 232, 30)))
                self.canvas.addItem(self.box)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.start is not None:
            rect = QRectF(self.start, self.mapToScene(event.position().toPoint())).normalized().intersected(self.bounds)
            self.box.setRect(rect)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.start is not None and event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self.start = None
            r = self.box.rect()
            if r.width() > 3 and r.height() > 3:
                self.selected.emit((r.left()/self.bounds.width(), r.top()/self.bounds.height(),
                                    r.right()/self.bounds.width(), r.bottom()/self.bounds.height()))
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if event.angleDelta().y() > 0 else 1/1.15)
            event.accept()
        else:
            super().wheelEvent(event)


STYLE = '''
QMainWindow, QWidget { background: #f5f7fb; color: #24344b; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QFrame#header { background: #172c4b; }
QFrame#header QLabel { background: transparent; color: white; }
QLabel#title { font-size: 23px; font-weight: 700; }
QLabel#muted { color: #6d7d92; }
QLabel#section { font-size: 15px; font-weight: 700; }
QPushButton { background: white; border: 1px solid #d5dfec; border-radius: 7px; padding: 9px 14px; }
QPushButton:hover { background: #eaf1ff; border-color: #91b2f3; }
QPushButton:disabled { color: #9ca8b8; background: #edf0f5; border-color: #e0e5ec; }
QPushButton#primary { background: #2563eb; color: white; border: 0; font-weight: 700; }
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton#primary:disabled { background: #bdcbea; color: #f5f7fb; }
QLineEdit, QTextEdit, QKeySequenceEdit { background: white; border: 1px solid #d5dfec; border-radius: 7px; padding: 9px; selection-background-color: #2563eb; }
QKeySequenceEdit QLineEdit { border: 0; padding: 0; }
QComboBox { background: white; border: 1px solid #d5dfec; border-radius: 7px; padding: 6px; }
QLineEdit:focus, QTextEdit:focus { border-color: #2563eb; }
QListWidget { background: white; border: 1px solid #dce4ef; border-radius: 8px; outline: 0; }
QListWidget::item { padding: 12px 7px; border-bottom: 1px solid #edf1f6; }
QListWidget::item:selected { background: #e5eeff; color: #174ea6; border-left: 3px solid #2563eb; }
QListWidget::item:hover { background: #f0f5ff; }
QCheckBox { spacing: 7px; }
QGraphicsView { border: 1px solid #d9e2ed; border-radius: 8px; }
QSplitter::handle { background: #f5f7fb; width: 10px; }
QStatusBar { background: #eaf0f7; color: #51657e; }
'''


def label(text, name=None):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(True)
    return widget


class MainWindow(ReportReviewMixin,QMainWindow):
    def __init__(self, shortcut_store=None):
        super().__init__()
        self.setWindowTitle('PDF 框选重命名 1.3.2')
        self.resize(1380, 880)
        self.setMinimumSize(1040, 690)
        self.setAcceptDrops(True)
        self.pool = QThreadPool(self)
        # Keep all PDF operations on a serial worker: MuPDF is not reentrant.
        self.pool.setMaxThreadCount(1)
        self.ocr = OcrEngine()
        self.jobs = set()
        self.folder = self.current = self.region = None
        self.generation = self.scan_generation = self.ocr_generation = 0
        self.loading = False
        self.history = []
        self.shortcut_store = shortcut_store or ShortcutStore()
        self.shortcut_values = self.shortcut_store.load()
        self.ocr_options_store = OcrOptionsStore(self.shortcut_store.path.with_name('ocr.json'))
        self.ocr_options = self.ocr_options_store.load()
        self.shortcuts = {}
        self.init_report_state()
        self.build_ui()
        self.apply_shortcuts(self.shortcut_values)
        self.statusBar().showMessage(self.shortcut_store.warning or '就绪 · PDF 内容与识别区域均在本机处理')

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QFrame()
        header.setObjectName('header')
        bar = QHBoxLayout(header)
        bar.setContentsMargins(24, 18, 24, 18)
        titles = QVBoxLayout()
        titles.addWidget(label('PDF 框选重命名', 'title'))
        titles.addWidget(label('01  导入文件夹     →     02  框选第一页文字     →     03  确认并重命名'))
        bar.addLayout(titles, 1)
        self.choose_button = QPushButton('选择文件夹')
        self.choose_button.clicked.connect(self.choose_folder)
        bar.addWidget(self.choose_button)
        self.settings_button = QPushButton('设置')
        self.settings_button.clicked.connect(self.open_settings)
        bar.addWidget(self.settings_button)
        layout.addWidget(header)
        splitter = QSplitter()
        splitter.setContentsMargins(16, 12, 16, 12)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(label('文件列表', 'section'))
        self.folder_label = label('将文件夹拖到窗口内\n或点击右上角选择文件夹', 'muted')
        self.folder_label.setMaximumHeight(76)
        lv.addWidget(self.folder_label)
        self.recursive = QCheckBox('包含子文件夹')
        self.recursive.setChecked(True)
        self.recursive.toggled.connect(lambda: self.load_folder(self.folder) if self.folder else None)
        lv.addWidget(self.recursive)
        self.add_report_mode_control(lv)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索文件名…')
        self.search.textChanged.connect(self.filter_list)
        lv.addWidget(self.search)
        self.count = label('0 个 PDF', 'muted')
        lv.addWidget(self.count)
        self.files = QListWidget()
        self.files.setAccessibleName('PDF 文件列表')
        self.files.currentItemChanged.connect(self.select_file)
        lv.addWidget(self.files, 1)
        navigation = QHBoxLayout()
        self.previous_button = QPushButton('↑ 上一个')
        self.previous_button.clicked.connect(lambda: self.navigate_file(-1))
        self.next_button = QPushButton('↓ 下一个')
        self.next_button.clicked.connect(lambda: self.navigate_file(1))
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.next_button)
        lv.addLayout(navigation)
        refresh = QPushButton('刷新文件夹')
        refresh.clicked.connect(lambda: self.load_folder(self.folder) if self.folder else self.choose_folder())
        lv.addWidget(refresh)
        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.page_info = label('第一页预览', 'section')
        toolbar.addWidget(self.page_info, 1)
        for text, fn in [('−', lambda: self.view.zoom(1/1.2)), ('＋', lambda: self.view.zoom(1.2)), ('适合页面', lambda: self.view.fit_page())]:
            button = QPushButton(text)
            button.clicked.connect(fn)
            toolbar.addWidget(button)
        cv.addLayout(toolbar)
        self.view = PageView()
        self.view.selected.connect(self.on_region)
        cv.addWidget(self.view, 1)
        cv.addWidget(label('鼠标左键拖动框选 · 松开后自动 OCR · Ctrl + 滚轮缩放 · 滚动条移动页面', 'muted'))
        right = QWidget()
        right.setMinimumWidth(270)
        right.setMaximumWidth(370)
        rv = QVBoxLayout(right)
        rv.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        rv.setContentsMargins(6, 0, 0, 0)
        rv.addWidget(label('识别与重命名', 'section'))
        self.current_label = label('尚未选择文件', 'muted')
        self.current_label.setMaximumHeight(85)
        rv.addWidget(self.current_label)
        self.build_report_panel(rv,OcrComboBox)
        self.manual_ocr_panel = QWidget()
        mv = QVBoxLayout(self.manual_ocr_panel)
        mv.setContentsMargins(0,0,0,0)
        rv.addWidget(self.manual_ocr_panel)
        self.model_combo = OcrComboBox()
        self.model_combo.setAccessibleName('OCR 模型')
        for key, text in MODELS.items():
            self.model_combo.addItem(text,key)
        self.model_combo.setCurrentIndex(self.model_combo.findData(self.ocr_options.model))
        self.model_combo.setToolTip('切换识别模型，或用两个模型识别同一选区后选择结果')
        mv.addWidget(self.model_combo)
        image_options = QHBoxLayout()
        self.dpi_combo = OcrComboBox()
        self.dpi_combo.setAccessibleName('OCR 渲染分辨率')
        for dpi in (300,450,600):
            self.dpi_combo.addItem(f'{dpi} DPI',dpi)
        self.dpi_combo.setCurrentIndex(self.dpi_combo.findData(self.ocr_options.dpi))
        self.enhancement_combo = OcrComboBox()
        self.enhancement_combo.setAccessibleName('OCR 图像增强')
        for key, text in ENHANCEMENTS.items():
            self.enhancement_combo.addItem(text,key)
        self.enhancement_combo.setCurrentIndex(self.enhancement_combo.findData(self.ocr_options.enhancement))
        image_options.addWidget(self.dpi_combo)
        image_options.addWidget(self.enhancement_combo,1)
        mv.addLayout(image_options)
        self.deskew_check = QCheckBox('自动纠正轻微倾斜')
        self.deskew_check.setChecked(self.ocr_options.deskew)
        self.deskew_check.setToolTip('纠正约 ±10° 内的轻微倾斜；若效果不理想可取消勾选后重新识别')
        mv.addWidget(self.deskew_check)
        for combo in (self.model_combo,self.dpi_combo,self.enhancement_combo):
            combo.currentIndexChanged.connect(self.change_ocr_options)
        self.deskew_check.toggled.connect(self.change_ocr_options)
        self.drop_symbol_check = QCheckBox('删除识别结果开头的符号')
        self.drop_symbol_check.setChecked(self.ocr_options.drop_leading_symbol)
        self.drop_symbol_check.setToolTip('只删除开头的一个标点或符号，例如 *合同 → 合同；字母、数字和汉字不受影响')
        self.drop_symbol_check.toggled.connect(self.change_symbol_filter)
        mv.addWidget(self.drop_symbol_check)
        mv.addWidget(label('框中文字 · 可手动修改'))
        self.candidate_combo = OcrComboBox()
        self.candidate_combo.setAccessibleName('双模型识别候选结果')
        self.candidate_combo.currentIndexChanged.connect(self.choose_candidate)
        self.candidate_combo.hide()
        mv.addWidget(self.candidate_combo)
        self.result = QTextEdit()
        self.result.setAccessibleName('OCR 识别结果')
        self.result.setPlaceholderText('先在预览中框选文字\n松开鼠标后自动识别')
        self.result.setMinimumHeight(65)
        self.result.setMaximumHeight(140)
        self.result.textChanged.connect(self.result_changed)
        mv.addWidget(self.result, 1)
        self.ocr_status = label('支持中文、英文、数字；离线识别', 'muted')
        mv.addWidget(self.ocr_status)
        self.ocr_button = QPushButton('重新识别选区')
        self.ocr_button.clicked.connect(self.recognize)
        self.ocr_button.setEnabled(False)
        mv.addWidget(self.ocr_button)
        rv.addSpacing(16)
        rv.addWidget(label('新文件名 · 可编辑'))
        self.name_edit = QLineEdit()
        self.name_edit.setAccessibleName('新文件名')
        self.name_edit.setPlaceholderText('识别结果将自动填入此处')
        self.name_edit.textChanged.connect(self.update_target)
        self.name_edit.textEdited.connect(self.mark_review_override)
        rv.addWidget(self.name_edit)
        self.target_label = label('自动保留 .pdf 后缀', 'muted')
        rv.addWidget(self.target_label)
        self.rename_button = QPushButton('确认重命名')
        self.rename_button.setObjectName('primary')
        self.rename_button.setEnabled(False)
        self.rename_button.clicked.connect(self.rename_current)
        rv.addWidget(self.rename_button)
        self.next_check = QCheckBox('重命名后自动选中下一个')
        self.next_check.setChecked(True)
        rv.addWidget(self.next_check)
        self.undo_button = QPushButton('撤销上次重命名')
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self.undo)
        rv.addWidget(self.undo_button)
        rv.addStretch(1)
        self.preservation_note = label('原地修改文件名，不改动 PDF 内容。\n同名文件会提示，不会覆盖。\n撤销记录仅保留到关闭软件。', 'muted')
        rv.addWidget(self.preservation_note)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setMinimumWidth(280)
        right_scroll.setMaximumWidth(390)
        right_scroll.setWidget(right)
        for widget in [left, center, right_scroll]:
            splitter.addWidget(widget)
        splitter.setSizes([260, 760, 300])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

    def apply_shortcuts(self, values):
        values = validate_shortcuts(values)
        for shortcut in self.shortcuts.values():
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.shortcuts.clear()
        self.shortcut_values = dict(values)
        callbacks = {
            'previous': lambda: self.navigate_file(-1),
            'next': lambda: self.navigate_file(1),
            'rename': self.rename_button.click,
            'recognize': self.ocr_button.click,
            'undo': self.undo_button.click,
            'choose_folder': self.choose_folder,
            'focus_name': self.name_edit.setFocus,
            'fit_page': self.view.fit_page,
            'settings': self.open_settings,
        }
        for action, value in values.items():
            if value:
                shortcut = QShortcut(QKeySequence(value), self)
                shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
                shortcut.setAutoRepeat(False)
                shortcut.activated.connect(callbacks[action])
                self.shortcuts[action] = shortcut
        widgets = {
            'previous': self.previous_button, 'next': self.next_button,
            'rename': self.rename_button, 'recognize': self.ocr_button,
            'undo': self.undo_button, 'choose_folder': self.choose_button,
            'focus_name': self.name_edit, 'settings': self.settings_button,
        }
        for action, widget in widgets.items():
            key = display_key(values[action])
            widget.setToolTip(ACTIONS[action][0] + (f'（{key}）' if key else '（未设置快捷键）'))
        for action, widget in [('rename', self.rename_button), ('undo', self.undo_button)]:
            key = display_key(values[action])
            widget.setText(ACTIONS[action][0] + (f'  {key}' if key else ''))
        self.update_review_approve_label()

    def open_settings(self):
        if self.bulk_running:
            return
        dialog = SettingsDialog(self, self.shortcut_store, self.shortcut_values)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self.apply_shortcuts(dialog.values)
            self.statusBar().showMessage('快捷键设置已保存，立即生效')

    def navigate_file(self, direction):
        if self.bulk_running:
            return
        row = self.files.currentRow()
        if row < 0:
            row = -1 if direction > 0 else self.files.count()
        for candidate in range(row + direction, self.files.count() if direction > 0 else -1, direction):
            item = self.files.item(candidate)
            if not item.isHidden():
                self.files.setCurrentItem(item)
                self.files.scrollToItem(item)
                return

    def submit(self, fn, done, failed):
        job = Job(fn)
        self.jobs.add(job)
        job.signals.done.connect(done)
        job.signals.error.connect(failed)
        job.signals.done.connect(lambda _: self.jobs.discard(job))
        job.signals.error.connect(lambda _: self.jobs.discard(job))
        self.pool.start(job)

    def clear_candidates(self):
        self.candidate_combo.blockSignals(True)
        self.candidate_combo.clear()
        self.candidate_combo.blockSignals(False)
        self.candidate_combo.hide()

    def change_ocr_options(self):
        options = OcrOptions(model=self.model_combo.currentData(),dpi=self.dpi_combo.currentData(),
                             enhancement=self.enhancement_combo.currentData(),deskew=self.deskew_check.isChecked(),
                             drop_leading_symbol=self.drop_symbol_check.isChecked())
        self.ocr_options = options
        self.ocr_generation += 1
        self.clear_candidates()
        self.set_result_text('')
        self.name_edit.clear()
        self.ocr_button.setEnabled(bool(self.current and self.region))
        self.ocr_status.setText('识别选项已更新，请框选或点击重新识别')
        try:
            self.ocr_options_store.save(options)
        except OSError as exc:
            self.statusBar().showMessage(f'本次识别选项已生效，但无法保存：{exc}')

        if self.report_mode.isChecked():
            self.invalidate_report_results()
            self.start_review()

    def candidate_caption(self, candidate):
        text = filter_leading_symbol(candidate.text,self.ocr_options.drop_leading_symbol)
        return MODELS[candidate.model] + '：' + (('失败：'+candidate.error) if candidate.error else (text or '未识别到文字'))

    def change_symbol_filter(self, enabled):
        self.ocr_options = replace(self.ocr_options,drop_leading_symbol=enabled)
        for i in range(self.candidate_combo.count()):
            caption = self.candidate_caption(self.candidate_combo.itemData(i))
            self.candidate_combo.setItemText(i,caption[:70])
            self.candidate_combo.setItemData(i,caption,Qt.ItemDataRole.ToolTipRole)
        self.choose_candidate(self.candidate_combo.currentIndex())
        try:
            self.ocr_options_store.save(self.ocr_options)
        except OSError as exc:
            self.statusBar().showMessage(f'本次开关已生效，但无法保存：{exc}')

    def choose_candidate(self, index):
        if index < 0:
            return
        candidate = self.candidate_combo.itemData(index)
        if candidate and not candidate.error:
            self.set_result_text(filter_leading_symbol(candidate.text,self.ocr_options.drop_leading_symbol))
            status = (f'{MODELS[candidate.model]} · 置信度 {candidate.confidence:.0%}'
                      if candidate.text else f'{MODELS[candidate.model]}未识别到文字，请更换图像增强方式或选区')
            if candidate.angle:
                status += f' · 已纠偏 {abs(candidate.angle):.1f}°'
            self.ocr_status.setText(status)

    def choose_folder(self):
        if self.bulk_running:
            return
        folder = QFileDialog.getExistingDirectory(self, '选择包含 PDF 的文件夹', str(self.folder or Path.home()))
        if folder:
            self.load_folder(Path(folder))

    def dragEnterEvent(self, event):
        if self.bulk_running:
            return
        if event.mimeData().hasUrls() and any(Path(u.toLocalFile()).is_dir() for u in event.mimeData().urls() if u.isLocalFile()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self.bulk_running:
            return
        folders = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile() and Path(u.toLocalFile()).is_dir()]
        if folders:
            self.load_folder(folders[0])
            event.acceptProposedAction()

    def load_folder(self, folder):
        if self.bulk_running:
            self.statusBar().showMessage('请先停止批量通过，再切换文件夹')
            return
        folder = Path(folder)
        if not folder.is_dir():
            return self.show_error('文件夹不存在或无法访问。')
        self.reset_report_folder()
        self.folder = folder
        self.folder_label.setText(folder.name or str(folder))
        self.folder_label.setToolTip(str(folder))
        self.scan_generation += 1
        token = self.scan_generation
        recursive = self.recursive.isChecked()
        self.files.clear()
        self.count.setText('正在查找 PDF…')
        self.statusBar().showMessage('正在扫描文件夹…')
        def done(data):
            if token != self.scan_generation:
                return
            paths, errors = data
            self.files.setUpdatesEnabled(False)
            for path in paths:
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(str(path.relative_to(folder)))
                self.files.addItem(item)
            self.files.setUpdatesEnabled(True)
            self.filter_list()
            self.statusBar().showMessage(f'找到 {len(paths)} 个 PDF' + (f'；{len(errors)} 个目录无法读取' if errors else ''))
            for i in range(self.files.count()):
                if not self.files.item(i).isHidden():
                    self.files.setCurrentRow(i)
                    break
            if self.report_mode.isChecked():
                self.start_review()
        self.submit(lambda: scan_pdfs(folder, recursive), done,
                    lambda e: self.show_error(e) if token == self.scan_generation else None)

    def filter_list(self):
        needle = self.search.text().casefold()
        count = 0
        for i in range(self.files.count()):
            item = self.files.item(i)
            hidden = needle not in item.data(Qt.ItemDataRole.UserRole).name.casefold()
            item.setHidden(hidden)
            count += not hidden
        self.count.setText(f'{count} / {self.files.count()} 个 PDF')

    def select_file(self, item, previous=None):
        self.generation += 1
        self.ocr_generation += 1
        token = self.generation
        self.current = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.region = None
        self.report_selected()
        self.view.clear()
        self.clear_candidates()
        self.set_result_text('')
        self.name_edit.clear()
        self.ocr_button.setEnabled(False)
        self.ocr_status.setText('在第一页预览中框选需要的文字')
        if not self.current:
            self.current_label.setText('尚未选择文件')
            self.page_info.setText('第一页预览')
            return
        path = self.current
        self.current_label.setText(path.name)
        self.current_label.setToolTip(str(path))
        self.page_info.setText('正在加载第一页…')
        def load():
            pix, pages = render_first(path)
            return pix.tobytes('png'), pages
        def done(data):
            if token != self.generation:
                return
            self.view.show_page(data[0])
            self.page_info.setText(f'第 1 页 / 共 {data[1]} 页')
            self.report_preview_ready()
        def failed(error):
            if token == self.generation:
                self.page_info.setText('预览失败')
                self.ocr_status.setText(error)
                self.statusBar().showMessage(error)
        self.submit(load, done, failed)

    def on_region(self, region):
        if self.bulk_running:
            return
        if self.handle_report_region(region):
            return
        self.region = region
        self.recognize()

    def recognize(self):
        if not self.current or not self.region:
            return
        self.ocr_generation += 1
        token = self.ocr_generation
        path, region = self.current, self.region
        options = self.ocr_options
        self.clear_candidates()
        self.set_result_text('')
        self.ocr_button.setEnabled(False)
        self.ocr_status.setText('正在离线识别…首次识别需要加载模型')
        def done(data):
            if token != self.ocr_generation:
                return
            self.candidate_combo.blockSignals(True)
            for candidate in data:
                caption = self.candidate_caption(candidate)
                self.candidate_combo.addItem(caption[:70],candidate)
                idx = self.candidate_combo.count()-1
                self.candidate_combo.setItemData(idx,caption,Qt.ItemDataRole.ToolTipRole)
                if candidate.error:
                    self.candidate_combo.model().item(idx).setEnabled(False)
            selected = next((i for i,c in enumerate(data) if c.text and not c.error),
                            next((i for i,c in enumerate(data) if not c.error),0))
            self.candidate_combo.setCurrentIndex(selected)
            self.candidate_combo.blockSignals(False)
            self.candidate_combo.setVisible(len(data)>1)
            self.choose_candidate(selected)
            errors = [c.error for c in data if c.error]
            if errors:
                self.ocr_status.setText(self.ocr_status.text()+'；另一模型失败：'+errors[0])
            self.ocr_button.setEnabled(True)
        def failed(error):
            if token == self.ocr_generation:
                self.ocr_status.setText('识别失败：' + error)
                self.ocr_button.setEnabled(True)
        self.submit(lambda: self.ocr.recognize_candidates(path, region, options), done, failed)

    def result_changed(self):
        self.name_edit.setText(remove_whitespace(clean_name(remove_whitespace(self.result.toPlainText()))))
        self.mark_review_override()

    def update_target(self):
        stem = clean_name(self.name_edit.text())
        self.target_label.setText('将命名为：' + stem + '.pdf' if stem else '自动保留 .pdf 后缀')
        self.rename_button.setEnabled(bool(self.current and stem and self.review_can_approve()))

    def rename_current(self):
        if not self.current:
            return
        if self.review_before_rename():
            return
        try:
            record = rename_pdf(self.current, self.name_edit.text())
        except Exception as exc:
            return self.show_error(str(exc))
        self.history.append(record)
        self.review_after_rename(record)
        self.undo_button.setEnabled(True)
        self.ocr_generation += 1
        self.generation += 1
        self.current = record.renamed
        item = self.files.currentItem()
        item.setData(Qt.ItemDataRole.UserRole, record.renamed)
        item.setText(record.renamed.name)
        item.setToolTip(str(record.renamed))
        self.current_label.setText(record.renamed.name)
        self.current_label.setToolTip(str(record.renamed))
        self.filter_list()
        self.statusBar().showMessage(f'已重命名：{record.original.name} → {record.renamed.name}')
        if self.report_mode.isChecked():
            self.refresh_review_badges()
            self.advance_report()
            return
        if self.next_check.isChecked():
            for row in range(self.files.currentRow()+1, self.files.count()):
                if not self.files.item(row).isHidden():
                    self.files.setCurrentRow(row)
                    return
        self.set_result_text('')
        self.name_edit.clear()
        self.clear_candidates()
        self.ocr_button.setEnabled(bool(self.region))
        self.ocr_status.setText('已重命名；可选择其他文件继续')

    def undo(self):
        if not self.history:
            return
        record = self.history[-1]
        try:
            original = undo_rename(record)
        except Exception as exc:
            return self.show_error(str(exc))
        self.review_after_undo(record)
        self.history.pop()
        self.undo_button.setEnabled(bool(self.history))
        for i in range(self.files.count()):
            item = self.files.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == record.renamed:
                item.setText(original.name)
                item.setData(Qt.ItemDataRole.UserRole, original)
                item.setToolTip(str(original))
                if item == self.files.currentItem():
                    self.select_file(item)
        self.filter_list()
        self.statusBar().showMessage('已撤销：' + original.name)
        self.refresh_review_badges()

    def show_error(self, text):
        QMessageBox.warning(self, '操作未完成', text)

    def closeEvent(self, event):
        if self.bulk_running:
            self.finish_bulk_approval(cancelled=True,refresh_preview=False,resume_review=False)
        self.stop_review()
        if self.pool.activeThreadCount():
            self.statusBar().showMessage('正在完成后台任务，请稍后再关闭窗口。')
            event.ignore()
        else:
            event.accept()


def main():
    if len(sys.argv)==4 and sys.argv[1]=='--self-test-multiline':
        from report_diagnostic import run_multiline
        sys.exit(0 if run_multiline(sys.argv[2],sys.argv[3]) else 1)
    if len(sys.argv)==4 and sys.argv[1] in ('--self-test-reports','--self-test-bulk'):
        from report_diagnostic import run
        sys.exit(0 if run(MainWindow,sys.argv[2],sys.argv[3],bulk=sys.argv[1]=='--self-test-bulk') else 1)
    if len(sys.argv) == 4 and sys.argv[1] == '--self-test':
        # Packaged-runtime diagnostic; always work on a temporary copy.
        import hashlib
        import json
        import shutil
        import tempfile
        report = Path(sys.argv[3])
        try:
            with tempfile.TemporaryDirectory(prefix='pdf_rename_check_') as temp:
                source = Path(temp) / 'sample.pdf'
                shutil.copy2(sys.argv[2], source)
                before = hashlib.sha256(source.read_bytes()).hexdigest()
                pix, pages = render_first(source)
                pix.save(str(report.with_suffix('.png')))
                from dataclasses import asdict
                candidates = OcrEngine().recognize_candidates(source,(0,0,1,1),OcrOptions(model='compare',dpi=300))
                if any(c.error for c in candidates):
                    raise RuntimeError(str(candidates))
                text, confidence = candidates[0].text, candidates[0].confidence
                if not text:
                    raise RuntimeError('OCR returned no text')
                record = rename_pdf(source, text)
                assert hashlib.sha256(record.renamed.read_bytes()).hexdigest() == before
                undo_rename(record)
                assert hashlib.sha256(source.read_bytes()).hexdigest() == before
                result = dict(ok=True, pages=pages, text=text, confidence=confidence,
                              renamed=record.renamed.name, sha256=before, undo=True,
                              candidates=[asdict(c) for c in candidates])
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            return
        except Exception as exc:
            report.write_text(json.dumps(dict(ok=False, error=str(exc)), ensure_ascii=False), encoding='utf-8')
            raise
    app = QApplication(sys.argv)
    app.setApplicationName('PDF 框选重命名')
    app.setOrganizationName('LocalTools')
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    icon_path = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'app.ico'
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir():
        QTimer.singleShot(100, lambda: window.load_folder(Path(sys.argv[1])))
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
