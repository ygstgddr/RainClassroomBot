import tkinter as tk
from core.store import Store
from main import demo_data
from gui.interface import Interface


def test_gui_lists_tasks_and_controls(tmp_path):
    store = Store(tmp_path)
    demo_data(store)
    root = tk.Tk()
    root.withdraw()
    try:
        ui = Interface(root, store)
        root.update()
        assert len(ui.tree.get_children()) == 3
        assert '演示账号' in ui.summary.cget('text')
        ui.command('run')
        assert store.get('desired') == 'run'
        ui.command('paused')
        assert store.get('desired') == 'paused'
        ui.command('scan')
        assert store.get('desired') == 'scan'
        assert store.get('scan_progress')['status'] == 'queued'
        assert ui.progress_animating
        ui.command('paused')
        assert store.get('scan_progress')['status'] == 'cancelled'
        assert not ui.progress_animating
        ui.command('scan')
        store.set('scan_progress', {'status': 'running', 'total': 36, 'completed': 9,
                                   'tasks': 12, 'failed': 1, 'phase': '扫描课程'})
        ui.render_scan_progress()
        assert float(ui.scan_bar['value']) == 9
        assert float(ui.scan_bar['maximum']) == 36
        assert '25%' in ui.scan_label.cget('text')
        assert 'disabled' in ui.buttons['重新扫描'].state()
        store.set('scan_progress', {'status': 'failed', 'total': 36, 'completed': 36,
                                   'message': '扫描失败，请查看异常'})
        store.set('desired', 'idle')
        ui.render_scan_progress()
        assert ui.scan_label.cget('text') == '扫描失败，请查看异常'
        assert 'disabled' not in ui.buttons['重新扫描'].state()
        ui.only_unfinished.set(False)
        ui.refresh()
        assert len(ui.tree.get_children()) == 4
    finally:
        root.destroy()
