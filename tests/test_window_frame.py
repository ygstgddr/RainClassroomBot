"""Bound window controls keep learning state and graceful close intact."""
import threading

import pytest

from core.store import Store
from gui.web_interface import WebInterface
from gui.window_frame import WindowFrame, work_area_metrics
from tests.test_web_bridge import FakeDog, ready_store, wait


class FakeWindow:
    maximized = False
    def __init__(self):
        self.calls = []
        self.destroyed = False

    def minimize(self):
        self.calls.append('minimize')

    def maximize(self):
        self.calls.append('maximize')

    def restore(self):
        self.calls.append('restore')

    def destroy(self):
        self.destroyed = True


def bound(tmp_path):
    store = ready_store(tmp_path)
    ui = WebInterface(store)
    window = FakeWindow()
    ui.bind_window(window)
    ui.window_frame._ready = True
    return ui, window


def test_controls_are_disabled_without_a_bound_ready_native_window(tmp_path):
    ui = WebInterface(Store(tmp_path), demo=True)
    assert ui.snapshot()['window'] == {'maximized': False, 'native_controls': False}
    assert not ui.action('window_minimize')['ok']
    ui.bind_window(FakeWindow())
    assert not ui.snapshot()['window']['native_controls']
    assert not ui.action('window_minimize')['ok']


def test_minimize_maximize_restore_do_not_change_learning_state(tmp_path):
    ui, window = bound(tmp_path)
    ui.store.set('desired', 'run')
    ui.switching = True
    assert ui.action('window_minimize')['ok']
    assert ui.action('window_toggle_maximize')['window']['maximized']
    assert not ui.action('window_toggle_maximize')['window']['maximized']
    assert window.calls == ['minimize', 'maximize', 'restore']
    assert ui.store.get('desired') == 'run'
    assert ui.store.selected_courses() == ['10']


def test_resize_has_fixed_directions_and_no_arbitrary_native_access(tmp_path, monkeypatch):
    ui, window = bound(tmp_path)
    edges = []
    monkeypatch.setattr(ui.window_frame, 'resize', lambda edge: edges.append(edge))
    assert ui.action('window_resize', {'edge': 'se'})['ok']
    assert edges == ['se']
    for name, payload in [
        ('window_resize', {'edge': 'e', 'hwnd': 123}),
        ('window_resize', {'x': 1, 'y': 2}),
        ('window_resize', {'edge': ['e']}),
        ('window_minimize', {'hwnd': 123}),
        ('window_execute', {'command': 'shell'}),
    ]:
        assert not ui.action(name, payload)['ok']
    assert edges == ['se']
    with pytest.raises(ValueError):
        WindowFrame(window).resize('other')


def test_maximized_native_resize_is_rejected(tmp_path):
    ui, window = bound(tmp_path)
    ui.action('window_toggle_maximize')
    assert not ui.action('window_resize', {'edge': 'e'})['ok']


def test_window_close_waits_for_watchdog_and_is_idempotent(tmp_path):
    release = threading.Event()
    dog = FakeDog(release=release)
    ui = WebInterface(ready_store(tmp_path), dog)
    window = FakeWindow()
    ui.bind_window(window)
    ui.window_frame._ready = True
    ui.switching = True
    assert ui.action('window_close')['ok']
    assert dog.joining.wait(1)
    assert dog.stopped and not window.destroyed
    assert ui.action('window_close')['ok']
    assert not ui.action('window_minimize')['ok']
    release.set()
    wait(lambda: ui.closed and window.destroyed)
    assert ui.store.selected_courses() == ['10']
    assert ui.on_closing() is True


def test_work_area_handles_taskbars_and_negative_monitor_coordinates():
    assert work_area_metrics((0, 0, 2560, 1440), (0, 0, 2560, 1380)) == (0, 0, 2560, 1380)
    assert work_area_metrics((-1920, 0, 0, 1080), (-1880, 0, 0, 1080)) == (40, 0, 1880, 1080)
    assert work_area_metrics((0, -1080, 1920, 0), (0, -1040, 1920, 0)) == (0, 40, 1920, 1040)
