"""Windows 入口。只允许一个实例使用同一数据目录。"""
import argparse
import multiprocessing
import sys
import tempfile
from pathlib import Path
from core.config import load_config
from core.store import Store
from core.sites import read_sites, site_config, prepare_site


def instance_lock(root):
    import msvcrt
    folder = Path(root) / 'data'
    folder.mkdir(exist_ok=True, parents=True)
    handle = (folder / 'instance.lock').open('a+b')
    handle.seek(0)
    if not handle.read(1):
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError('助手已在运行；同一数据目录不能启动两个实例')
    return handle


def demo_data(store):
    store.set('account', {'id': 'demo', 'name': '演示账号（非真实学习记录）'})
    store.set('course_count', 2)
    store.set('course_catalog:demo', [
        dict(classroom_id='1', name='大学英语', class_name='2026 秋 · 英语教学班', start=1787155200, end=1801411199),
        dict(classroom_id='2', name='计算机基础', class_name='2026 春 · 计算机教学班', start=1772294400, end=1785427199)])
    store.select_courses(['1', '2'], 'demo')
    store.set('scanned_account', 'demo')
    store.set('scanned_courses', ['1', '2'])
    store.set('worker_status', '演示模式')
    for cid, course, lid, title, kind, state, progress in [
        ('1', '大学英语', '1', 'Unit 1 · Listening', 'video', 'pending', .42),
        ('1', '大学英语', '2', '课程导读', 'article', 'completed', 1),
        ('2', '计算机基础', '3', '计算机组成概述 · PPT', 'slides', 'pending', .35),
        ('2', '计算机基础', '4', '章节练习', 'manual', 'manual', None)]:
        task = dict(key=f'demo:{cid}:{lid}', classroom_id=cid, leaf_id=lid, course=course, title=title,
                    kind=kind, leaf_type={'video': 0, 'article': 3, 'slides': 7, 'manual': 6}[kind], state=state,
                    progress=progress, deadline=None, duration=1800 if kind == 'video' else None,
                    note='仅供界面演示', evidence='demo' if state == 'completed' else '')
        # 每个 demo 课程一次性合并，避免覆盖同课程的其他任务。
        existing = [dict(t) for t in store.rows('demo') if t['classroom_id'] == cid]
        for t in existing:
            t.pop('account', None)
        store.merge_course('demo', cid, [*existing, task])


def main():
    from gui.window_frame import icon_path, set_app_id
    set_app_id()
    parser = argparse.ArgumentParser(description='雨课堂智能任务管理助手')
    parser.add_argument('--demo', action='store_true', help='使用临时演示数据，不连接雨课堂')
    parser.add_argument('--self-check', action='store_true', help='检查配置和依赖，不打开账号')
    parser.add_argument('--classic', action='store_true', help='使用经典界面进行兼容性排查')
    parser.add_argument('--opaque', action='store_true', help='关闭桌面透明效果，使用内置背景')
    args = parser.parse_args()
    config = load_config()
    if args.self_check:
        import playwright, psutil, tkinter, PIL, webview
        print('配置、Playwright、psutil、Tkinter、Pillow、pywebview 导入成功。实站登录及任务完成仍需单独验收。')
        return
    demo_dir = tempfile.TemporaryDirectory(prefix='rainclassroom-demo-') if args.demo else None
    if demo_dir:
        config['root'] = demo_dir.name
    lock = instance_lock(config['root'])
    import tkinter as tk
    from recovery.watchdog import Watchdog
    base_config = config
    if not args.demo:
        config = site_config(base_config, read_sites(base_config)[0])
    store = Store(config['root'])
    store.set('current', {})
    store.recover()
    watchdog = None
    if args.demo:
        demo_data(store)
    else:
        # 启动先读取课程时间，用户确认选课后才扫描章节。崩溃恢复保留原命令。
        store = prepare_site(config)
        watchdog = Watchdog(config)
        watchdog.start()
    ui = None
    try:
        if args.classic:
            from gui.interface import Interface
            root = tk.Tk()
            if icon_path():
                root.iconbitmap(str(icon_path()))
            ui = Interface(root, store, watchdog, args.demo, base_config=base_config)
            root.mainloop()
        else:
            import webview
            from gui.web_interface import WebInterface
            ui = WebInterface(store, watchdog, args.demo, base_config=base_config)
            from gui.web_assets import load_document
            html = load_document()
            # Only the explicit title-bar region may move the window. Inputs,
            # course cards and traffic-light controls remain ordinary controls.
            webview.settings['DRAG_REGION_DIRECT_TARGET_ONLY'] = True
            window = webview.create_window('雨课堂 · 智能任务管理助手' + (' · 演示' if args.demo else ''),
                html=html, width=1280, height=850, min_size=(900, 660), background_color='#e7eaf4',
                frameless=True, easy_drag=False, resizable=True, shadow=False)
            ui.bind_window(window)
            ui.window_frame.desktop_glass_enabled = not args.opaque
            # Expose only two narrow endpoints, rather than every public controller method.
            window.expose(ui.snapshot, ui.action)
            window.events.closing += ui.on_closing
            webview.start(gui='edgechromium', private_mode=True,
                          icon=str(icon_path()) if icon_path() else None)
    finally:
        active_watchdog = ui.watchdog if ui else watchdog
        if active_watchdog:
            active_watchdog.stop()
            active_watchdog.join(timeout=15)
        lock.close()
        if demo_dir:
            demo_dir.cleanup()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    try:
        main()
    except Exception as exc:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        from gui.window_frame import icon_path
        if icon_path():
            root.iconbitmap(str(icon_path()))
        messagebox.showerror('雨课堂助手启动失败', str(exc))
        root.destroy()
        sys.exit(1)
