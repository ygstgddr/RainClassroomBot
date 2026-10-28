"""Optional native effects must fail back to a usable opaque renderer."""
import sys
from types import SimpleNamespace
import pytest
from gui import desktop_backdrop as module


class NativeCall:
    def __init__(self, function): self.function = function
    def __call__(self, *args): return self.function(*args)


@pytest.mark.parametrize('failure', ['extend', 'material'])
def test_native_failure_restores_renderer_and_native_background(monkeypatch, failure):
    monkeypatch.setattr(module, 'transparency_allowed', lambda: True)
    monkeypatch.setitem(sys.modules, 'System.Drawing', SimpleNamespace(Color=SimpleNamespace(Black='black', Transparent='clear')))
    attributes = []
    def attribute(hwnd, key, pointer, size):
        attributes.append((key, pointer._obj.value))
        return -1 if failure == 'material' and (key, pointer._obj.value) == (38, 3) else 0
    dwm = SimpleNamespace(DwmSetWindowAttribute=NativeCall(attribute),
        DwmExtendFrameIntoClientArea=NativeCall(lambda *_: -1 if failure == 'extend' else 0))
    monkeypatch.setattr(module.ctypes, 'WinDLL', lambda *_args, **_kw: dwm)
    native = SimpleNamespace(BackColor='original form', webview=SimpleNamespace(DefaultBackgroundColor='original web'),
                             Handle=SimpleNamespace(ToInt64=lambda: 123))
    backdrop = module.DesktopBackdrop()
    assert not backdrop.apply(native)
    assert not backdrop.active
    assert native.BackColor == 'original form'
    assert native.webview.DefaultBackgroundColor == 'original web'
    assert (38, 1) in attributes  # Explicitly disable a partially set material.


def test_disabled_system_transparency_never_touches_native_window(monkeypatch):
    monkeypatch.setattr(module, 'transparency_allowed', lambda: False)
    assert not module.DesktopBackdrop().apply(object())


@pytest.mark.parametrize('accent_available', [True, False])
def test_accent_success_and_documented_fallback_keep_foreground_opaque(monkeypatch, accent_available):
    monkeypatch.setattr(module, 'transparency_allowed', lambda: True)
    monkeypatch.setitem(sys.modules, 'System.Drawing', SimpleNamespace(Color=SimpleNamespace(Black='black', Transparent='clear')))
    monkeypatch.setattr(module.DesktopBackdrop, '_accent', staticmethod(lambda *_: accent_available))
    dwm = SimpleNamespace(DwmSetWindowAttribute=NativeCall(lambda *_: 0),
                         DwmExtendFrameIntoClientArea=NativeCall(lambda *_: 0))
    monkeypatch.setattr(module.ctypes, 'WinDLL', lambda *_args, **_kw: dwm)
    native = SimpleNamespace(BackColor='form', webview=SimpleNamespace(DefaultBackgroundColor='web'),
        Handle=SimpleNamespace(ToInt64=lambda: 123), Opacity=1)
    effect = module.DesktopBackdrop()
    assert effect.apply(native)
    assert effect.mode == ('accent' if accent_available else 'acrylic')
    assert native.Opacity == 1
    assert native.webview.DefaultBackgroundColor == 'clear'


@pytest.mark.parametrize('accepted', [True, False])
def test_accent_region_is_released_even_when_api_rejects_it(monkeypatch, accepted):
    released = []
    user = SimpleNamespace(SetWindowCompositionAttribute=NativeCall(lambda *_: accepted))
    gdi = SimpleNamespace(CreateRectRgn=NativeCall(lambda *_: 456), DeleteObject=NativeCall(released.append))
    dwm = SimpleNamespace(DwmEnableBlurBehindWindow=NativeCall(lambda *_: 0),
                         DwmExtendFrameIntoClientArea=NativeCall(lambda *_: 0))
    monkeypatch.setattr(module.ctypes, 'WinDLL', lambda name, **_kw: {'user32': user, 'gdi32': gdi}[name])
    assert module.DesktopBackdrop._accent(123, dwm) is accepted
    assert released == [456]
