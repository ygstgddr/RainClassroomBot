"""A single owned tool window, sharing the existing typing command queue."""
import threading

from gui.web_assets import load_document
from gui.window_frame import WindowFrame


class TypingWindow:
    def __init__(self, workbench):
        self.workbench = workbench
        self.window = None
        self._window_lock = threading.Lock()

    def show(self):
        import webview

        with self._window_lock:
            if self.workbench.closing or self.workbench.closed:
                raise ValueError('工作台正在关闭')
            if self.window and not self.window.events.closed.is_set():
                window = self.window
                self.workbench.window_frame._invoke(lambda: self._activate(window))
                return
            bridge = _TypingBridge(self.workbench)
            window = webview.create_window('模拟手动打字 · 雨课堂', html=load_document(typing_window=True),
                js_api=bridge, width=720, height=760, min_size=(470, 500), hidden=True, frameless=True,
                easy_drag=False, shadow=False, background_color='#172131', text_select=True)
            if window is None:
                raise RuntimeError('打字窗口未能打开')
            self.window = window
            window.events.closing += bridge._closing
            frame = WindowFrame(window, attach_shown=False)
            frame.desktop_glass_enabled = self.workbench.window_frame.desktop_glass_enabled
            bridge._frame = frame

            def attach():
                native = window.native
                parent = self.workbench.window.native
                if native.IsDisposed or parent.IsDisposed:
                    return
                from System.Drawing import Point
                from System.Windows.Forms import Screen
                area = Screen.FromControl(parent).WorkingArea
                width = min(native.Width, area.Width)
                height = min(native.Height, area.Height)
                x = parent.Right + 8 if parent.Right + width + 8 <= area.Right else area.Right - width - 12
                y = min(max(parent.Top + 28, area.Top), area.Bottom - height)
                native.Width, native.Height = width, height
                native.Location = Point(max(area.Left, x), y)
                native.Owner = parent

            self.workbench.window_frame._invoke(attach)
            try:
                frame.on_shown()
            except Exception:
                window.destroy()
                raise
            window.show()
            self.workbench.window_frame._invoke(lambda: self._activate(window))

    @staticmethod
    def _activate(window):
        from System.Windows.Forms import FormWindowState
        native = window.native
        if native.IsDisposed:
            return
        if native.WindowState == FormWindowState.Minimized:
            native.WindowState = FormWindowState.Normal
        native.Activate()

    def destroy(self):
        with self._window_lock:
            window, self.window = self.window, None
            if window and not window.events.closed.is_set():
                window.destroy()


class _TypingBridge:
    """The tool document can only read and control its own feature."""
    _ACTIONS = frozenset(('typing_select', 'typing_start', 'typing_resume', 'typing_cancel',
                          'typing_pause', 'typing_stop', 'typing_speed', 'pause'))
    _NEEDS_FOCUS = frozenset(('typing_select', 'typing_start', 'typing_resume'))

    def __init__(self, workbench):
        self._workbench = workbench
        self._closed = False
        self._frame = None

    def snapshot(self):
        owner = self._workbench
        with owner._lock:
            store = owner.store
            desired = store.get('desired', 'idle')
            busy = store.get('worker_busy', False)
            typing = owner._typing_view(store.get('typing_status', {}), desired, busy)
            if owner._login_gate:
                typing.update(available=False, can_select=False, can_start=False, can_resume=False,
                              message='请先在主窗口完成登录')
            elif owner.embedded_browser and owner.embedded_browser.rect is None and not typing['learning_busy']:
                typing.update(can_select=False, can_start=False, can_resume=False,
                              message='请打开主窗口的「浏览器」页，关闭遮挡网页的设置弹层')
            return {'typing': typing, 'window': self._frame.snapshot() if self._frame else {},
                    'state': {'phase': 'closing' if owner.closing else
                     'switching' if owner.switching else 'idle'}}

    def action(self, name, payload=None):
        if isinstance(name, str) and name.startswith('window_'):
            return self._window_action(name, payload)
        if not isinstance(name, str) or name not in self._ACTIONS:
            return {'ok': False, 'message': '该操作请在主窗口完成'}
        owner = self._workbench
        if name in self._NEEDS_FOCUS and owner.embedded_browser:
            view = self.snapshot()['typing']
            allowed = {'typing_select': 'can_select', 'typing_start': 'can_start', 'typing_resume': 'can_resume'}[name]
            if self._closed or not view[allowed]:
                return {'ok': False, 'message': '请确认主窗口已连接、学习任务已暂停，并检查输入目标'}
            try:
                if not owner.embedded_browser.focus_for_typing():
                    return {'ok': False, 'message': '请让主窗口的浏览器可见后重试'}
            except Exception:
                return {'ok': False, 'message': '主窗口的浏览器暂不可用，请稍候重试'}
        with owner._lock:
            if self._closed:
                return {'ok': False, 'message': '打字窗口已关闭'}
            return owner.action(name, payload)

    def _window_action(self, name, payload):
        frame = self._frame
        if self._closed or not frame or not frame.snapshot()['native_controls']:
            return {'ok': False, 'message': '打字窗口正在初始化或关闭'}
        try:
            if name == 'window_resize' and isinstance(payload, dict) and set(payload) == {'edge'} and isinstance(payload['edge'], str):
                frame.resize(payload['edge'])
            elif not payload and name == 'window_close':
                frame.window.destroy()
            elif not payload and name == 'window_minimize':
                frame.minimize()
            elif not payload and name == 'window_toggle_maximize':
                frame.toggle_maximize()
            else:
                raise ValueError('不支持的窗口操作')
            return {'ok': True}
        except Exception as exc:
            return {'ok': False, 'message': str(exc) if isinstance(exc, ValueError) else '窗口操作暂未完成'}

    def _closing(self):
        owner = self._workbench
        with owner._lock:
            if not (owner.closing or owner.closed or owner.switching):
                state = owner.store.get('typing_status', {})
                if state.get('target') or owner._typing_locked():
                    result = owner.action('typing_stop')
                    if not result.get('ok') and owner.watchdog and owner._alive():
                        return False
            self._closed = True
            return True
