"""Background report review queue; only explicit approval renames files."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt,QTimer,Signal
from PySide6.QtGui import QColor,QPixmap,QImage
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QCheckBox,QDialog,QFormLayout,QLineEdit,QSpinBox,QFrame)

from core import clean_name,signature
from report_review import ReportTemplateStore,ReportFinding,analyze_report


class EvidenceLabel(QLabel):
    clicked = Signal()
    def __init__(self):
        super().__init__('识别完成后显示编号截图')
        self.original = QPixmap()
        self.setMinimumHeight(70)
        self.setMaximumHeight(90)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet('background: white; border: 1px solid #c6d6ed; border-radius: 6px;')
        self.setToolTip('点击放大编号截图')
        self.setAccessibleName('报告编号原图截图')

    def show_png(self,data):
        self.original = QPixmap.fromImage(QImage.fromData(data)) if data else QPixmap()
        self.refresh()

    def refresh(self):
        if self.original.isNull():
            self.clear()
            self.setText('暂无编号截图，请查看中间预览')
        else:
            self.setPixmap(self.original.scaled(max(30,self.width()-10),max(30,self.height()-10),
                                                Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.refresh()

    def mouseReleaseEvent(self,event):
        if event.button() == Qt.MouseButton.LeftButton and not self.original.isNull():
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ReportRulesDialog(QDialog):
    def __init__(self,parent,template):
        super().__init__(parent)
        self.setWindowTitle('报告编号定位规则')
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        note = QLabel('先在第一页框一个较大的搜索范围，覆盖报告编号及附近的委托/记录编号。\n编号会自动定位，允许扫描位置有偏移。')
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.anchor = QLineEdit(template.anchor)
        self.digits = QSpinBox()
        self.digits.setRange(1,6)
        self.digits.setValue(template.serial_digits)
        form.addRow('定位文字',self.anchor)
        form.addRow('日期后流水号位数',self.digits)
        layout.addLayout(form)
        self.crosscheck = QCheckBox('用同页委托编号、记录编号交叉核对')
        self.crosscheck.setChecked(template.crosscheck)
        layout.addWidget(self.crosscheck)
        layout.addWidget(QLabel('例如：20250313-009 = 8 位日期 + 横线 + 3 位流水号'))
        buttons = QHBoxLayout()
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        save = QPushButton('保存规则')
        save.setObjectName('primary')
        save.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)


class ReportReviewMixin:
    def init_report_state(self):
        self.report_store = ReportTemplateStore(self.shortcut_store.path.with_name('report.json'))
        self.report_template = self.report_store.load()
        self.review_results = {}
        self.review_states = {}
        self.review_overrides = set()
        self.review_pending = []
        self.review_active = None
        self.review_running = False
        self.review_epoch = 0
        self.preview_ready = False
        self.review_preview_signature = None
        self._result_programmatic = False
        self.review_timer = QTimer(self)
        self.review_timer.setSingleShot(True)
        self.review_timer.timeout.connect(self.review_pump)

    def add_report_mode_control(self,layout):
        self.report_mode = QCheckBox('报告编号审批模式')
        self.report_mode.setToolTip('自动生成编号候选，逐份确认后才重命名')
        self.report_mode.toggled.connect(self.toggle_report_mode)
        layout.addWidget(self.report_mode)

    def build_report_panel(self,layout,combo_class):
        self.report_panel = QWidget()
        self.report_panel.setStyleSheet('QPushButton { padding: 5px 7px; }')
        panel = QVBoxLayout(self.report_panel)
        panel.setContentsMargins(0,0,0,0)
        first = QHBoxLayout()
        self.report_region_button = QPushButton('框选搜索范围')
        self.report_region_button.setCheckable(True)
        self.report_region_button.clicked.connect(self.choose_report_region)
        rules = QPushButton('定位规则')
        rules.clicked.connect(self.edit_report_rules)
        first.addWidget(self.report_region_button,1)
        first.addWidget(rules)
        panel.addLayout(first)
        second = QHBoxLayout()
        start = QPushButton('预识别')
        start.clicked.connect(self.start_review)
        stop = QPushButton('暂停')
        stop.clicked.connect(self.stop_review)
        retry = QPushButton('重试本份')
        retry.clicked.connect(self.retry_report)
        for button in (start,stop,retry):
            second.addWidget(button)
        panel.addLayout(second)
        self.review_progress = QLabel('选择文件夹后可开始预识别')
        self.review_progress.setWordWrap(True)
        panel.addWidget(self.review_progress)
        self.review_status = QLabel('默认搜索第一页右上区域；可重新框选较大范围')
        self.review_status.setWordWrap(True)
        self.review_status.setMaximumHeight(60)
        panel.addWidget(self.review_status)
        self.report_candidates = combo_class()
        self.report_candidates.setAccessibleName('报告编号候选')
        self.report_candidates.currentIndexChanged.connect(self.choose_report_candidate)
        panel.addWidget(self.report_candidates)
        self.report_evidence = EvidenceLabel()
        self.report_evidence.clicked.connect(self.enlarge_report_evidence)
        panel.addWidget(self.report_evidence)
        extra = QHBoxLayout()
        self.manual_report_button = QPushButton('手动框选 / OCR 选项')
        self.manual_report_button.setCheckable(True)
        self.manual_report_button.toggled.connect(lambda checked:self.manual_ocr_panel.setVisible(checked or not self.report_mode.isChecked()))
        skip = QPushButton('跳过本份')
        skip.clicked.connect(self.skip_report)
        extra.addWidget(self.manual_report_button,1)
        extra.addWidget(skip)
        panel.addLayout(extra)
        layout.addWidget(self.report_panel)
        self.report_panel.hide()

    def report_paths(self):
        return [self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())]

    def toggle_report_mode(self,enabled):
        self.report_region_button.setChecked(False)
        if self.view.box:
            self.view.canvas.removeItem(self.view.box)
            self.view.box = None
        self.report_panel.setVisible(enabled)
        self.manual_ocr_panel.setVisible(not enabled or self.manual_report_button.isChecked())
        self.next_check.setVisible(not enabled)
        self.preservation_note.setVisible(not enabled)
        self.set_result_text('')
        self.name_edit.clear()
        self.region = None
        self.ocr_button.setEnabled(False)
        if enabled:
            self.start_review()
            if self.preview_ready:
                self.apply_report_result()
        else:
            self.stop_review()
        self.refresh_review_badges()
        self.update_review_approve_label()
        self.update_target()

    def update_review_approve_label(self):
        if not hasattr(self,'report_mode'):
            return
        from shortcuts import display_key
        title = '确认通过并重命名' if self.report_mode.isChecked() else '确认重命名'
        key = display_key(self.shortcut_values['rename'])
        self.rename_button.setText(title+(f'  {key}' if key else ''))

    def reset_report_folder(self):
        self.review_epoch += 1
        self.review_running = False
        self.review_timer.stop()
        self.review_results.clear()
        self.review_states.clear()
        self.review_overrides.clear()
        self.review_pending.clear()
        self.review_active = None

    def invalidate_report_results(self):
        was_running = self.review_running
        self.review_epoch += 1
        self.review_timer.stop()
        self.review_active = None
        self.review_pending.clear()
        self.review_results.clear()
        self.review_overrides.clear()
        self.report_evidence.show_png(b'')
        self.report_candidates.clear()
        self.set_result_text('')
        self.name_edit.clear()
        if self.report_mode.isChecked() and was_running:
            self.start_review()
        self.refresh_review_badges()

    def start_review(self):
        if not self.report_mode.isChecked():
            return
        self.review_running = True
        for path in self.report_paths():
            if path not in self.review_results and path not in self.review_pending and path != self.review_active:
                if self.review_states.get(path) not in ('已审批','已跳过'):
                    self.review_pending.append(path)
        if self.current in self.review_pending:
            self.review_pending.remove(self.current)
            self.review_pending.insert(0,self.current)
        self.refresh_review_badges()
        self.review_pump()

    def stop_review(self):
        self.review_running = False
        self.review_timer.stop()
        self.refresh_review_badges()

    def review_pump(self):
        if not self.review_running or self.review_active is not None or not self.report_mode.isChecked():
            return
        paths = set(self.report_paths())
        while self.review_pending:
            path = self.review_pending.pop(0)
            if path in paths and path not in self.review_results and self.review_states.get(path) not in ('已审批','已跳过'):
                break
        else:
            self.refresh_review_badges()
            return
        self.review_active = path
        epoch,template,options = self.review_epoch,self.report_template,self.ocr_options
        self.refresh_review_badges()
        def done(finding):
            if epoch != self.review_epoch:
                return
            self.review_active = None
            self.review_results[path] = finding
            if self.current == path and self.preview_ready and path not in self.review_overrides:
                self.apply_report_result()
            self.refresh_review_badges()
            if self.review_running:
                self.review_timer.start(50)
        def failed(error):
            done(ReportFinding(error=error))
        self.submit(lambda:analyze_report(self.ocr,path,template,options),done,failed)

    def report_selected(self):
        self.preview_ready = False
        self.report_evidence.show_png(b'')
        self.report_candidates.blockSignals(True)
        self.report_candidates.clear()
        self.report_candidates.blockSignals(False)
        self.review_preview_signature = None
        try:
            if self.current:
                self.review_preview_signature = signature(self.current)
        except OSError:
            pass
        if self.report_mode.isChecked():
            self.review_status.setText('正在加载预览…')
            if self.current in self.review_pending:
                self.review_pending.remove(self.current)
                self.review_pending.insert(0,self.current)
            self.review_pump()

    def report_preview_ready(self):
        self.preview_ready = True
        if self.report_mode.isChecked():
            self.apply_report_result()

    def finding_issues(self,path,finding):
        issues = list(finding.notices)
        if finding.error:
            return ['识别失败：'+finding.error]
        if finding.number:
            others = [p for p,f in self.review_results.items()
                      if p != path and p.parent == path.parent and f.number == finding.number]
            if others:
                issues.append('同目录多个文件得到相同候选编号')
            target = path.with_name(finding.number+'.pdf')
            if target.exists() and str(target).casefold() != str(path).casefold():
                issues.append('目标文件名已存在，不会覆盖')
        return list(dict.fromkeys(issues))

    def refresh_review_badges(self):
        if not hasattr(self,'report_mode'):
            return
        enabled = self.report_mode.isChecked()
        for i in range(self.files.count()):
            item = self.files.item(i)
            path = item.data(Qt.ItemDataRole.UserRole)
            if not enabled:
                item.setText(path.name)
                item.setForeground(QColor('#24344b'))
                continue
            finding = self.review_results.get(path)
            state = self.review_states.get(path)
            color = '#6d7d92'
            if state in ('已审批','已跳过'):
                detail = state
                color = '#16815c' if state == '已审批' else '#6d7d92'
            elif finding:
                issues = self.finding_issues(path,finding)
                detail = ('需检查' if issues else '待审批')+(' · '+finding.number if finding.number else '')
                color = '#a76505' if issues else '#2165b8'
            else:
                detail = '正在识别' if path == self.review_active else '等待预识别'
            item.setText(path.name+'\n'+detail)
            item.setForeground(QColor(color))
        total = self.files.count()
        complete = sum(p in self.review_results for p in self.report_paths())
        approved = sum(self.review_states.get(p)=='已审批' for p in self.report_paths())
        skipped = sum(self.review_states.get(p)=='已跳过' for p in self.report_paths())
        mode = '预识别中' if self.review_running and (self.review_active or self.review_pending) else ('已暂停' if not self.review_running else '预识别完成')
        self.review_progress.setText(f'{mode} {complete}/{total} · 审批 {approved} · 跳过 {skipped}')
        if enabled and self.current in self.review_results and self.preview_ready and self.current not in self.review_overrides:
            finding = self.review_results[self.current]
            issues = self.finding_issues(self.current,finding)
            state = self.review_states.get(self.current)
            self.review_status.setText((state+' · ' if state else '')+('需检查：'+'；'.join(issues) if issues else '待审批 · 已定位报告编号并通过当前核对'))
            self.review_status.setStyleSheet('color: #a76505;' if issues else 'color: #18634d;')
            self.review_status.setToolTip('\n'.join(issues)+'\n'+finding.raw_text)

    def apply_report_result(self):
        if not self.report_mode.isChecked() or not self.current or not self.preview_ready:
            return
        state = self.review_states.get(self.current)
        finding = self.review_results.get(self.current)
        if not finding:
            self.review_status.setText(state or ('正在识别本份…' if self.current == self.review_active else '等待预识别，可点“预识别”继续'))
            self.update_target()
            return
        try:
            if finding.file_signature is not None and signature(self.current) != finding.file_signature:
                self.review_status.setText('文件已变更，请点“重试本份”')
                self.set_result_text('')
                return
        except OSError as exc:
            self.review_status.setText(str(exc))
            self.set_result_text('')
            return
        self.report_candidates.blockSignals(True)
        self.report_candidates.clear()
        for candidate in finding.candidates:
            self.report_candidates.addItem(candidate.number+'（'+candidate.sources[0].split(' / ')[0]+'）',candidate)
        self.report_candidates.blockSignals(False)
        self.review_overrides.discard(self.current)
        self.show_report_candidate(0)
        if not finding.candidates:
            self.report_evidence.show_png(finding.fallback_evidence)
            self.set_result_text('')
        issues = self.finding_issues(self.current,finding)
        self.review_status.setText((state+' · ' if state else '')+('需检查：'+'；'.join(issues) if issues else '待审批 · 已定位报告编号并通过当前核对'))
        self.review_status.setStyleSheet('color: #a76505;' if issues else 'color: #18634d;')
        self.review_status.setToolTip('\n'.join(issues)+'\n'+finding.raw_text)
        self.update_target()

    def show_report_candidate(self,index):
        if index < 0:
            return
        candidate = self.report_candidates.itemData(index)
        if candidate:
            self.report_evidence.show_png(candidate.evidence)
            self.set_result_text(candidate.number)

    def choose_report_candidate(self,index):
        if index < 0:
            return
        self.show_report_candidate(index)
        if self.current:
            self.review_overrides.add(self.current)

    def mark_review_override(self,*args):
        if self.report_mode.isChecked() and self.current and not self._result_programmatic:
            self.review_overrides.add(self.current)
            self.review_status.setText('已手动修改候选，请核对编号截图或中间预览后确认')

    def set_result_text(self,text):
        previous = self._result_programmatic
        self._result_programmatic = True
        try:
            self.result.setPlainText(text)
        finally:
            self._result_programmatic = previous

    def handle_report_region(self,region):
        if not self.report_mode.isChecked():
            return False
        if self.report_region_button.isChecked():
            self.report_template = replace(self.report_template,region=tuple(region))
            self.report_region_button.setChecked(False)
            self.save_report_template()
            self.invalidate_report_results()
            self.start_review()
            self.review_status.setText('搜索范围已更新，正在重新预识别')
            return True
        self.review_overrides.add(self.current)
        self.manual_report_button.setChecked(True)
        self.report_evidence.show_png(b'')
        self.review_status.setText('已切换为本份手动框选，请核对预览后确认')
        return False

    def choose_report_region(self,checked):
        if checked:
            self.review_status.setText('请在中间预览框一个较大范围，包含“报告编号”及右侧编号；可同时包含委托/记录编号')
        self.update_target()

    def edit_report_rules(self):
        dialog = ReportRulesDialog(self,self.report_template)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.report_template = replace(self.report_template,anchor=dialog.anchor.text().strip(),
                        serial_digits=dialog.digits.value(),crosscheck=dialog.crosscheck.isChecked())
            except ValueError as exc:
                self.show_error(str(exc))
                return
            self.save_report_template()
            self.invalidate_report_results()
            self.start_review()

    def save_report_template(self):
        try:
            self.report_store.save(self.report_template)
        except OSError as exc:
            self.statusBar().showMessage('规则本次已生效，但无法保存：'+str(exc))

    def retry_report(self):
        if not self.current:
            return
        if self.current == self.review_active:
            return
        self.review_results.pop(self.current,None)
        self.review_states.pop(self.current,None)
        self.review_overrides.discard(self.current)
        self.set_result_text('')
        self.report_evidence.show_png(b'')
        self.start_review()

    def review_can_approve(self):
        return (not self.report_mode.isChecked() or
                (self.preview_ready and not self.report_region_button.isChecked()
                 and self.review_states.get(self.current) not in ('已审批','已跳过')))

    def review_before_rename(self):
        if not self.report_mode.isChecked():
            return False
        if not self.review_can_approve():
            return True
        finding = self.review_results.get(self.current)
        expected = finding.file_signature if finding and finding.file_signature else self.review_preview_signature
        try:
            if expected is not None and signature(self.current) != expected:
                self.show_error('文件已变更，请点“重试本份”并重新核对预览。')
                return True
        except OSError as exc:
            self.show_error(str(exc))
            return True
        stem = clean_name(self.name_edit.text())
        if stem and (stem+'.pdf').casefold() == self.current.name.casefold():
            self.review_states[self.current] = '已审批'
            self.refresh_review_badges()
            self.advance_report()
            return True
        return False

    def review_after_rename(self,record):
        if not self.report_mode.isChecked():
            return
        finding = self.review_results.pop(record.original,None)
        if finding:
            self.review_results[record.renamed] = finding
        self.review_states.pop(record.original,None)
        self.review_states[record.renamed] = '已审批'
        self.review_overrides.discard(record.original)
        self.review_pending = [p for p in self.review_pending if p != record.original]

    def review_after_undo(self,record):
        finding = self.review_results.pop(record.renamed,None)
        if finding:
            self.review_results[record.original] = finding
        self.review_states.pop(record.renamed,None)
        self.review_states.pop(record.original,None)

    def advance_report(self):
        start = self.files.currentRow()+1
        for i in list(range(start,self.files.count()))+list(range(0,start)):
            item = self.files.item(i)
            path = item.data(Qt.ItemDataRole.UserRole)
            if not item.isHidden() and self.review_states.get(path) not in ('已审批','已跳过'):
                self.files.setCurrentRow(i)
                return
        self.review_status.setText('当前可见文件审批完成，可修改搜索条件查看其他文件')
        self.update_target()

    def skip_report(self):
        if self.current and self.report_mode.isChecked():
            self.review_states[self.current] = '已跳过'
            self.refresh_review_badges()
            self.advance_report()

    def enlarge_report_evidence(self):
        if self.report_evidence.original.isNull():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('编号截图核对')
        layout = QVBoxLayout(dialog)
        image = QLabel()
        image.setPixmap(self.report_evidence.original.scaled(1100,500,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(image)
        close = QPushButton('关闭')
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()
