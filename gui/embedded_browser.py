"""Own the learning WebView2 on the WinForms thread, separate from the UI bridge."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


def browser_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) <= 32 for c in value):
        raise ValueError('请输入有效的网址')
    parts = urlsplit(value)
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or '\\' in value or parts.port == 0
            or not (parts.scheme == 'https' or (parts.scheme == 'http'
                      and parts.hostname in ('localhost', '127.0.0.1')))):
        raise ValueError('请输入 HTTPS 网址')
    return value


class EmbeddedBrowser:
    def __init__(self, window, invoke):
        self.window, self.invoke = window, invoke
        self.control = None
        self.home = ''
        self.folder = None
        self.ports = {}
        self.rect = None
        self.interactive = True
        self.navigation_revision = 0
        self._document_url = ''
        self._assessment_registry = f'_rain_assessment_{uuid4().hex}'
        self._assessment_lock = threading.Lock()
        self._assessment_capture = None
        self._assessment_surface = None
        self.recovery_timer = None
        self.recovery_attempts = 0
        self.state = {'phase': 'idle', 'message': '内置浏览器尚未连接'}

    def _post(self, callback):
        from System import Action
        native = self.window.native
        if native and not native.IsDisposed:
            native.BeginInvoke(Action(callback))

    def snapshot(self):
        return dict(self.state)

    def document_digest(self):
        return hashlib.sha256(self._document_url.encode('utf-8')).hexdigest()

    def connect(self, config):
        """Wait off the GUI thread for one control with a site-specific profile."""
        folder = Path(config['root']) / 'webview2_profile'
        home = config['base_url'] + config['entry_path']
        ready = threading.Event()

        def prepare():
            if self.folder == folder and self.control and self.state['phase'] in ('ready', 'loading', 'navigation_error'):
                ready.set()
                return
            self._dispose()
            self.folder, self.home = folder, home
            self.recovery_attempts = 0
            self._create(ready)

        self.invoke(prepare)
        if not ready.wait(25) or self.state['phase'] in ('error', 'starting'):
            raise RuntimeError('内置浏览器初始化失败')
        return f'http://127.0.0.1:{self.ports[folder]}'

    def _create(self, ready):
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2
        self.state = {'phase': 'starting', 'message': '正在启动内置浏览器…'}
        if self.folder not in self.ports:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                self.ports[self.folder] = listener.getsockname()[1]
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = str(self.folder)
        # The workbench WebView2 has a different profile and no automation port.
        props.AdditionalBrowserArguments = (
            f'--remote-debugging-port={self.ports[self.folder]} --remote-debugging-address=127.0.0.1 '
            '--disable-backgrounding-occluded-windows --disable-renderer-backgrounding')
        view = WebView2()
        self.control = view
        view.CreationProperties = props
        view.Visible = False
        native = self.window.native
        native.Controls.Add(view)

        def initialized(sender, args):
            if self.control is not view:
                return
            try:
                if not args.IsSuccess:
                    raise RuntimeError('WebView2 初始化失败')
                core = view.CoreWebView2
                settings = core.Settings
                settings.AreHostObjectsAllowed = False
                settings.IsWebMessageEnabled = False
                settings.AreDevToolsEnabled = False
                settings.IsStatusBarEnabled = False
                core.IsMuted = True
                core.NavigationStarting += self._navigation_starting
                core.NavigationCompleted += self._navigation_completed
                core.SourceChanged += self._source_changed
                core.HistoryChanged += self._source_changed
                core.DocumentTitleChanged += self._source_changed
                core.IsMutedChanged += self._source_changed
                core.NewWindowRequested += self._new_window
                core.ProcessFailed += self._process_failed
                self.state = {'phase': 'ready', 'message': '内置浏览器已连接'}
                self._apply_layout()
            except Exception:
                self.state = {'phase': 'error', 'message': '内置浏览器初始化失败，请重新连接'}
            finally:
                ready.set()

        view.CoreWebView2InitializationCompleted += initialized
        view.EnsureCoreWebView2Async(None)

    def _navigation_starting(self, core, args):
        try:
            browser_url(str(args.Uri))
        except ValueError:
            args.Cancel = True
            return
        self.navigation_revision += 1
        self.state = {**self.state, 'phase': 'loading', 'message': '网页加载中…'}

    def _navigation_completed(self, core, args):
        self.state = {**self.state, 'phase': 'ready' if args.IsSuccess else 'navigation_error',
                      'message': '网页已加载' if args.IsSuccess else f'网页加载未完成（{args.WebErrorStatus}）'}
        self._source_changed(core, args)

    def _source_changed(self, core, _):
        document_url = str(core.Source or '')
        parts = urlsplit(document_url)
        # Avoid exposing course signatures and login callback parameters in the toolbar.
        source = urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')) if parts.hostname else ''
        if document_url != self._document_url:
            self.navigation_revision += 1
            self._document_url = document_url
        self.state = {**self.state, 'url': source, 'title': str(core.DocumentTitle or '')[:180],
                      'back': bool(core.CanGoBack), 'forward': bool(core.CanGoForward), 'muted': bool(core.IsMuted)}

    def _new_window(self, core, args):
        args.Handled = True
        try:
            url = browser_url(str(args.Uri))
        except ValueError:
            return
        self._post(lambda: core.Navigate(url) if self.control and self.control.CoreWebView2 == core else None)

    def _process_failed(self, core, args):
        kind = str(args.ProcessFailedKind)
        if kind not in ('BrowserProcessExited', 'RenderProcessExited', 'RenderProcessUnresponsive'):
            return
        self._schedule_recovery()

    def recover(self):
        self._post(self._schedule_recovery)

    def _schedule_recovery(self):
        if not self.control:
            return
        self.state = {**self.state, 'phase': 'error', 'message': '内置浏览器进程异常，正在尝试恢复…'}
        if self.recovery_timer:
            return
        if self.recovery_attempts >= 2:
            self.state['message'] = '内置浏览器未能恢复，请重新连接'
            return
        from System.Windows.Forms import Timer
        self.recovery_attempts += 1
        timer = Timer()
        self.recovery_timer = timer
        timer.Interval = 1500 * self.recovery_attempts

        def recover(*_):
            self._dispose()
            try:
                self._create(threading.Event())
            except Exception:
                self.state = {'phase': 'error', 'message': '内置浏览器未能恢复，请重新连接'}

        timer.Tick += recover
        timer.Start()

    def _apply_layout(self):
        view = self.control
        if not view or view.IsDisposed:
            return
        if view.CoreWebView2 is None:
            view.Visible = False
            return
        view.Enabled = self.interactive
        if not self.interactive and not view.CoreWebView2.IsMuted:
            view.CoreWebView2.IsMuted = True
        if self.rect is None:
            # Park the surface outside the client area while another page is open.
            # Keep a real viewport for Playwright and media in the same session.
            view.SetBounds(-32768, 0, max(view.Width, 960), max(view.Height, 540))
            view.Visible = True
            return
        size = self.window.native.ClientSize
        x, y, w, h = self.rect
        view.SetBounds(round(x * size.Width), round(y * size.Height),
                       round(w * size.Width), round(h * size.Height))
        view.Visible = True
        view.BringToFront()

    def layout(self, rect):
        self.rect = rect
        self._post(self._apply_layout)

    def allow_input(self, allowed):
        if self.interactive != allowed:
            self.interactive = allowed
            self._post(self._apply_layout)

    def focus(self):
        self._post(lambda: self.control.Focus() if self.control and self.control.Visible else None)

    def focus_for_typing(self):
        available = False

        def apply():
            nonlocal available
            if self.rect is not None and self.control and self.control.Visible and self.control.Enabled:
                self.window.native.Activate()
                # Focus() can report false while the Chromium child receives
                # focus. The typing engine checks the actual document and target.
                self.control.Focus()
                available = True

        self.invoke(apply)
        return available

    def _assessment_run(self, build_script, expected_revision=None, *, writing=False, cancelled=None):
        """Use the WinForms thread for dispatch and await the result on the caller's worker."""
        from browser.assessment_route import assessment_origin

        pending = None
        core = None
        source = ''
        revision = 0
        start_error = ''

        def start():
            nonlocal pending, core, source, revision, start_error
            if cancelled and cancelled():
                start_error = '操作已停止，请重新确认后再填写。'
                return
            core = self.control.CoreWebView2 if self.control else None
            if core is None or self.state['phase'] != 'ready' or self.rect is None or (writing and not self.interactive):
                start_error = '请先打开并等待可操作的题页加载。'
                return
            source = str(core.Source)
            revision = self.navigation_revision
            if expected_revision is not None and revision != expected_revision:
                start_error = '题页已改变，请重新读取。'
                return
            origin = assessment_origin(source, self.home)
            if origin is None:
                start_error = '当前网页不是此雨课堂站点或已适配的考试题页。'
                return
            pending = core.ExecuteScriptAsync(build_script(origin, self._assessment_registry))

        self.invoke(start)
        if start_error:
            raise ValueError(start_error)
        try:
            if pending is None or not pending.Wait(8000):
                raise ValueError('题页操作超时，请核对当前页面。')
            result = json.loads(str(pending.Result))
        except (TypeError, json.JSONDecodeError):
            raise ValueError('题页没有返回有效内容，请重试。') from None

        same_document = False

        def check_document():
            nonlocal same_document
            same_document = (self.control is not None and self.control.CoreWebView2 == core
                             and str(core.Source) == source and self.navigation_revision == revision)

        self.invoke(check_document)
        if not same_document:
            raise ValueError('操作期间题页已改变，请核对当前页面。')
        if not isinstance(result, dict):
            raise ValueError('题页没有返回有效内容，请重试。')
        if result.get('error'):
            raise ValueError(result['error'])
        return result, revision

    def read_assessment_surface(self):
        """Return a bounded observation without control values or link targets."""
        from browser.assessment_page import surface_script

        with self._assessment_lock:
            result, revision = self._assessment_run(surface_script)
            surface = {key: result[key] for key in ('title', 'text', 'controls')}
            surface['revision'] = revision
            self._assessment_surface = deepcopy(surface)
            return surface

    def agent_session(self, cancelled):
        """Attach browser tools to this exact WebView2 without launching Edge."""
        from browser.agent_browser import connect_agent_browser
        source = ''
        def current():
            nonlocal source
            if self.control and self.control.CoreWebView2:
                source = str(self.control.CoreWebView2.Source)
        self.invoke(current)
        if not source or self.folder not in self.ports:
            raise ValueError('内置浏览器尚未连接。')
        return connect_agent_browser(f'http://127.0.0.1:{self.ports[self.folder]}', source, self.home, cancelled)

    def read_assessment_page(self, surface=None, groups=None):
        """Read standard questions or adopt groups of observed native controls."""
        from browser.assessment_page import adapt_groups, normalize_capture, question_script

        with self._assessment_lock:
            proposals = None
            revision = None
            if surface is not None or groups is not None:
                if surface is None or surface != self._assessment_surface:
                    raise ValueError('题页观测已过期，请重新观察。')
                proposals = adapt_groups(surface, groups)
                revision = surface['revision']
            result, revision = self._assessment_run(
                lambda origin, registry: question_script(origin, registry, proposals), revision)
            questions, warnings = normalize_capture(result)
            capture = questions, warnings, revision
            self._assessment_capture = deepcopy(capture), result, proposals
            return capture

    def _revalidate_assessment(self, capture):
        from browser.assessment_page import question_script

        if self._assessment_capture is None or capture != self._assessment_capture[0]:
            raise ValueError('题页读取结果已过期，请重新读取。')
        _, expected, proposals = self._assessment_capture
        current, _ = self._assessment_run(
            lambda origin, registry: question_script(origin, registry, proposals), capture[2])
        if current != expected:
            raise ValueError('题页内容或答题控件已变化，请重新读取。')
        return expected, proposals

    def revalidate_assessment_page(self, capture):
        with self._assessment_lock:
            self._revalidate_assessment(capture)
            return capture

    def fill_assessment_answers(self, capture, answers, *, cancelled=None):
        """Revalidate the reviewed capture and fill only eligible empty fields."""
        from browser.assessment_page import fill_script, verify_fill_script

        if not isinstance(answers, dict) or len(answers) > 80 or any(
                not isinstance(key, str) or not key.isdecimal() or len(key) > 2 for key in answers):
            raise ValueError('待填写的答案格式无效。')
        with self._assessment_lock:
            expected, proposals = self._revalidate_assessment(capture)
            questions = {question['id']: question for question in capture[0]}
            eligible, errors = {}, {}
            for key, answer in answers.items():
                question = questions.get(key)
                if question is None or question['issues']:
                    errors[key] = '未找到可安全填写的题目。'
                    continue
                if question['kind'] == 'multiple':
                    valid = isinstance(answer, list) and 1 <= len(answer) <= 20 and all(
                        isinstance(value, str) and len(value) <= 2 for value in answer)
                else:
                    valid = isinstance(answer, str) and 1 <= len(answer) <= 1000
                if valid:
                    eligible[key] = answer
                else:
                    errors[key] = '待填写的答案格式无效。'
            if not eligible:
                return {'filled': [], 'errors': errors}
            result, _ = self._assessment_run(lambda origin, registry: fill_script(
                origin, registry, proposals, expected, eligible), capture[2], writing=True, cancelled=cancelled)
            filled = result['filled']
            errors.update(result['errors'])
            if pending := result.get('pending'):
                verified, _ = self._assessment_run(lambda origin, registry: verify_fill_script(
                    origin, registry, proposals, expected, pending), capture[2])
                filled.extend(verified['filled'])
                for question_id in set(pending) - set(verified['filled']):
                    errors[question_id] = '页面未确认选中，请核对这道题。'
            return {'filled': filled, 'errors': errors}

    def submit_assessment_answers(self, capture, answers, *, cancelled=None):
        """Check every reviewed answer and submit once, including the site dialog.

        A click is not completion. Missing receipts or navigation return unknown;
        they must never trigger a second click or mark the task completed.
        """
        from browser.assessment_submit import submission_script

        with self._assessment_lock:
            expected, proposals = self._revalidate_assessment(capture)
            ids = {row['id'] for row in capture[0]}
            if (not ids or set(answers) != ids or capture[1]
                    or any(row['issues'] for row in capture[0])):
                return {'state': 'blocked', 'message': '题目尚未完整读取、复核和填写，已停止提交。'}
            deadline = time.monotonic() + 15
            begin = True
            while time.monotonic() < deadline:
                if cancelled and cancelled():
                    return {'state': 'unknown', 'message': '操作已停止，请核对网站的提交状态。'}
                try:
                    result, _ = self._assessment_run(lambda origin, registry: submission_script(
                        origin, registry, proposals, expected, answers, begin=begin),
                        capture[2], writing=True, cancelled=cancelled)
                except ValueError as exc:
                    return {'state': 'unknown', 'message': f'未获得可靠提交回执：{exc}'}
                if result.get('state') != 'waiting':
                    return result
                begin = False
                time.sleep(.25)
            return {'state': 'unknown', 'message': '网站尚未确认提交结果；不会重复提交，请稍后重新扫描。'}

    def command(self, name, url=None):
        def apply():
            core = self.control.CoreWebView2 if self.control else None
            if core is None:
                return
            try:
                if name == 'go':
                    core.Navigate(url)
                elif name == 'home':
                    core.Navigate(self.home)
                elif name == 'back' and core.CanGoBack:
                    core.GoBack()
                elif name == 'forward' and core.CanGoForward:
                    core.GoForward()
                elif name == 'reload':
                    core.Reload()
                elif name == 'stop':
                    core.Stop()
                elif name == 'mute':
                    core.IsMuted = not core.IsMuted
            except Exception:
                self.state = {**self.state, 'message': '浏览器操作暂未完成，请重试'}

        self._post(apply)

    def _dispose(self):
        self.navigation_revision += 1
        self._document_url = ''
        self._assessment_capture = None
        self._assessment_surface = None
        if self.recovery_timer:
            self.recovery_timer.Stop()
            self.recovery_timer.Dispose()
            self.recovery_timer = None
        view, self.control = self.control, None
        if view:
            self.window.native.Controls.Remove(view)
            view.Dispose()

    def close(self):
        self.invoke(self._dispose)
        self.folder = None
        self.state = {'phase': 'idle', 'message': '内置浏览器尚未连接'}
