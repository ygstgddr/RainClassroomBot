"""Native window operations for the local glass title bar.

The page never chooses an HWND, process, screen coordinate or window size.
Only this application's bound window receives commands. Windows still owns
the modal sizing loop, monitor work area, minimum size and accessibility.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import threading


APP_ID = 'RainClassroomBot.Desktop'
EDGES = {'w': 1, 'e': 2, 'n': 3, 'nw': 4, 'ne': 5, 's': 6, 'sw': 7, 'se': 8}


def icon_path():
    """Find the official ICO in source or in PyInstaller's resource folder."""
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    path = base / 'gui' / 'assets' / 'yuketang.ico'
    return path if path.is_file() else None


def set_app_id():
    """Group this executable under its own icon, rather than python.exe."""
    if os.name == 'nt':
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def work_area_metrics(monitor, work):
    """WM_GETMINMAXINFO uses monitor-relative positions, including left screens."""
    return (work[0] - monitor[0], work[1] - monitor[1],
            work[2] - work[0], work[3] - work[1])


class WindowFrame:
    """A tiny controller; expensive native work never runs during snapshots."""
    def __init__(self, window):
        self.window = window
        self._ready = False
        self._closed = False
        self._maximized = bool(getattr(window, 'maximized', False))
        self._lock = threading.RLock()
        self._commands = threading.Lock()
        self._subclass_callback = None
        self._native = None
        self._user32 = None
        self.desktop_glass_enabled = True
        self._backdrop = None
        events = getattr(window, 'events', None)
        if events:
            events.shown += self.on_shown
            events.maximized += self.on_maximized
            events.restored += self.on_restored
            events.closed += self.on_closed

    def snapshot(self):
        with self._lock:
            if self._ready and not self._closed and self._native and self._user32:
                # WinForms resize events dispatch on background threads; read
                # the native flag to avoid a late event reversing a fast toggle.
                self._maximized = bool(self._user32.IsZoomed(self._native))
            state = {'maximized': self._maximized,
                     'native_controls': self._ready and not self._closed}
            if self._backdrop and self._backdrop.active:
                state['desktop_glass'] = True
            return state

    def on_shown(self, *args):
        if os.name == 'nt':
            self._configure_windows()
        with self._lock:
            self._ready = True

    def on_maximized(self, *args):
        with self._lock:
            self._maximized = True

    def on_restored(self, *args):
        with self._lock:
            self._maximized = False

    def on_closed(self, *args):
        with self._lock:
            self._closed = True
            self._ready = False

    def _require(self):
        if not self.snapshot()['native_controls']:
            raise ValueError('窗口仍在初始化，请稍候')

    def minimize(self):
        with self._commands:
            self._require()
            self.window.minimize()

    def toggle_maximize(self):
        with self._commands:
            self._require()
            if self.snapshot()['maximized']:
                self.window.restore()
                self.on_restored()
            else:
                self._prepare_work_area()
                self.window.maximize()
                self.on_maximized()

    def resize(self, edge):
        with self._commands:
            self._require()
            if edge not in EDGES:
                raise ValueError('窗口缩放方向无效')
            if self.snapshot()['maximized']:
                raise ValueError('最大化时不能调整窗口边缘')
            if os.name != 'nt' or self._native is None:
                raise ValueError('当前窗口不支持原生边缘缩放')
            # Posting keeps the bridge available during the OS-managed sizing
            # loop. WinForms constrains the resulting size to MinimumSize.
            user32 = self._user32
            user32.ReleaseCapture()
            if not user32.PostMessageW(self._native, 0x0112, 0xF000 | EDGES[edge], 0):
                raise RuntimeError('Unable to begin window resize')

    def _invoke(self, function):
        """WinForms property mutations must run on its STA GUI thread."""
        from System import Action
        native = self.window.native
        if native.InvokeRequired:
            native.Invoke(Action(function))
        else:
            function()

    def _prepare_work_area(self):
        if os.name != 'nt' or self._native is None:
            return
        def update():
            from System.Windows.Forms import Screen
            self.window.native.MaximizedBounds = Screen.FromControl(self.window.native).WorkingArea
        self._invoke(update)

    def _configure_windows(self):
        """Retain resize/Snap semantics without drawing a system caption.

        pywebview sets FormBorderStyle.None. Reintroducing WS_THICKFRAME makes
        Windows SC_SIZE and keyboard sizing work; WM_NCCALCSIZE keeps all pixels
        inside the WebView, and WM_GETMINMAXINFO keeps maximization above taskbars.
        The subclass belongs only to this known native window and its callback
        stays alive for the controller lifetime.
        """
        from ctypes import wintypes
        # Private DLL wrappers matter: pywebview calls SetWindowPos with None
        # for ignored dimensions. Mutating ctypes.windll's shared argtypes would
        # break its drag handler even though our own native calls are valid.
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        comctl32 = ctypes.WinDLL('comctl32', use_last_error=True)
        self._user32 = user32
        result_type = ctypes.c_ssize_t
        subclass_type = ctypes.WINFUNCTYPE(result_type, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, subclass_type, ctypes.c_size_t, ctypes.c_size_t]
        comctl32.SetWindowSubclass.restype = wintypes.BOOL
        comctl32.RemoveWindowSubclass.argtypes = [wintypes.HWND, subclass_type, ctypes.c_size_t]
        comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
        comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        comctl32.DefSubclassProc.restype = result_type
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.IsZoomed.argtypes = [wintypes.HWND]
        if hasattr(user32, 'GetDpiForWindow'):
            user32.GetDpiForWindow.argtypes = [wintypes.HWND]
            user32.GetDpiForWindow.restype = wintypes.UINT
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE

        class MonitorInfo(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                        ('work', wintypes.RECT), ('flags', wintypes.DWORD)]

        class MinMaxInfo(ctypes.Structure):
            _fields_ = [('reserved', wintypes.POINT), ('max_size', wintypes.POINT),
                        ('max_position', wintypes.POINT), ('min_track', wintypes.POINT),
                        ('max_track', wintypes.POINT)]

        class WindowPosition(ctypes.Structure):
            _fields_ = [('window', wintypes.HWND), ('insert_after', wintypes.HWND),
                        ('x', ctypes.c_int), ('y', ctypes.c_int),
                        ('width', ctypes.c_int), ('height', ctypes.c_int),
                        ('flags', wintypes.UINT)]

        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        def subclass(hwnd, message, wparam, lparam, subclass_id, reference):
            try:
                if message == 0x0083:  # WM_NCCALCSIZE
                    return 0
                if message == 0x0085:  # WM_NCPAINT: HTML owns every frame pixel.
                    return 0
                if message == 0x0086:  # Keep native activation, skip frame repaint.
                    return comctl32.DefSubclassProc(hwnd, message, wparam, -1)
                if message == 0x0046 and user32.IsZoomed(hwnd):  # WM_WINDOWPOSCHANGING
                    position = ctypes.cast(lparam, ctypes.POINTER(WindowPosition)).contents
                    if position.flags & 0x0001 and not position.flags & 0x0002:
                        # pywebview's JavaScript drag handler calls move() even
                        # while maximized. Keep that move inside the work area;
                        # otherwise IsZoomed stays true but the taskbar is covered.
                        info = MonitorInfo()
                        info.size = ctypes.sizeof(info)
                        if user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
                            position.x, position.y = info.work.left, info.work.top
                if message == 0x0024:  # WM_GETMINMAXINFO
                    info = MonitorInfo()
                    info.size = ctypes.sizeof(info)
                    if user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
                        data = ctypes.cast(lparam, ctypes.POINTER(MinMaxInfo)).contents
                        monitor = info.monitor
                        work = info.work
                        x, y, width, height = work_area_metrics(
                            (monitor.left, monitor.top, monitor.right, monitor.bottom),
                            (work.left, work.top, work.right, work.bottom))
                        data.max_position.x, data.max_position.y = x, y
                        data.max_size.x, data.max_size.y = width, height
                        # Let WinForms still apply MinimumSize tracking rules.
                        result = comctl32.DefSubclassProc(hwnd, message, wparam, lparam)
                        data.max_position.x, data.max_position.y = x, y
                        data.max_size.x, data.max_size.y = width, height
                        return result
                if message == 0x0084 and not user32.IsZoomed(hwnd):  # WM_NCHITTEST
                    bounds = wintypes.RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(bounds)):
                        x = ctypes.c_short(lparam & 0xffff).value
                        y = ctypes.c_short((lparam >> 16) & 0xffff).value
                        dpi = user32.GetDpiForWindow(hwnd) if hasattr(user32, 'GetDpiForWindow') else 96
                        edge = max(6, round(6 * dpi / 96))
                        left, right = x < bounds.left + edge, x >= bounds.right - edge
                        top, bottom = y < bounds.top + edge, y >= bounds.bottom - edge
                        if top and left: return 13
                        if top and right: return 14
                        if bottom and left: return 16
                        if bottom and right: return 17
                        if left: return 10
                        if right: return 11
                        if top: return 12
                        if bottom: return 15
                if message == 0x0082:  # WM_NCDESTROY: remove only this subclass.
                    comctl32.RemoveWindowSubclass(hwnd, self._subclass_callback, 1)
            except Exception:
                # Never let an optional native enhancement break the message loop.
                pass
            return comctl32.DefSubclassProc(hwnd, message, wparam, lparam)

        self._subclass_callback = subclass_type(subclass)

        def configure():
            from System.Drawing import Icon
            native = self.window.native
            hwnd = native.Handle.ToInt64()
            self._native = hwnd
            if not comctl32.SetWindowSubclass(hwnd, self._subclass_callback, 1, 0):
                raise RuntimeError('Unable to initialize frameless window')
            get_style = user32.GetWindowLongW
            set_style = user32.SetWindowLongW
            get_style.argtypes = [wintypes.HWND, ctypes.c_int]
            set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            # CAPTION off; resize, standard system menu, min/max keyboard support on.
            style = (get_style(hwnd, -16) | 0x00040000 | 0x00020000 | 0x00010000 | 0x00080000) & ~0x00C00000
            set_style(hwnd, -16, style)
            path = icon_path()
            if path:
                # WinForms sends both WM_SETICON sizes and owns this Icon resource.
                native.Icon = Icon(str(path))
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0020 | 0x0001 | 0x0002 | 0x0004 | 0x0010)
            try:
                dwm = ctypes.WinDLL('dwmapi', use_last_error=True)
                dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                    ctypes.c_void_p, wintypes.DWORD]
                rounded = ctypes.c_int(2)  # DWMWCP_ROUND, supported on Windows 11.
                border = wintypes.DWORD(0xFFFFFFFE)  # DWMWA_COLOR_NONE.
                dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded))
                dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
            except (AttributeError, OSError):
                pass
            if self.desktop_glass_enabled:
                try:
                    from gui.desktop_backdrop import DesktopBackdrop
                    self._backdrop = DesktopBackdrop()
                    if self._backdrop.apply(native):
                        # The first activation predates this subclass/material.
                        # Recalculate nonclient composition, then restore the
                        # actual activation state. This never moves input focus.
                        user32.GetForegroundWindow.restype = wintypes.HWND
                        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                        user32.SendMessageW.restype = result_type
                        user32.SendMessageW(hwnd, 0x0086, 0, -1)
                        user32.SendMessageW(hwnd, 0x0086,
                            int(user32.GetForegroundWindow() == hwnd), -1)
                except Exception:
                    # Material support must not disable resize/window controls.
                    import logging
                    logging.getLogger(__name__).exception('Unable to enable desktop glass')
        self._invoke(configure)
        self._prepare_work_area()
