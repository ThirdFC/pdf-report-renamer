import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from PySide6.QtCore import Qt,QBuffer,QIODevice
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication,QListWidgetItem
from core import OcrLine,signature
from report_review import ReportTemplate,ReportTemplateStore,ReportFinding,ReportCandidate,extract_report,original_box
from app import MainWindow
from shortcuts import ShortcutStore


def row(label,value,y,shift=0):
    return [OcrLine(label,(100+shift,y,220+shift,y+30),.99),
            OcrLine('HSZQX-ZTJDQ-JO-GJ-'+value,(240+shift,y,1000+shift,y+30),.99)]


@pytest.mark.parametrize('shift',[-60,0,150])
def test_anchor_and_crosscheck_tolerate_position_shifts(shift):
    lines = row('报告编号','20250313-009',120,shift)+row('委托编号','20250313-009',200,shift)+row('记录编号','20250313-009',300,shift)
    finding,_ = extract_report({'v4':lines},ReportTemplate())
    assert finding.number == '20250313-009'
    assert not finding.notices
    assert len(finding.candidates[0].sources) == 3


def test_disagreement_is_flagged_and_primary_report_wins():
    lines = row('报告编号','20250313-009',120)+row('委托编号','20250313-008',200)
    finding,_ = extract_report({'v4':lines},ReportTemplate())
    assert finding.number == '20250313-009'
    assert any('不一致' in message for message in finding.notices)
    assert {c.number for c in finding.candidates} == {'20250313-009','20250313-008'}


def test_dates_and_other_numbers_are_not_report_numbers():
    lines = [OcrLine('报告日期2025-03-14',(0,0,400,30),.99),
             OcrLine('炉号25690016',(0,100,400,130),.99)]
    finding,_ = extract_report({'v4':lines},ReportTemplate())
    assert not finding.number
    assert finding.notices


def test_invalid_date_missing_anchor_and_model_conflict_are_flagged():
    finding,_ = extract_report({'v4':row('其他','20250231-009',20)},ReportTemplate())
    assert finding.number == '20250231-009'
    assert any('日期无效' in x for x in finding.notices)
    assert any('未准确定位' in x for x in finding.notices)
    finding,_ = extract_report({'v4':row('报告编号','20250313-009',20),
                               'v6':row('报告编号','20250313-008',20)},ReportTemplate())
    assert any('两个模型' in x for x in finding.notices)


def test_dash_normalization_and_adjustable_serial_count(tmp_path):
    finding,_ = extract_report({'v4':row('报 告 编 号','２０２５０３１３－００９９',20)},ReportTemplate(serial_digits=4,crosscheck=False))
    assert finding.number == '20250313-0099'
    template = ReportTemplate((.2,.1,.9,.5),'报告编号',4,False)
    store = ReportTemplateStore(tmp_path/'report.json')
    store.save(template)
    assert store.load() == template


def test_review_evidence_maps_back_to_unenhanced_scan():
    assert original_box((116,66,316,116),(300,800,3),(332,832,3),0) == (100,50,300,100)


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    w = MainWindow(ShortcutStore(tmp_path/'settings.json'))
    w.pending = []
    w.submit = lambda fn,done,failed:w.pending.append((fn,done,failed))
    for name in ('a.pdf','b.pdf'):
        path = tmp_path/name
        path.write_bytes(('contents '+name).encode())
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole,path)
        w.files.addItem(item)
    w.files.setCurrentRow(0)
    w.pending.clear()
    w.preview_ready = True
    w.report_mode.setChecked(True)
    w.review_timer.stop()
    yield w
    w.stop_review()
    w.close()
    w.deleteLater()
    app.processEvents()


def finding(path,number='20250313-009'):
    image = QImage(500,90,QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer,'PNG')
    return ReportFinding([ReportCandidate(number,['报告编号 / v4'],.99,(0,0,500,90),bytes(buffer.data()))],
                         file_signature=signature(path))


def test_batch_recognition_never_renames_before_approval(window):
    w = window
    path = w.current
    data = path.read_bytes()
    callback = w.pending[-1][1]
    callback(finding(path))
    w.review_timer.stop()
    assert path.exists() and path.read_bytes() == data
    assert not (path.parent/'20250313-009.pdf').exists()
    assert w.name_edit.text() == '20250313-009'
    assert w.rename_button.isEnabled()
    w.rename_button.click()
    target = path.parent/'20250313-009.pdf'
    assert not path.exists() and target.read_bytes() == data
    assert w.review_states[target] == '已审批'
    assert w.current.name == 'b.pdf'
    assert not w.rename_button.isEnabled()  # Next preview must load before approval.


def test_late_background_result_does_not_replace_manual_edit(window):
    w = window
    callback = w.pending[-1][1]
    path = w.current
    w.name_edit.setText('手工名称')
    w.mark_review_override()
    callback(finding(path))
    w.review_timer.stop()
    assert w.name_edit.text() == '手工名称'


def test_changed_source_cannot_be_approved_from_stale_finding(window):
    w = window
    path = w.current
    w.pending[-1][1](finding(path))
    w.review_timer.stop()
    path.write_bytes(b'changed externally')
    errors = []
    w.show_error = errors.append
    w.rename_current()
    assert errors and path.exists()
    assert not (path.parent/'20250313-009.pdf').exists()


def test_duplicate_target_cannot_be_overwritten(window):
    w = window
    path = w.current
    target = path.parent/'20250313-009.pdf'
    target.write_bytes(b'keep this existing file')
    w.pending[-1][1](finding(path))
    w.review_timer.stop()
    errors = []
    w.show_error = errors.append
    w.rename_current()
    assert errors
    assert path.exists() and target.read_bytes() == b'keep this existing file'
    assert w.review_states.get(path) != '已审批'


def test_skip_has_no_file_side_effects(window):
    w = window
    path = w.current
    before = path.read_bytes()
    w.skip_report()
    assert w.review_states[path] == '已跳过'
    assert path.read_bytes() == before
    assert w.current.name == 'b.pdf'


def test_new_template_invalidates_old_background_result(window):
    w = window
    callback = w.pending[-1][1]
    path = w.current
    w.invalidate_report_results()
    callback(finding(path))
    assert path not in w.review_results
    assert not w.name_edit.text()


def test_undo_restores_file_and_review_state(window):
    w = window
    original = w.current
    w.pending[-1][1](finding(original))
    w.review_timer.stop()
    w.rename_current()
    w.undo()
    assert original.exists()
    assert w.review_states.get(original) != '已审批'
    assert not (original.parent/'20250313-009.pdf').exists()


def test_matching_existing_name_only_marks_approved(window):
    w = window
    path = w.current
    before = path.read_bytes()
    w.name_edit.setText(path.stem)
    w.rename_current()
    assert path.read_bytes() == before
    assert not w.history
    assert w.review_states[path] == '已审批'
    assert w.current.name == 'b.pdf'


def test_approval_blocked_until_preview_ready(window):
    w = window
    path = w.current
    w.preview_ready = False
    w.name_edit.setText('20250313-009')
    w.update_target()
    assert not w.rename_button.isEnabled()
    w.rename_current()
    assert path.exists() and not w.history


def test_one_background_error_does_not_stop_the_queue(window):
    w = window
    path = w.current
    w.pending[-1][2]('测试损坏文件')
    w.review_timer.stop()
    assert w.review_results[path].error
    w.review_pump()
    assert w.review_active.name == 'b.pdf'


def ready_for_bulk(w):
    w.stop_review()
    w.review_active = None
    w.review_pending.clear()
    for index,path in enumerate(w.report_paths()):
        w.review_results[path] = finding(path,f'20250313-{index+9:03}')
    w.apply_report_result()
    w.refresh_review_badges()


def finish_bulk(w):
    while w.bulk_running:
        w.bulk_timer.stop()
        w.bulk_approve_step()


def test_bulk_approval_renames_all_ready_and_remains_undoable(window):
    w = window
    ready_for_bulk(w)
    originals = {p:p.read_bytes() for p in w.report_paths()}
    assert not w.history
    w.bulk_approve_button.click()
    w.bulk_timer.stop()
    assert w.bulk_running and all(p.exists() for p in originals)
    finish_bulk(w)
    assert w.bulk_result['renamed'] == 2 and not w.bulk_result['failures']
    assert len(w.history)==2
    for index,(path,data) in enumerate(originals.items()):
        target = path.with_name(f'20250313-{index+9:03}.pdf')
        assert target.read_bytes()==data and w.review_states[target]=='已审批'
    w.undo()
    w.undo()
    assert all(p.read_bytes()==data for p,data in originals.items())


@pytest.mark.parametrize('excluded', ['need_check','skipped','manual'])
def test_bulk_leaves_non_pending_rows_untouched(window, excluded):
    w = window
    ready_for_bulk(w)
    other = w.report_paths()[1]
    if excluded=='need_check':
        w.review_results[other].notices.append('编号不一致')
    elif excluded=='skipped':
        w.review_states[other]='已跳过'
    else:
        w.review_overrides.add(other)
    w.refresh_review_badges()
    assert len(w.bulk_approval_plan())==1
    w.start_bulk_approval()
    finish_bulk(w)
    assert other.exists() and w.bulk_result['renamed']==1


def test_bulk_includes_hidden_pending_but_not_future_results(window):
    w = window
    ready_for_bulk(w)
    other = w.report_paths()[1]
    w.files.item(1).setHidden(True)
    assert len(w.bulk_approval_plan())==2
    later = w.review_results.pop(other)
    w.start_bulk_approval()
    w.bulk_timer.stop()
    w.review_results[other]=later
    finish_bulk(w)
    assert w.bulk_result['total']==1 and other.exists()
    assert w.review_states.get(other)!='已审批'


def test_bulk_approves_ready_files_hidden_by_search(window):
    w = window
    ready_for_bulk(w)
    directory=w.current.parent
    w.search.setText('a.pdf')
    assert w.files.item(1).isHidden()
    w.start_bulk_approval()
    finish_bulk(w)
    assert w.bulk_result['renamed']==2
    assert (directory/'20250313-010.pdf').exists()


def test_manual_pending_flag_survives_reopening_cached_result(window):
    w = window
    ready_for_bulk(w)
    path=w.current
    w.review_overrides.add(path)
    w.apply_report_result()
    assert path in w.review_overrides
    assert all(entry[0]!=path for entry in w.bulk_approval_plan())


@pytest.mark.parametrize('failure', ['changed','target_exists'])
def test_bulk_failure_keeps_file_and_continues(window, failure):
    w = window
    ready_for_bulk(w)
    first,second = w.report_paths()
    w.start_bulk_approval()
    w.bulk_timer.stop()
    if failure=='changed':
        first.write_bytes(b'changed externally')
    else:
        first.with_name('20250313-009.pdf').write_bytes(b'existing target')
    finish_bulk(w)
    assert first.exists()
    assert second.with_name('20250313-010.pdf').exists()
    assert len(w.bulk_result['failures'])==1 and w.bulk_result['renamed']==1
    assert w.review_states.get(first)!='已审批'
    assert first in w.review_batch_errors
    if failure=='target_exists':
        assert first.with_name('20250313-009.pdf').read_bytes()==b'existing target'


def test_bulk_duplicate_candidates_are_not_pending(window):
    w = window
    ready_for_bulk(w)
    first,second = w.report_paths()
    w.review_results[second]=finding(second,'20250313-009')
    assert not w.bulk_approval_plan()
    w.start_bulk_approval()
    assert not w.bulk_running and not w.history and first.exists() and second.exists()


def test_bulk_cancel_does_not_process_remaining_files(window):
    w = window
    ready_for_bulk(w)
    first,second = w.report_paths()
    w.start_bulk_approval()
    w.bulk_timer.stop()
    w.bulk_approve_step()
    w.bulk_approve_clicked()
    w.bulk_approve_step()  # An already queued timer callback cannot continue after stopping.
    assert not w.bulk_running and w.bulk_result['cancelled']
    assert w.bulk_result['remaining']==1
    assert second.exists() and first.with_name('20250313-009.pdf').exists()
    assert len(w.history)==1


def test_bulk_existing_correct_name_only_marks_approved(window):
    w = window
    ready_for_bulk(w)
    path = w.report_paths()[0]
    w.review_results[path]=finding(path,'a')
    w.review_states[w.report_paths()[1]]='已跳过'
    before=path.read_bytes()
    w.start_bulk_approval()
    finish_bulk(w)
    assert w.bulk_result['matched']==1 and w.bulk_result['renamed']==0
    assert not w.history and path.read_bytes()==before and w.review_states[path]=='已审批'
