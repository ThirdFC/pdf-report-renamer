"""Background report review queue; only explicit approval renames files."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt,QTimer,Signal
from PySide6.QtGui import QColor,QPixmap,QImage
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QCheckBox,QDialog,QFormLayout,QLineEdit,QSpinBox,QFrame)

from core import clean_name,signature,rename_pdf
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
        self.bulk_running = False
        self.bulk_queue = []
        self.bulk_result = None
        self.review_batch_errors = {}
        self.bulk_timer = QTimer(self)
        self.bulk_timer.setSingleShot(True)
        self.bulk_timer.timeout.connect(self.bulk_approve_step)
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
        rules = self.report_rules_button = QPushButton('定位规则')
        rules.clicked.connect(self.edit_report_rules)
        first.addWidget(self.report_region_button,1)
        first.addWidget(rules)
        panel.addLayout(first)
        second = QHBoxLayout()
        start = self.report_start_button = QPushButton('预识别')
        start.clicked.connect(self.start_review)
        stop = self.report_pause_button = QPushButton('暂停')
        stop.clicked.connect(self.stop_review)
        retry = self.report_retry_button = QPushButton('重试本份')
        retry.clicked.connect(self.retry_report)
        for button in (start,stop,retry):
            second.addWidget(button)
        panel.addLayout(second)
        self.bulk_approve_button = QPushButton('一键通过所有待审批（0）')
        self.bulk_approve_button.setEnabled(False)
        self.bulk_approve_button.setToolTip('处理点击时已识别完成、无检查提示且未手动修改的所有待审批文件（包含筛选隐藏项）。需检查、已跳过和仍在识别的文件不处理。')
        self.bulk_approve_button.clicked.connect(self.bulk_approve_clicked)
        panel.addWidget(self.bulk_approve_button)
        self.bulk_summary = QLabel()
        self.bulk_summary.setWordWrap(True)
        self.bulk_summary.hide()
        panel.addWidget(self.bulk_summary)
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
        skip = self.report_skip_button = QPushButton('跳过本份')
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
        self.review_batch_errors.clear()
        self.bulk_summary.hide()

    def invalidate_report_results(self):
        was_running = self.review_running
        self.review_epoch += 1
        self.review_timer.stop()
        self.review_active = None
        self.review_pending.clear()
        self.review_results.clear()
        self.review_overrides.clear()
        self.review_batch_errors.clear()
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
        if path in self.review_batch_errors:
            issues.append('批量通过失败：'+self.review_batch_errors[path])
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
            elif path in self.review_overrides:
                detail = '手动待确认'
                color = '#a76505'
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
        if not self.bulk_running:
            ready = len(self.bulk_approval_plan()) if enabled else 0
            self.bulk_approve_button.setText(f'一键通过所有待审批（{ready}）')
            self.bulk_approve_button.setEnabled(enabled and ready>0 and not self.report_region_button.isChecked())
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
            self.refresh_review_badges()

    def mark_review_override(self,*args):
        if self.report_mode.isChecked() and self.current and not self._result_programmatic:
            self.review_overrides.add(self.current)
            self.review_status.setText('已手动修改候选，请核对编号截图或中间预览后确认')
            self.refresh_review_badges()

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
        self.refresh_review_badges()
        return False

    def choose_report_region(self,checked):
        if checked:
            self.review_status.setText('请在中间预览框一个较大范围，包含“报告编号”及右侧编号；可同时包含委托/记录编号')
        self.update_target()
        self.refresh_review_badges()

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
        self.review_batch_errors.pop(self.current,None)
        self.set_result_text('')
        self.report_evidence.show_png(b'')
        self.start_review()

    def review_can_approve(self):
        return not self.bulk_running and (not self.report_mode.isChecked() or
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
        self.review_batch_errors.pop(record.original,None)
        self.review_pending = [p for p in self.review_pending if p != record.original]

    def review_after_undo(self,record):
        finding = self.review_results.pop(record.renamed,None)
        if finding:
            self.review_results[record.original] = finding
        self.review_states.pop(record.renamed,None)
        self.review_states.pop(record.original,None)
        self.review_batch_errors.pop(record.renamed,None)

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

    def bulk_approval_plan(self):
        plan = []
        for path in self.report_paths():
            finding = self.review_results.get(path)
            if (self.review_states.get(path) in ('已审批','已跳过') or path in self.review_overrides
                    or not finding or finding.error or not finding.number or finding.file_signature is None):
                continue
            if self.finding_issues(path,finding):
                continue
            plan.append((path,finding.number,finding.file_signature))
        return plan

    def bulk_approve_clicked(self):
        if self.bulk_running:
            self.finish_bulk_approval(cancelled=True)
        else:
            self.start_bulk_approval()

    def start_bulk_approval(self):
        if not self.report_mode.isChecked() or self.bulk_running or self.report_region_button.isChecked():
            return
        plan = self.bulk_approval_plan()
        if not plan:
            self.bulk_summary.setText('当前没有可批量通过的待审批文件。')
            self.bulk_summary.show()
            return
        self.bulk_queue = list(plan)
        self.bulk_result = dict(total=len(plan),renamed=0,matched=0,failures=[],cancelled=False,remaining=0)
        self.bulk_resume_review = self.review_running
        self.bulk_running = True
        self.stop_review()
        self.generation += 1
        self.ocr_generation += 1
        self.bulk_summary.setText(f'正在批量通过 {len(plan)} 份文件…')
        self.bulk_summary.setToolTip('')
        self.bulk_summary.show()
        names = ('report_mode','choose_button','settings_button','report_region_button','report_rules_button',
                 'report_start_button','report_pause_button','report_retry_button','manual_report_button','report_skip_button',
                 'files','search','recursive','previous_button','next_button','name_edit','result','report_candidates',
                 'model_combo','dpi_combo','enhancement_combo','deskew_check','drop_symbol_check','undo_button','rename_button')
        self.bulk_controls = [(getattr(self,name),getattr(self,name).isEnabled()) for name in names]
        for control,_ in self.bulk_controls:
            control.setEnabled(False)
        self.bulk_approve_button.setEnabled(True)
        self.bulk_approve_button.setText(f'停止批量通过（0/{len(plan)}）')
        self.bulk_timer.start(0)

    def bulk_approve_step(self):
        if not self.bulk_running:
            return
        if not self.bulk_queue:
            self.finish_bulk_approval()
            return
        path,number,expected = self.bulk_queue.pop(0)
        try:
            item = next((self.files.item(i) for i in range(self.files.count())
                         if self.files.item(i).data(Qt.ItemDataRole.UserRole)==path),None)
            if item is None:
                raise ValueError('文件已不在当前列表中')
            if signature(path) != expected:
                raise ValueError('文件已变更，请重试本份并重新核对')
            if self.review_states.get(path) in ('已审批','已跳过'):
                raise ValueError('文件审批状态已变化')
            if (number+'.pdf').casefold() == path.name.casefold():
                self.review_states[path] = '已审批'
                self.bulk_result['matched'] += 1
            else:
                record = rename_pdf(path,number)
                self.history.append(record)
                self.review_after_rename(record)
                item.setData(Qt.ItemDataRole.UserRole,record.renamed)
                item.setText(record.renamed.name)
                item.setToolTip(str(record.renamed))
                if self.current == path:
                    self.current = record.renamed
                    self.current_label.setText(record.renamed.name)
                    self.current_label.setToolTip(str(record.renamed))
                self.bulk_result['renamed'] += 1
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            self.bulk_result['failures'].append(dict(file=str(path),reason=reason))
            self.review_batch_errors[path] = reason
        done = self.bulk_result['total']-len(self.bulk_queue)
        self.bulk_approve_button.setText(f'停止批量通过（{done}/{self.bulk_result["total"]}）')
        self.refresh_review_badges()
        self.bulk_timer.start(0)

    def finish_bulk_approval(self,cancelled=False,refresh_preview=True,resume_review=True):
        if not self.bulk_running:
            return
        self.bulk_timer.stop()
        self.bulk_result['cancelled'] = cancelled
        self.bulk_result['remaining'] = len(self.bulk_queue)
        self.bulk_queue.clear()
        self.bulk_running = False
        for control,enabled in self.bulk_controls:
            control.setEnabled(enabled)
        self.undo_button.setEnabled(bool(self.history))
        result = self.bulk_result
        summary = ('已停止批量通过' if cancelled else '批量通过完成')+f'：改名 {result["renamed"]}，原名匹配 {result["matched"]}，失败 {len(result["failures"])}'
        if cancelled:
            summary += f'，未处理 {result["remaining"]}'
        self.bulk_summary.setText(summary)
        self.bulk_summary.setToolTip('\n'.join(f'{entry["file"]}：{entry["reason"]}' for entry in result['failures']))
        self.statusBar().showMessage(summary)
        if refresh_preview and self.files.currentItem():
            self.select_file(self.files.currentItem())
        self.filter_list()
        self.refresh_review_badges()
        self.update_target()
        if resume_review and self.bulk_resume_review:
            self.start_review()
