"""Packaged report-review smoke test; only modifies temporary sample copies."""
import os
import json
import time
import shutil
import tempfile
import hashlib
from pathlib import Path


def run(window_class,source,report):
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtWidgets import QApplication
    from shortcuts import ShortcutStore
    app = QApplication.instance() or QApplication([])
    window = None
    def wait_for(predicate):
        deadline = time.monotonic()+120
        while not predicate():
            if time.monotonic()>deadline:
                raise RuntimeError('后台预识别超时')
            app.processEvents()
            time.sleep(.02)
    try:
        with tempfile.TemporaryDirectory(prefix='report_review_diagnostic_') as temp:
            folder = Path(temp)/'pdfs'
            shutil.copytree(source,folder)
            before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.pdf')}
            if len(before)!=5:
                raise ValueError('请传入随软件提供的 report_samples 五份测试报告')
            window = window_class(ShortcutStore(Path(temp)/'settings.json'))
            window.show()
            window.load_folder(folder)
            wait_for(lambda:window.files.count()==5 and window.preview_ready)
            window.report_mode.setChecked(True)
            wait_for(lambda:len(window.review_results)==5 and not window.jobs)
            assert not window.history
            assert all(f.number and not f.error for f in window.review_results.values())
            assert {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.pdf')} == before
            assert window.name_edit.text()=='20250313-009'
            window.rename_button.click()
            wait_for(lambda:window.current.name.startswith('02-') and window.preview_ready)
            assert (folder/'20250313-009.pdf').exists() and len(window.history)==1
            assert window.name_edit.text()=='20250313-010'
            window.undo_button.click()
            wait_for(lambda:not window.jobs)
            assert not window.history
            assert {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.pdf')} == before
            payload = dict(ok=True,preidentified=5,first='20250313-009',second='20250313-010',
                           no_rename_before_approval=True,undo=True,bytes_preserved=True)
            window.stop_review()
            window.close()
        Path(report).write_text(json.dumps(payload,indent=2),encoding='utf-8')
        return True
    except Exception as exc:
        if window:
            window.stop_review()
        Path(report).write_text(json.dumps(dict(ok=False,error=str(exc)),ensure_ascii=False,indent=2),encoding='utf-8')
        return False
