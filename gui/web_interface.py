"""Thread-safe desktop bridge for the local WebView2 interface.

Only ``snapshot`` and ``action`` should be exposed to JavaScript. The worker
continues to own browser automation and server completion decisions; this
module presents a small, explicitly whitelisted view of local state.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from core.config import app_root
from core.courses import course_state, date_text
from core.sites import PRESETS, normalize_site, prepare_site, read_sites, save_site, site_config
from core.task_types import AUTOMATED_KINDS, type_label


STATES = {'pending': '待处理', 'running': '进行中', 'completed': '服务器已确认',
          'manual': '需手动完成', 'locked': '未开放', 'needs_adapter': '需要适配', 'failed': '失败'}
ACTIVE = frozenset(('run', 'scan', 'catalog', 'login'))


def _text(value, limit=700):
    """Display diagnostics without carrying credentials or signed URLs."""
    text = str(value or '')[:limit]
    text = re.sub(r'(?i)\b(cookie|authorization|session|access_token|refresh_token|course_sign)\s*[:=]\s*[^\s,;]+',
                  r'\1=[已隐藏]', text)
    return re.sub(r'(https?://[^\s?#]+)[?#][^\s]*', r'\1', text)


def _number(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError):
        return default


def _site_label(url):
    host = urlsplit(url).hostname or '雨课堂'
    return {'www.yuketang.cn': '雨课堂', 'changjiang.yuketang.cn': '长江雨课堂'}.get(host, host)


class WebInterface:
    """Small bridge with short serialized actions and asynchronous lifecycle.

    WebView calls can arrive concurrently on separate threads. A lock protects
    site identity and user commands. Stopping/joining a watchdog happens outside
    that lock so a snapshot, pause, or close can still finish promptly.
    """
    def __init__(self, store, watchdog=None, demo=False, base_config=None, watchdog_factory=None):
        self.store, self.watchdog, self.demo = store, watchdog, demo
        self.base_config, self.watchdog_factory = base_config, watchdog_factory
        self.active_site, self.history = (read_sites(base_config) if base_config
                                         else (PRESETS[0], list(PRESETS)))
        self.window = None
        self.switching = self.closing = self.closed = False
        self._lock = threading.RLock()
        self._lifecycle_thread = None
        self._notice = ''
        self._notice_until = 0.0
        self._changed = time.time()
        self._filters = {'task_type': 'all', 'unfinished_only': True}
        version = app_root() / 'VERSION.txt'
        self.version = version.read_text(encoding='utf8').strip() if version.exists() else ''

    def bind_window(self, window):
        self.window = window

    def _notify(self, message):
        self._notice, self._notice_until = _text(message), time.monotonic() + 6
        self._changed = time.time()
        return {'ok': True, 'message': self._notice}

    def _alive(self):
        return self.watchdog is None or self.watchdog.is_alive()

    def _busy(self):
        return (self.switching or self.closing or self.store.get('worker_busy', False)
                or self.store.get('desired', 'idle') in ACTIVE)

    def _primary(self, desired, worker_busy, account, courses, selected, tasks):
        if self.closing:
            return {'label': '正在保存…', 'action': 'none', 'enabled': False}
        if self.switching:
            return {'label': '切换中…', 'action': 'none', 'enabled': False}
        if not self.demo and not self._alive():
            return {'label': '恢复连接', 'action': 'recover', 'enabled': True}
        if desired in ACTIVE:
            return {'label': '暂停等待' if desired == 'login' else '暂停', 'action': 'pause', 'enabled': True}
        if worker_busy:
            return {'label': '正在暂停…', 'action': 'none', 'enabled': False}
        if not account.get('id') or not courses:
            return {'label': '连接课程', 'action': 'load_courses', 'enabled': True}
        if not selected:
            return {'label': '选择课程', 'action': 'choose_courses', 'enabled': True}
        runnable = any(t['kind'] in AUTOMATED_KINDS and t['state'] in ('pending', 'running') for t in tasks)
        if not runnable and any(t['state'] == 'failed' and t['kind'] in AUTOMATED_KINDS for t in tasks):
            return {'label': '重试失败任务', 'action': 'retry_failed', 'enabled': True}
        return {'label': '继续' if desired == 'paused' else '开始学习', 'action': 'start', 'enabled': True}

    def snapshot(self):
        """Read one SQLite transaction, then return display fields only.

        Avoid opening a connection for every course/task on every poll. Account
        identity, selected scope, rows, and events share the same read snapshot.
        Raw meta data and task evidence never leave this method.
        """
        with self._lock:
            store = self.store
            with store.db() as db:
                db.execute('BEGIN')
                meta = {row['key']: json.loads(row['value']) for row in db.execute('SELECT key,value FROM meta')}
                account = meta.get('account') or {}
                account_id = str(account.get('id') or '')
                courses = meta.get('course_catalog:' + account_id) or []
                selected = set(str(cid) for cid in (meta.get('selected_courses:' + account_id) or []))
                rows = [dict(row) for row in db.execute('SELECT * FROM tasks WHERE account=? AND active=1 '
                        'ORDER BY CASE WHEN deadline>0 THEN deadline ELSE 99999999999 END, '
                        'CASE kind WHEN "article" THEN 0 WHEN "video" THEN 1 ELSE 2 END, course, leaf_id', (account_id,))
                        if str(row['classroom_id']) in selected]
                events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 80')]
            desired = meta.get('desired', 'idle')
            worker_busy = bool(meta.get('worker_busy', False))
            scan = meta.get('scan_progress') or {}
            workflow = meta.get('workflow') or {}
            current = meta.get('current') or {}
            scan_status = scan.get('status')
            scanning = desired in ('catalog', 'scan', 'run') and scan_status in ('queued', 'running')
            phase = ('closing' if self.closing else 'switching' if self.switching else 'error' if not self._alive()
                     else 'login_required' if desired == 'login' else 'loading_courses' if desired == 'catalog'
                     else 'scanning' if scanning or desired == 'scan' else 'running' if desired == 'run'
                     else 'pausing' if worker_busy else 'error' if workflow.get('phase') == 'error'
                     else 'paused' if desired == 'paused' else 'idle')
            message = workflow.get('message') or meta.get('worker_status') or '选好课程，即可开始'
            if self.closing:
                message = '正在保存状态并关闭受管理的 Edge…'
            elif self.switching:
                message = '正在安全退出原站点，随后连接新站点…'
            elif not self._alive():
                message = '浏览器守护已停止，点击“恢复连接”重启；选课和历史记录会保留'
            elif desired == 'login':
                message = workflow.get('message') or '请在独立 Edge 窗口登录，完成后会自动继续'
            elif desired in ('idle', 'paused') and scan_status in ('failed', 'partial'):
                message = scan.get('message') or '部分课程未扫描成功，请查看异常后重试'
            elif desired in ('idle', 'paused') and not selected:
                message = '请选择需要处理的课程，选择会自动保存'
            elif desired in ('idle', 'paused') and not (meta.get('scanned_account') == account_id
                    and meta.get('scanned_courses') == sorted(selected)) and phase != 'error':
                message = '课程选择已保存，点击开始会自动扫描并处理'
            if self.demo:
                message = '演示模式 · 虚构数据，可体验选课、筛选和操作反馈'
            if self._notice and time.monotonic() < self._notice_until:
                message = self._notice
            counts = Counter(t['state'] for t in rows)
            error_rows = meta.get('scan_errors') or []
            scope_scanned = bool(selected) and meta.get('scanned_account') == account_id and meta.get('scanned_courses') == sorted(selected)
            if not account_id or not courses:
                course_empty = '课程正在等待连接。在独立 Edge 中登录后，课程会自动出现。'
            else:
                course_empty = '没有匹配的课程，请更换搜索词。'
            if not selected:
                task_empty = '先选择要处理的课程，然后直接点击开始学习。'
            elif scan_status in ('failed', 'partial'):
                task_empty = '课程扫描未成功完成，请查看扫描异常并重试。空列表不代表任务已完成。'
            elif not scope_scanned:
                task_empty = '选课已保存。点击开始学习，会先自动扫描所选课程。'
            elif rows:
                task_empty = '当前筛选没有匹配任务，可修改类型或关闭“仅未完成”。'
            else:
                task_empty = '所选课程未发现任务，请结合扫描记录确认课程内容。'
            total = int(_number(scan.get('total'), 0)) if scanning else len(rows)
            complete = int(_number(scan.get('completed'), 0)) if scanning else counts['completed']
            total, complete = max(0, total), max(0, complete)
            public_courses = []
            task_counts = Counter(t['classroom_id'] for t in rows)
            for course in sorted(courses, key=lambda c: (c.get('start') or 0, str(c['classroom_id'])), reverse=True):
                cid = str(course['classroom_id'])
                state = course_state(course)
                public_courses.append({'id': cid, 'name': _text(course.get('name'), 160),
                    'class_name': _text(course.get('class_name') or course.get('term'), 180),
                    'start_date': date_text(course.get('start')), 'end_date': date_text(course.get('end')),
                    'status': state, 'current': state == '进行中', 'selected': cid in selected,
                    'task_count': task_counts[cid], 'message': _text(course.get('date_error'), 250)})
            public_tasks = [{'id': t['key'], 'course_id': t['classroom_id'], 'course_name': _text(t['course'], 160),
                'title': _text(t['title'], 240), 'type': t['kind'], 'type_label': type_label(t),
                'status': t['state'], 'status_label': STATES.get(t['state'], t['state']),
                'progress': _number(t['progress']), 'duration': _number(t['duration']),
                'message': _text(t['note']), 'attempts': t['attempts']} for t in rows]
            public_current = {key: _text(current[key]) for key in ('course', 'title', 'phase') if key in current}
            if 'key' in current:
                public_current['id'] = str(current['key'])
            for key in ('server_progress', 'currentTime', 'current_time', 'duration', 'playbackRate',
                        'playback_rate', 'page', 'pages', 'read_pages', 'total_pages',
                        'pages_read', 'pages_total', 'pages_visited', 'reading_round'):
                if key in current:
                    public_current[key] = _number(current[key])
            public_errors = [{'course': _text(e.get('course'), 160), 'message': _text(e.get('reason') or e.get('message'))}
                             for e in error_rows]
            public_logs = [{'time': time.strftime('%m-%d %H:%M:%S', time.localtime(e['time'])),
                'level': _text(e.get('status'), 100), 'course': _text(e.get('course'), 160),
                'task': _text(e.get('task'), 240), 'message': _text(e.get('reason'))} for e in events]
            busy = self.switching or self.closing or worker_busy or desired in ACTIVE
            return {'version': self.version, 'demo': self.demo,
                'site': {'url': self.active_site, 'label': _site_label(self.active_site),
                         'history': [{'url': url, 'label': _site_label(url)} for url in self.history]},
                'account': {'name': _text(account.get('name') or '等待登录', 100),
                            'logged_in': bool(account_id) and desired != 'login'},
                'state': {'phase': phase, 'busy': busy, 'selection_locked': busy, 'message': _text(message),
                          'primary': self._primary(desired, worker_busy, account, courses, selected, rows)},
                'counts': {'courses': len(courses), 'selected': len(selected), 'tasks': len(rows),
                           'completed': counts['completed'], 'pending': counts['pending'] + counts['running'],
                           'errors': len(public_errors), 'failed': counts['failed'],
                           'manual': counts['manual'], 'needs_adapter': counts['needs_adapter']},
                'progress': {'current': complete, 'total': total,
                             'percent': min(100, 100 * complete / total) if total else 0,
                             'indeterminate': scanning and not total,
                             'label': _text(scan.get('phase') if scanning else message)},
                'courses': public_courses, 'tasks': public_tasks, 'logs': public_logs,
                'scan_errors': public_errors, 'current': public_current,
                'workflow': {key: _text(workflow[key]) for key in ('phase', 'message') if key in workflow},
                'filters': dict(self._filters), 'empty': {'courses': course_empty, 'tasks': task_empty},
                'updated_at': max([self._changed, _number(workflow.get('updated'), 0),
                                   *[e['time'] for e in events], *[t['updated'] for t in rows]])}

    def action(self, name, payload=None):
        """Validate small commands; never expose arbitrary Store writes."""
        with self._lock:
            if not isinstance(name, str) or (payload is not None and not isinstance(payload, dict)):
                return {'ok': False, 'message': '操作参数无效，请重试'}
            payload = payload or {}
            if self.closing or self.closed:
                return {'ok': False, 'message': '程序正在关闭，状态会自动保存'}
            if self.switching:
                return {'ok': False, 'message': '正在切换站点，请稍候'}
            try:
                return self._action(name, payload)
            except ValueError as exc:
                return {'ok': False, 'message': _text(exc)}
            except Exception as exc:
                # Exception messages may contain profile paths or raw HTTP
                # details. Expose a short category and keep diagnostics local.
                self.store.event('界面操作失败', type(exc).__name__)
                return {'ok': False, 'message': f'操作暂未成功（{type(exc).__name__}），请查看运行记录后重试'}

    def _action(self, name, payload):
        if name == 'primary':
            desired = self.store.get('desired', 'idle')
            name = self._primary(desired, self.store.get('worker_busy', False), self.store.get('account', {}),
                                 self.store.catalog(), self.store.selected_courses(), self.store.scoped_rows())['action']
        if name == 'choose_courses':
            return {**self._notify('先勾选需要处理的课程，再点击开始学习'), 'navigate': 'courses'}
        if name == 'none':
            return {'ok': False, 'message': '当前操作尚未停止，请稍候'}
        if name == 'task_filter':
            allowed = {'all', 'video', 'article', 'slides', 'manual', 'failed', 'needs_adapter', 'homework', 'exam', 'quiz'}
            kind = payload.get('type', 'all')
            if kind not in allowed or not isinstance(payload.get('unfinished_only', True), bool):
                raise ValueError('筛选参数无效')
            self._filters = {'task_type': kind, 'unfinished_only': payload.get('unfinished_only', True)}
            return self._notify('任务筛选已更新')
        if name == 'switch_site':
            return self._begin_switch(payload.get('url', ''))
        if name == 'recover':
            return self._begin_switch(self.active_site, recover=True)
        if name == 'open_logs':
            folder = Path(self.store.path).parents[1] / 'logs'
            folder.mkdir(exist_ok=True)
            os.startfile(str(folder))
            return self._notify('已打开当前站点的日志目录')
        if name == 'open_browser':
            return self._show_browser()
        if name in ('pause', 'paused'):
            if self.store.get('desired') == 'login':
                self.store.set('login_resume', None)
            scan = self.store.get('scan_progress', {})
            if scan.get('status') == 'queued':
                self.store.set('scan_progress', {**scan, 'status': 'cancelled', 'message': '扫描已暂停'})
            self.store.set('desired', 'paused')
            return self._notify('正在暂停，状态会自动保存')
        if self._busy():
            raise ValueError('请先暂停并等待当前操作停止，再修改课程或重新扫描')
        if name in ('select_course', 'course_toggle'):
            cid = str(payload.get('id') or '')
            if cid not in {str(c['classroom_id']) for c in self.store.catalog()}:
                raise ValueError('这门课程已不在当前账号目录中，请刷新课程')
            selected = set(self.store.selected_courses())
            if 'selected' not in payload:
                selected.symmetric_difference_update({cid})
            elif not isinstance(payload['selected'], bool):
                raise ValueError('课程选择参数无效')
            elif payload['selected']:
                selected.add(cid)
            else:
                selected.discard(cid)
            self.store.select_courses(selected)
            return self._notify('课程选择已保存，开始时会自动扫描')
        if name in ('select_courses', 'course_group'):
            mode = payload.get('mode', '')
            if mode not in ('current', 'all', 'clear', 'none'):
                raise ValueError('课程选择方式无效')
            selected = [c['classroom_id'] for c in self.store.catalog()
                        if mode == 'all' or (mode == 'current' and course_state(c) == '进行中')]
            self.store.select_courses(selected)
            return self._notify('课程选择已保存，开始时会自动扫描')
        if name in ('retry', 'retry_tasks', 'retry_failed'):
            tasks = self.store.scoped_rows()
            ids = [t['key'] for t in tasks if t['state'] == 'failed'] if name == 'retry_failed' else payload.get('ids', [])
            if not isinstance(ids, list) or not ids or not all(isinstance(key, str) for key in ids):
                raise ValueError('请选择失败任务后重试')
            chosen = [t for t in tasks if t['key'] in set(ids)]
            if len(chosen) != len(set(ids)) or any(t['state'] != 'failed' for t in chosen):
                raise ValueError('只能重试当前所选课程中的失败任务；需要适配的任务请重新扫描')
            for task in chosen:
                self.store.update(task['key'], state='pending', attempts=0, note='用户已重置重试预算')
            if name != 'retry_failed':
                return self._notify(f'已将 {len(chosen)} 个失败任务加入队列，点击开始学习继续')
            name = 'start'
        if name in ('start', 'run', 'resume', 'scan', 'load_courses', 'catalog'):
            desired = 'catalog' if name in ('load_courses', 'catalog') else 'scan' if name == 'scan' else 'run'
            if not self.demo and not self._alive():
                raise ValueError('浏览器守护已停止，请先点击“恢复连接”')
            if desired in ('scan', 'run') and not self.store.selected_courses():
                return {**self._notify('请先选择需要处理的课程'), 'navigate': 'courses', 'ok': False}
            if self.demo and desired != 'run':
                return self._notify('演示模式只展示虚构数据，不会连接真实网站')
            if desired in ('scan', 'catalog'):
                self.store.set('scan_progress', {'status': 'queued', 'mode': 'catalog' if desired == 'catalog' else 'tasks',
                    'phase': '正在读取课程' if desired == 'catalog' else '正在扫描所选课程',
                    'total': 0, 'completed': 0, 'succeeded': 0, 'failed': 0, 'tasks': 0, 'started': time.time(), 'current': []})
            else:
                self.store.set('run_mode', 'resume' if self.store.get('desired') == 'paused' or name == 'resume' else 'start')
            self.store.set('desired', desired)
            return {**self._notify('正在连接课程' if desired == 'catalog' else '正在扫描所选课程' if desired == 'scan'
                                 else '已开始，会自动扫描所选课程并处理任务'),
                    'navigate': 'courses' if desired == 'catalog' else 'tasks'}
        raise ValueError('未知操作，请刷新界面后重试')

    def _begin_switch(self, value, recover=False):
        if self.demo or not self.base_config:
            raise ValueError('演示模式不切换真实网站')
        if not isinstance(value, str):
            raise ValueError('请填写雨课堂网站主页地址')
        url = normalize_site(value)
        config = site_config(self.base_config, url)
        if url == self.active_site and self._alive() and not recover:
            return self._notify('当前已经是这个站点')
        self.switching = True
        self._notify('正在保存当前状态并切换站点')
        old_dog, old_store, old_url = self.watchdog, self.store, self.active_site
        self._lifecycle_thread = threading.Thread(target=self._finish_switch,
            args=(url, config, old_dog, old_store, old_url), daemon=True, name='RainClassroomSiteSwitch')
        self._lifecycle_thread.start()
        return {'ok': True, 'message': self._notice, 'navigate': 'courses'}

    def _finish_switch(self, url, config, old_dog, old_store, old_url):
        dog = None
        try:
            if old_dog:
                old_dog.stop()
                while old_dog.is_alive():
                    old_dog.join(timeout=.2)
            with self._lock:
                if self.closing:
                    self.switching = False
                    return
                from recovery.watchdog import Watchdog
                new_store = prepare_site(config)
                dog = (self.watchdog_factory or Watchdog)(config)
                # Publish the new identity only once the old worker has exited.
                # Start failures restore the original selection and do not leave
                # a worker running against an undisclosed site.
                dog.start()
                save_site(self.base_config, url)
                history = read_sites(self.base_config)[1]
                self.watchdog, self.store, self.active_site = dog, new_store, url
                self.history = history
                self._filters = {'task_type': 'all', 'unfinished_only': True}
                self.switching = False
                self._notify('站点已切换，登录和任务记录分别保存')
        except Exception as exc:
            # A newly launched worker must exit before restoring the old view.
            # Cleanup may take seconds and therefore cannot hold the API lock.
            if dog:
                dog.stop()
                while dog.is_alive():
                    dog.join(timeout=.2)
            with self._lock:
                self.store, self.active_site, self.watchdog = old_store, old_url, old_dog
                old_store.set('desired', 'paused')
                old_store.set('worker_busy', False)
                old_store.event('站点切换失败', type(exc).__name__)
                self.switching = False
                self._notify(f'站点切换失败（{type(exc).__name__}），请检查目录权限后恢复连接')

    def _show_browser(self):
        """Raise only an Edge window owned by this app, never a fresh profile."""
        if self.demo:
            return self._notify('演示模式不打开真实网站')
        if not self._alive() or not self.watchdog:
            raise ValueError('浏览器尚未连接，请先点击恢复连接')
        import ctypes
        from ctypes import wintypes
        import psutil
        pids = set()
        for pid, created in dict(getattr(self.watchdog, 'owned', {})).items():
            try:
                proc = psutil.Process(pid)
                if abs(proc.create_time() - created) < .01 and proc.name().casefold() == 'msedge.exe':
                    pids.add(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
        windows = []
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                windows.append(hwnd)
            return True
        user32.EnumWindows(callback_type(visit), 0)
        if not windows:
            raise ValueError('当前没有可显示的受管理 Edge 窗口，请检查浏览器是否仍在启动或配置为后台模式')
        user32.ShowWindow(windows[0], 9)
        user32.SetForegroundWindow(windows[0])
        return self._notify('已显示受管理的 Edge 窗口')

    def on_closing(self, *args):
        """Cancel the immediate native close until owned browser cleanup ends."""
        if self.closed:
            return True
        self.close()
        return False

    def close(self):
        with self._lock:
            if self.closing or self.closed:
                return
            self.closing = True
            thread = threading.Thread(target=self._finish_close, daemon=True, name='RainClassroomClose')
            thread.start()

    def _finish_close(self):
        # A site-switch thread may already be joining the old worker. Waiting
        # outside the command lock avoids a close/switch deadlock.
        switch = self._lifecycle_thread
        if switch and switch is not threading.current_thread():
            switch.join()
        with self._lock:
            dog = self.watchdog
        if dog:
            dog.stop()
            while dog.is_alive():
                dog.join(timeout=.2)
        with self._lock:
            self.closed = True
            window = self.window
        if window:
            window.destroy()
