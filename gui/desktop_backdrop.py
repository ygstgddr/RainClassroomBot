"""Windows 11 desktop Acrylic behind an opaque-foreground WebView2.

Inspired by token-monitor's native backgroundMaterial + clear renderer design.
Accent recipe adapted from Javis603/token-monitor (MIT); see third-party notices.
Falls back to documented DWM Acrylic if Accent is unavailable.
Never changes Form.Opacity, transparency keys, or input hit testing.
"""
import ctypes
from ctypes import wintypes
import logging
import os
import sys


def transparency_allowed():
    """Honor the OS accessibility preference before enabling the material."""
    if os.name != 'nt' or sys.getwindowsversion().build < 22621:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
            if not winreg.QueryValueEx(key, 'EnableTransparency')[0]:
                return False
    except OSError:
        pass  # Windows defaults to enabled when no preference has been saved.
    class HighContrast(ctypes.Structure):
        _fields_ = [('size', wintypes.UINT), ('flags', wintypes.DWORD), ('scheme', wintypes.LPWSTR)]
    state = HighContrast()
    state.size = ctypes.sizeof(state)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    if not user32.SystemParametersInfoW(0x0042, state.size, ctypes.byref(state), 0):
        return False
    return not bool(state.flags & 1)


class DesktopBackdrop:
    """Created and applied on the bound WinForms UI thread only."""
    def __init__(self):
        self.active = False
        self.mode = 'opaque'

    @staticmethod
    def _accent(hwnd, dwm):
        """Accent Acrylic keeps its material when another window is active.

        This API is optional/undocumented. Resolve lazily, free the region on
        every path, and undo partial configuration before the DWM fallback.
        """
        class Accent(ctypes.Structure):
            _fields_ = [('state', ctypes.c_int), ('flags', ctypes.c_int),
                        ('color', wintypes.DWORD), ('animation', ctypes.c_int)]
        class Data(ctypes.Structure):
            _fields_ = [('attribute', ctypes.c_int), ('data', ctypes.c_void_p), ('size', ctypes.c_size_t)]
        class Blur(ctypes.Structure):
            _fields_ = [('flags', wintypes.DWORD), ('enabled', wintypes.BOOL),
                        ('region', wintypes.HANDLE), ('transition', wintypes.BOOL)]
        user = ctypes.WinDLL('user32', use_last_error=True)
        composition = user.SetWindowCompositionAttribute
        composition.argtypes = [wintypes.HWND, ctypes.POINTER(Data)]
        composition.restype = wintypes.BOOL
        gdi = ctypes.WinDLL('gdi32', use_last_error=True)
        gdi.CreateRectRgn.argtypes = [ctypes.c_int] * 4
        gdi.CreateRectRgn.restype = wintypes.HANDLE
        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi.DeleteObject.restype = wintypes.BOOL
        dwm.DwmEnableBlurBehindWindow.argtypes = [wintypes.HWND, ctypes.POINTER(Blur)]
        dwm.DwmEnableBlurBehindWindow.restype = ctypes.c_long
        region = gdi.CreateRectRgn(0, 0, -1, -1)
        if not region:
            return False
        success = False
        try:
            blur = Blur(7, True, region, True)
            if dwm.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(blur)) < 0:
                return False
            # Enabling blur may reset the client's composition margins. Extend
            # after that call, matching the upstream recipe's required order.
            margins = (ctypes.c_int * 4)(-1, -1, -1, -1)
            if dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) < 0:
                return False
            policy = Accent(4, 0, 0x3a232323, 0)
            data = Data(19, ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p), ctypes.sizeof(policy))
            success = bool(composition(hwnd, ctypes.byref(data)))
            return success
        finally:
            try:
                if not success:
                    policy = Accent(0, 0, 0, 0)
                    data = Data(19, ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p), ctypes.sizeof(policy))
                    composition(hwnd, ctypes.byref(data))
                    blur = Blur(1, False, None, False)
                    dwm.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(blur))
            finally:
                gdi.DeleteObject(region)

    def apply(self, native):
        if self.active:
            return True
        if not transparency_allowed():
            return False
        from System.Drawing import Color
        old_color = native.BackColor
        old_web_color = native.webview.DefaultBackgroundColor
        hwnd = native.Handle.ToInt64()
        dwm = ctypes.WinDLL('dwmapi', use_last_error=True)
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.c_void_p]
        dwm.DwmExtendFrameIntoClientArea.restype = ctypes.c_long

        def attribute(key, value):
            data = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, key, ctypes.byref(data), ctypes.sizeof(data))

        try:
            # Black becomes the transparent client substrate only after the
            # DWM frame is extended. Text and controls retain full alpha.
            native.BackColor = Color.Black
            native.webview.DefaultBackgroundColor = Color.Transparent
            margins = (ctypes.c_int * 4)(-1, -1, -1, -1)
            if dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) < 0:
                raise OSError('DWM client extension unavailable')
            attribute(20, 1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            attribute(38, 1)  # Do not stack system Acrylic on top of Accent.
            try:
                accent = self._accent(hwnd, dwm)
            except (AttributeError, OSError):
                accent = False
            if accent:
                self.mode = 'accent'
            elif attribute(38, 3) >= 0:
                self.mode = 'acrylic'
            else:
                raise OSError('Desktop glass unavailable')
            self.active = True
            return True
        except Exception:
            # Restore both native and WebView backgrounds before the renderer
            # is allowed to hide its wallpaper. No black/invisible fallback.
            attribute(38, 1)
            attribute(20, 0)
            margins = (ctypes.c_int * 4)(0, 0, 0, 0)
            dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
            native.webview.DefaultBackgroundColor = old_web_color
            native.BackColor = old_color
            logging.getLogger(__name__).exception('Desktop Acrylic unavailable; using local wallpaper')
            return False
