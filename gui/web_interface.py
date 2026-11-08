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
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from core.config import app_root
from core.courses import course_state, date_text
from core.sites import PRESETS, normalize_site, prepare_site, read_sites, save_site, site_config
from core.task_types import AUTOMATED_KINDS, type_label
from core.updater import UpdateManager
from gui.assessment_panel import AssessmentPanel
from gui.window_frame import WindowFrame


STATES = {'pending': '待处理', 'running': '进行中', 'completed': '服务器已确认',
          'manual': '需手动完成', 'locked': '未开放', 'needs_adapter': '需要适配', 'failed': '失败'}
ACTIVE = frozenset(('run', 'scan', 'catalog', 'login', 'assessment_scan', 'assessment_open'))
TYPING_ACTIVE = frozenset(('selecting', 'ready', 'running', 'paused'))
TYPING_PHASES = TYPING_ACTIVE | frozenset(('idle', 'completed', 'stopped', 'error'))


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
    def __init__(self, store, watchdog=None, demo=False, base_config=None, watchdog_factory=None, defer_login=False):
        self.store, self.watchdog, self.demo = store, watchdog, demo
        self.base_config, self.watchdog_factory = base_config, watchdog_factory
        self.active_site, self.history = (read_sites(base_config) if base_config
                                         else (PRESETS[0], list(PRESETS)))
        self.window = None
        self.window_frame = None
        self.embedded_browser = None
        self.typing_window = None
        self.switching = self.closing = self.closed = False
        self._lock = threading.RLock()
        self._lifecycle_thread = None
        self._notice = ''
        self._notice_until = 0.0
        self._changed = time.time()
        self._filters = {'task_type': 'all', 'unfinished_only': True}
        self._typing_pending = None
        self.assessment = AssessmentPanel(self)
        self._deferred_login = bool(defer_login and not demo)
        self._login_gate = self._deferred_login
        self._login_session = None
        self._login_error = ''
        version = app_root() / 'VERSION.txt'
        self.version = version.read_text(encoding='utf8').strip() if version.exists() else ''
        self.updater = UpdateManager(self.version, app_root(), install_supported=False if demo else None)
        self._update_install = False

    def bind_window(self, window, embedded=False, separate_typing=False):
        self.window = window
        self.window_frame = WindowFrame(window)
        if separate_typing:
            from gui.typing_window import TypingWindow
            self.typing_window = TypingWindow(self)
        if embedded:
            from gui.embedded_browser import EmbeddedBrowser
            self.embedded_browser = EmbeddedBrowser(window, self.window_frame._invoke)

    def _browser_view(self, desired, worker_busy, typing, media):
        if not self.embedded_browser:
            return {'enabled': False}
        interactive = not (self.switching or self.closing) and (
            desired == 'login' or (desired in ('idle', 'paused') and not worker_busy))
        self.embedded_browser.allow_input(interactive)
        return {**self.embedded_browser.snapshot(), 'enabled': True,
                'login_surface': bool(self._login_gate and self._login_session and (self.switching or self.watchdog)),
                'can_navigate': interactive and not typing['pending'] and typing['phase'] not in TYPING_ACTIVE,
                'can_pause': not self._login_gate and desired in ('catalog', 'scan', 'run', 'assessment_scan', 'assessment_open'),
                'media': media}

    def _notify(self, message):
        self._notice, self._notice_until = _text(message), time.monotonic() + 6
        self._changed = time.time()
        return {'ok': True, 'message': self._notice}

    def _alive(self):
        return self.watchdog is None or self.watchdog.is_alive()

    def _busy(self):
        return (self.switching or self.closing or self.store.get('worker_busy', False)
                or self.store.get('desired', 'idle') in ACTIVE or self._typing_locked() or self.assessment.owns_browser)

    def _typing_locked(self):
        state = self.store.get('typing_status', {})
        return state.get('phase') in TYPING_ACTIVE or self._pending_typing(state)

    def _pending_typing(self, state):
        pending = self._typing_pending
        if pending and (state.get('request_id') == pending['request_id']
                        or state.get('generation') != pending['generation'] or not self._alive()):
            self._typing_pending = None
        return bool(self._typing_pending)

    def _typing_view(self, state, desired, worker_busy):
        """Display-only typing state; never return the user's source text."""
        state = state if isinstance(state, dict) else {}
        phase = state.get('phase') if state.get('phase') in TYPING_PHASES else 'idle'
        pending = self._pending_typing(state)
        target = state.get('target')
        public_target = None
        if isinstance(target, dict) and isinstance(target.get('token'), str):
            public_target = {key: _text(target.get(key), 250) for key in
                ('token', 'label', 'url', 'frame', 'tag', 'type')}
            public_target['characters'] = max(0, int(_number(target.get('characters'), 0)))
            public_target['multiline'] = bool(target.get('multiline', target.get('tag') != 'input'))
        available = self.demo or bool(self._alive() and self.watchdog
            and callable(getattr(self.watchdog, 'submit_typing', None)) and state.get('connected'))
        quiet = not (self.switching or self.closing or worker_busy or desired in ACTIVE)
        enabled = available and quiet
        if pending and phase not in TYPING_ACTIVE and phase != 'completed':
            phase = 'selecting'  # 命令已提交但 worker 尚未应答时也锁住学习导航。
        return {'phase': phase, 'available': available, 'connected': available, 'target': public_target,
            'sent': max(0, int(_number(state.get('sent'), 0))),
            'total': max(0, int(_number(state.get('total'), 0))),
            'speed': min(60, max(1, _number(state.get('speed'), 20))),
            'message': _text(state.get('message') or '先指定网页输入框，再确认开始打字'),
            'pending': pending, 'learning_busy': not quiet,
            'can_select': enabled and not pending and phase not in ('running', 'paused'),
            'can_start': enabled and not pending and phase in ('ready', 'completed') and bool(public_target),
            'can_pause': enabled and phase == 'running',
            'can_resume': enabled and not pending and phase == 'paused' and bool(public_target),
            'can_stop': available and (phase in TYPING_ACTIVE or bool(public_target) or pending)}

    def _primary(self, desired, worker_busy, account, courses, selected, tasks):
        if self.closing:
            return {'label': '正在保存…', 'action': 'none', 'enabled': False}
        if self.switching:
            return {'label': '切换中…', 'action': 'none', 'enabled': False}
        if self._login_gate:
            return {'label': '登录', 'action': 'login', 'enabled': True}
        if not self.demo and not self._alive():
            return {'label': '恢复连接', 'action': 'recover', 'enabled': True}
        if desired in ACTIVE:
            return {'label': '暂停等待' if desired == 'login' else '暂停', 'action': 'pause', 'enabled': True}
        if worker_busy:
            return {'label': '正在暂停…', 'action': 'none', 'enabled': False}
        if self._typing_locked():
            return {'label': '请先结束手动打字', 'action': 'none', 'enabled': False}
        if self.assessment.owns_browser:
            return {'label': '答题 Agent 正在使用浏览器', 'action': 'none', 'enabled': False}
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
            if self._deferred_login:
                # Each browser launch has its own nonce, so old database account
                # records cannot dismiss the first-run screen.
                if desired == 'login':
                    self._login_gate = True
                elif (self._login_session and account_id and self.watchdog and self._alive()
                      and meta.get('authenticated_session') == self._login_session):
                    self._login_gate = False
            worker_busy = bool(meta.get('worker_busy', False))
            typing = self._typing_view(meta.get('typing_status') or {}, desired, worker_busy)
            scan = meta.get('scan_progress') or {}
            workflow = meta.get('workflow') or {}
            current = meta.get('current') or {}
            scan_status = scan.get('status')
            scanning = desired in ('catalog', 'scan', 'run') and scan_status in ('queued', 'running')
            phase = ('closing' if self.closing else 'switching' if self.switching else 'error' if not self._alive()
                     else 'login_required' if desired == 'login' else 'loading_courses' if desired == 'catalog'
                     else 'scanning' if scanning or desired in ('scan', 'assessment_scan') else 'running' if desired in ('run', 'assessment_open')
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
            elif desired == 'assessment_scan':
                message = '正在读取作业、测验和考试清单'
            elif desired == 'assessment_open':
                message = '正在打开已确认的任务'
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
                'message': _text(t['note']), 'attempts': t['attempts'],
                'execution_group': 'automatic' if t['kind'] in AUTOMATED_KINDS and t['state'] not in ('manual', 'needs_adapter') else 'manual',
                'selectable': t['kind'] in AUTOMATED_KINDS and t['state'] in ('pending', 'failed'),
                'hidden_until_open': t['state'] == 'locked' and t['leaf_type'] == 6} for t in rows]
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
            busy = (self.switching or self.closing or worker_busy or desired in ACTIVE
                    or typing['phase'] in TYPING_ACTIVE or typing['pending'] or self.assessment.owns_browser)
            login_status, login_message = 'idle', '点击登录后打开 Edge，已有登录状态会自动复用'
            if self._login_gate:
                if self._login_error or (self.watchdog and not self._alive()):
                    login_status, login_message = 'error', self._login_error or '浏览器连接已停止，请重新登录'
                elif self.switching or (self.watchdog and desired != 'login' and workflow.get('phase') != 'error'):
                    login_status, login_message = 'connecting', '正在打开 Edge 并检查登录状态…'
                elif self.watchdog and desired == 'login':
                    login_status, login_message = 'waiting', message
                elif self.watchdog and workflow.get('phase') == 'error':
                    login_status, login_message = 'error', message
                phase, message = 'awaiting_login', login_message
            selection_locked = (self.switching or self.closing or login_status == 'connecting') if self._login_gate else busy
            return {'version': self.version, 'demo': self.demo,
                'development': not getattr(sys, 'frozen', False) and not self.demo,
                'browser': self._browser_view(desired, worker_busy, typing, meta.get('browser_media', {})),
                'assessment': self.assessment.snapshot(meta.get('assessment_inventory') or {}, account_id, desired,
                    bool(self.embedded_browser and self.watchdog and self._alive() and not self._login_gate and not self.switching and not self.closing),
                    meta.get('assessment_entry')),
                'update': dict(self.updater.snapshot(), can_install=not busy and not self.demo),
                'window': (self.window_frame.snapshot() if self.window_frame
                           else {'maximized': False, 'native_controls': False}),
                'site': {'url': self.active_site, 'label': _site_label(self.active_site),
                         'history': [{'url': url, 'label': _site_label(url)} for url in self.history]},
                'account': {'name': _text(account.get('name') or '等待登录', 100),
                            'logged_in': bool(account_id) and desired != 'login' and not self._login_gate},
                'state': {'phase': phase, 'busy': busy, 'selection_locked': selection_locked, 'message': _text(message),
                          'login_gate': self._login_gate, 'login_status': login_status,
                          'login_message': _text(login_message),
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
                'task_scope': meta.get('task_scope'),
                'scan_errors': public_errors, 'current': public_current,
                'workflow': {key: _text(workflow[key]) for key in ('phase', 'message') if key in workflow},
                'filters': dict(self._filters), 'empty': {'courses': course_empty, 'tasks': task_empty},
                'typing': typing,
                'separate_typing': self.typing_window is not None,
                'updated_at': max([self._changed, _number(workflow.get('updated'), 0),
                                   *[e['time'] for e in events], *[t['updated'] for t in rows]])}

    def action(self, name, payload=None):
        """Validate small commands; never expose arbitrary Store writes."""
        if not isinstance(name, str) or (payload is not None and not isinstance(payload, dict)):
            return {'ok': False, 'message': '操作参数无效，请重试'}
        payload = payload or {}
        if name == 'open_typing_window':
            if payload or not self.typing_window or self._login_gate:
                return {'ok': False, 'message': '请先在工作台完成登录'}
            try:
                self.typing_window.show()
                return {'ok': True, 'navigate': 'browser' if self.embedded_browser else None}
            except Exception:
                return {'ok': False, 'message': '打字窗口暂未打开，请重试'}
        if name == 'browser_layout':
            try:
                rect = payload.get('rect')
                if (set(payload) != {'rect'} or not self.embedded_browser or self.closed
                        or (rect is not None and (not isinstance(rect, list) or len(rect) != 4
                            or any(type(n) not in (int, float) or not 0 <= n <= 1 for n in rect)
                            or rect[0] + rect[2] > 1.001 or rect[1] + rect[3] > 1.001))):
                    raise ValueError('浏览器区域无效')
                self.embedded_browser.layout(rect)
                return {'ok': True}
            except Exception:
                return {'ok': False, 'message': '浏览器区域暂未同步'}
        # Window controls remain usable while sites switch or learning runs.
        # Never hold the Store command lock across a WinForms Invoke.
        if name.startswith('window_'):
            try:
                return self._window_action(name, payload)
            except ValueError as exc:
                return {'ok': False, 'message': _text(exc)}
            except Exception as exc:
                return {'ok': False, 'message': f'窗口操作暂未成功（{type(exc).__name__}）'}
        with self._lock:
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

    def _window_action(self, name, payload):
        allowed = {'window_minimize', 'window_toggle_maximize', 'window_close', 'window_resize'}
        if name not in allowed:
            raise ValueError('不支持的窗口操作')
        if not self.window or not self.window_frame:
            raise ValueError('当前界面没有原生窗口')
        if (name != 'window_resize' and payload) or (name == 'window_resize' and set(payload) != {'edge'}):
            raise ValueError('窗口操作参数无效')
        if name == 'window_close':
            self.close()
            return {'ok': True, 'window': self.window_frame.snapshot()}
        if self.closing or self.closed:
            raise ValueError('程序正在关闭，状态会自动保存')
        if name == 'window_minimize':
            self.window_frame.minimize()
        elif name == 'window_toggle_maximize':
            self.window_frame.toggle_maximize()
        else:
            edge = payload.get('edge')
            if not isinstance(edge, str):
                raise ValueError('窗口缩放方向无效')
            self.window_frame.resize(edge)
        return {'ok': True, 'window': self.window_frame.snapshot()}

    def _action(self, name, payload):
        if name == 'browser_command':
            browser = self.embedded_browser
            command = payload.get('command')
            if not browser or command not in ('back', 'forward', 'reload', 'stop', 'home', 'go', 'mute'):
                raise ValueError('不支持的浏览器操作')
            if browser.snapshot()['phase'] not in ('ready', 'loading', 'navigation_error'):
                raise ValueError('浏览器尚未连接')
            if set(payload) != ({'command', 'url'} if command == 'go' else {'command'}):
                raise ValueError('浏览器操作参数无效')
            desired = self.store.get('desired', 'idle')
            if ((desired != 'login' and (desired not in ('idle', 'paused') or self.store.get('worker_busy', False)))
                    or self._typing_locked()):
                raise ValueError('请先暂停任务或结束打字，再操作网页')
            from gui.embedded_browser import browser_url
            browser.command(command, browser_url(payload['url']) if command == 'go' else None)
            return {'ok': True}
        if name.startswith('update_'):
            if payload:
                raise ValueError('更新操作不接受自定义地址或文件路径')
            if name == 'update_check':
                self.updater.check()
            elif name == 'update_download':
                self.updater.download()
            elif name == 'update_cancel':
                self.updater.cancel()
            elif name == 'update_release':
                import webbrowser
                webbrowser.open(self.updater.snapshot()['release_url'])
            elif name == 'update_install':
                if self._busy() or self.demo:
                    raise ValueError('请先暂停学习并结束手动打字，等待当前操作停止后安装')
                self.updater.reserve_install()
                self.store.set('desired', 'paused')
                self._update_install = True
                self.close()
            else:
                raise ValueError('不支持的更新操作')
            return {'ok': True}
        if name == 'login':
            if payload:
                raise ValueError('登录操作不需要额外参数')
            if self.watchdog and self._alive() and self.store.get('desired') == 'login':
                return self._show_browser()
            if (self.watchdog and self._alive() and self.store.get('desired') in ('catalog', 'scan', 'run')
                    and (self.store.get('workflow', {}) or {}).get('phase') != 'error'):
                return self._notify('正在检查登录状态，请稍候')
            if not self._login_gate and self.watchdog and self._alive():
                return self._show_browser()
            return self._begin_switch(self.active_site, recover=True, connect=True)
        if self._login_gate and name not in ('primary', 'switch_site', 'recover', 'open_logs', 'open_browser', 'none'):
            raise ValueError('请先点击登录并在 Edge 中完成登录')
        if name.startswith('assessment_'):
            return self.assessment.action(name, payload)
        if name.startswith('typing_'):
            return self._typing_action(name, payload)
        if name == 'primary':
            desired = self.store.get('desired', 'idle')
            name = self._primary(desired, self.store.get('worker_busy', False), self.store.get('account', {}),
                                 self.store.catalog(), self.store.selected_courses(), self.store.scoped_rows())['action']
            if name == 'login':
                return self._action('login', {})
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
            return self._begin_switch(self.active_site, recover=True, connect=True)
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
            raise ValueError('请先暂停并等待当前操作停止；手动打字期间请先停止打字，再开始学习')
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
        if name == 'start_tasks':
            ids = payload.get('ids')
            if (set(payload) != {'ids'} or not isinstance(ids, list) or not ids or len(ids) > 10000
                    or not all(isinstance(key, str) for key in ids) or len(ids) != len(set(ids))):
                raise ValueError('请勾选需要执行的任务')
            rows = {task['key']: task for task in self.store.scoped_rows()}
            if any(key not in rows or rows[key]['kind'] not in AUTOMATED_KINDS
                   or rows[key]['state'] not in ('pending', 'failed') for key in ids):
                raise ValueError('所选任务已变化，只能执行当前课程中已开放且可自动处理的任务')
            if not self.demo and not self._alive():
                raise ValueError('浏览器守护已停止，请先恢复连接')
            self.store.set('task_scope', {'account': self.store.get('account', {}).get('id'),
                'courses': self.store.selected_courses(), 'ids': list(ids)})
            for key in ids:
                if rows[key]['state'] == 'failed':
                    self.store.update(key, state='pending', attempts=0, note='用户选择重新执行此任务')
            # User selected this exact list. A later rescan may remove items,
            # but must never broaden the scope to the rest of the course.
            scanned = self.store.scope_scanned() and self.store.get('scan_progress', {}).get('status') == 'completed'
            self.store.set('run_mode', 'resume' if scanned else 'start')
            self.store.set('desired', 'run')
            return {**self._notify(f'只执行所选 {len(ids)} 项，按勾选顺序进行'), 'navigate': 'tasks'}
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
                resuming = self.store.get('desired') == 'paused' or name == 'resume'
                if not resuming:
                    self.store.set('task_scope', None)
                self.store.set('run_mode', 'resume' if resuming else 'start')
            self.store.set('desired', desired)
            return {**self._notify('正在连接课程' if desired == 'catalog' else '正在扫描所选课程' if desired == 'scan'
                                 else '已开始，会自动扫描所选课程并处理任务'),
                    'navigate': 'courses' if desired == 'catalog' else 'tasks'}
        raise ValueError('未知操作，请刷新界面后重试')

    def _typing_action(self, name, payload):
        allowed = {'typing_select': set(), 'typing_cancel': set(), 'typing_stop': set(),
            'typing_pause': set(), 'typing_resume': {'token'}, 'typing_speed': {'speed'},
            'typing_start': {'token', 'text', 'speed', 'confirmed'}}
        if name not in allowed or not payload.keys() <= allowed[name]:
            raise ValueError('手动打字操作参数无效')
        state = self.store.get('typing_status', {})
        desired, worker_busy = self.store.get('desired', 'idle'), self.store.get('worker_busy', False)
        view = self._typing_view(state, desired, worker_busy)
        if not view['available']:
            raise ValueError('受管理的 Edge 尚未连接，请先连接课程或恢复浏览器')
        if name not in ('typing_stop', 'typing_cancel') and (desired in ACTIVE or worker_busy or self.assessment.owns_browser):
            raise ValueError('请先暂停学习，并等待当前扫描或任务停止后再使用手动打字')
        if view['pending'] and name not in ('typing_pause', 'typing_stop', 'typing_cancel'):
            raise ValueError('正在处理上一条打字操作，请稍候')
        phase, target = view['phase'], view['target']
        if name == 'typing_select' and phase in ('running', 'paused'):
            raise ValueError('请先停止当前打字，再重新指定网页输入框')
        if name == 'typing_start' and phase not in ('ready', 'completed'):
            raise ValueError('请先指定网页输入框并确认目标')
        if name == 'typing_pause' and phase != 'running':
            raise ValueError('当前没有正在输出的文字')
        if name == 'typing_resume' and phase != 'paused':
            raise ValueError('当前打字未暂停，请重新指定目标')
        if name in ('typing_start', 'typing_resume'):
            token = payload.get('token')
            if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', token)
                    or not target or token != target['token']):
                raise ValueError('指定输入框已变化，请重新选择并确认')
            if target.get('type', '').casefold() == 'password':
                raise ValueError('密码输入框不支持手动打字')
        if name in ('typing_start', 'typing_speed'):
            speed = _number(payload.get('speed', 20))
            if speed is None or not 1 <= speed <= 60:
                raise ValueError('打字速度必须在每秒 1–60 字之间')
        if name == 'typing_start':
            text = payload.get('text')
            if not isinstance(text, str) or not text or len(text) > 50000:
                raise ValueError('请输入 1–50000 字的文字')
            if payload.get('confirmed') is not True:
                raise ValueError('请核对指定输入框并确认开始打字')
            if not target.get('multiline') and any(ch in text for ch in '\r\n'):
                raise ValueError('指定的是单行输入框，不能输出包含换行的文字')
        if self.demo:
            return self._demo_typing(name, payload, view)
        command = dict(payload, action=name, generation=state.get('generation'), request_id=uuid.uuid4().hex)
        if not self.watchdog.submit_typing(command):
            raise ValueError('打字连接已变化或操作队列已满，请恢复连接后重新指定输入框')
        self._typing_pending = {'request_id': command['request_id'], 'generation': command['generation']}
        if self.embedded_browser and name in ('typing_select', 'typing_start', 'typing_resume'):
            self.embedded_browser.focus()
        messages = {'typing_select': '请在受管理 Edge 中点击指定输入框，按 Esc 可取消',
            'typing_start': '已确认指定目标，开始模拟逐字输入', 'typing_pause': '正在暂停手动打字',
            'typing_resume': '继续向原指定输入框输出', 'typing_stop': '正在停止打字并清除目标',
            'typing_cancel': '正在取消指定输入框', 'typing_speed': '打字速度已更新'}
        return self._notify(messages[name])

    def _demo_typing(self, name, payload, view):
        state = {'phase': view['phase'], 'target': view['target'], 'sent': view['sent'],
                 'total': view['total'], 'speed': view['speed'], 'message': '演示模式，不会向真实网页输出文字'}
        if name == 'typing_select':
            state.update(phase='ready', target={'token': uuid.uuid4().hex, 'label': '演示答案输入框',
                'url': 'https://demo.example', 'frame': 'https://demo.example', 'tag': 'textarea',
                'type': '', 'multiline': True, 'characters': 0}, sent=0, total=0)
        elif name == 'typing_start':
            state.update(phase='completed', sent=len(payload['text']), total=len(payload['text']),
                         speed=payload.get('speed', 20))
            if state['target']:
                state['target'] = dict(state['target'], characters=state['target'].get('characters', 0) + len(payload['text']))
        elif name in ('typing_stop', 'typing_cancel'):
            state.update(phase='stopped', target=None)
        elif name == 'typing_speed':
            state['speed'] = payload['speed']
        elif name == 'typing_pause':
            state['phase'] = 'paused'
        elif name == 'typing_resume':
            state['phase'] = 'running'
        self.store.set('typing_status', state)
        return self._notify(state['message'])

    def _begin_switch(self, value, recover=False, connect=None):
        if self.demo or not self.base_config:
            raise ValueError('演示模式不切换真实网站')
        if not isinstance(value, str):
            raise ValueError('请填写雨课堂网站主页地址')
        url = normalize_site(value)
        config = site_config(self.base_config, url)
        connect = not self._deferred_login if connect is None else connect
        if url == self.active_site and self._alive() and not recover:
            return self._notify('当前已经是这个站点')
        self.switching = True
        self.assessment.reset(clear_chat=True)
        if self._deferred_login:
            self._login_gate = True
            self._login_error = ''
            self._login_session = uuid.uuid4().hex if connect else None
            config = dict(config, login_session=self._login_session, headless=False if connect else config.get('headless', False))
        self._notify('正在保存当前状态并切换站点')
        old_dog, old_store, old_url = self.watchdog, self.store, self.active_site
        self._lifecycle_thread = threading.Thread(target=self._finish_switch,
            args=(url, config, old_dog, old_store, old_url, connect), daemon=True, name='RainClassroomSiteSwitch')
        self._lifecycle_thread.start()
        return {'ok': True, 'message': self._notice,
                'navigate': 'browser' if self.embedded_browser and connect else 'courses'}

    def _finish_switch(self, url, config, old_dog, old_store, old_url, connect=True):
        dog = None
        try:
            if old_dog:
                old_dog.stop()
                while old_dog.is_alive():
                    old_dog.join(timeout=.2)
            if self.embedded_browser and not self.closing:
                if connect:
                    endpoint = self.embedded_browser.connect(config)
                    config = dict(config, embedded_endpoint=endpoint, headless=False)
                else:
                    self.embedded_browser.close()
            with self._lock:
                if self.closing:
                    self.switching = False
                    return
                from recovery.watchdog import Watchdog
                new_store = prepare_site(config, connect=connect)
                dog = (self.watchdog_factory or Watchdog)(config) if connect else None
                # Publish the new identity only once the old worker has exited.
                # Start failures restore the original selection and do not leave
                # a worker running against an undisclosed site.
                if dog:
                    if self.embedded_browser:
                        dog.browser_recover = self.embedded_browser.recover
                    dog.start()
                save_site(self.base_config, url)
                history = read_sites(self.base_config)[1]
                self.watchdog, self.store, self.active_site = dog, new_store, url
                self.history = history
                self._filters = {'task_type': 'all', 'unfinished_only': True}
                self.switching = False
                self._login_error = ''
                self._notify('站点已切换，登录和任务记录分别保存')
        except Exception as exc:
            # A newly launched worker must exit before restoring the old view.
            # Cleanup may take seconds and therefore cannot hold the API lock.
            if dog:
                dog.stop()
                while dog.is_alive():
                    dog.join(timeout=.2)
            if self.embedded_browser and not self.closing:
                self._dispose_browser()
            with self._lock:
                self.store, self.active_site, self.watchdog = old_store, old_url, old_dog
                old_store.set('desired', 'paused')
                old_store.set('worker_busy', False)
                old_store.event('站点切换失败', type(exc).__name__)
                self.switching = False
                self._login_error = f'连接失败（{type(exc).__name__}），请检查目录权限后重试'
                self._notify(f'站点切换失败（{type(exc).__name__}），请检查目录权限后恢复连接')

    def _show_browser(self):
        """Raise only an Edge window owned by this app, never a fresh profile."""
        if self.demo:
            return self._notify('演示模式不打开真实网站')
        if not self._alive() or not self.watchdog:
            raise ValueError('浏览器尚未连接，请先点击恢复连接')
        if self.embedded_browser:
            return {'ok': True, 'navigate': 'browser'}
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

    def _dispose_browser(self):
        if self.embedded_browser:
            try:
                self.embedded_browser.close()
            except Exception as exc:
                # Closing the native form also disposes its children. A failed
                # WebView2 cleanup must not leave the close gate locked forever.
                self.store.event('内置浏览器清理异常', type(exc).__name__)

    def close(self):
        with self._lock:
            if self.closing or self.closed:
                return
            self.closing = True
            self.assessment.reset(clear_chat=True)
            self.assessment.key = ''
            self.updater.cancel()
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
        self._dispose_browser()
        if self.typing_window:
            try:
                self.typing_window.destroy()
            except Exception as exc:
                self.store.event('打字窗口清理异常', type(exc).__name__)
        if self._update_install:
            try:
                # WatchDog.stop() uses desired=shutdown while draining the worker;
                # persist paused again only once it is gone, for failed/finished upgrades.
                self.store.set('desired', 'paused')
                self.updater.launch_installer()
            except Exception:
                # Leave the window available for retry if Windows rejects Setup.
                with self._lock:
                    self._update_install = False
                    self.closing = False
                    self._notify('安装程序未启动，工作台已保留；请在设置中重试更新')
                return
        with self._lock:
            self.closed = True
            window = self.window
        if window:
            window.destroy()
