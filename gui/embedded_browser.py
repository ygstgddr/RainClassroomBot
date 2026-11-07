"""Own the learning WebView2 on the WinForms thread, separate from the UI bridge."""
from pathlib import Path
import socket
import threading
from urllib.parse import urlsplit, urlunsplit


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
        self.state = {**self.state, 'phase': 'loading', 'message': '网页加载中…'}

    def _navigation_completed(self, core, args):
        self.state = {**self.state, 'phase': 'ready' if args.IsSuccess else 'navigation_error',
                      'message': '网页已加载' if args.IsSuccess else f'网页加载未完成（{args.WebErrorStatus}）'}
        self._source_changed(core, args)

    def _source_changed(self, core, _):
        parts = urlsplit(str(core.Source or ''))
        # Avoid exposing course signatures and login callback parameters in the toolbar.
        source = urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')) if parts.hostname else ''
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
