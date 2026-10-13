"""Bridge commands/lifecycle without WebView2 or a real learning account."""
import json
import threading
import time

import pytest

from core.config import load_config
from core.sites import PRESETS, read_sites, site_config
from core.store import Store
from gui.web_interface import WebInterface
import gui.web_interface as bridge
from tests.test_core import sample


def ready_store(tmp_path):
    store = Store(tmp_path)
    store.set('account', {'id': 'a', 'name': '测试账号', 'cookie': 'PRIVATE_COOKIE'})
    store.set('course_catalog:a', [
        {'classroom_id': '10', 'name': '当前课程', 'class_name': '演示班级',
         'start': time.time() - 1000, 'end': time.time() + 1000, 'course_sign': 'PRIVATE_SIGN'},
        {'classroom_id': '11', 'name': '历史课程', 'start': time.time() - 5000, 'end': time.time() - 1000}])
    store.select_courses(['10'])
    store.merge_course('a', '10', [sample()])
    store.set('desired', 'idle')
    store.set('worker_busy', False)
    return store


def config_at(tmp_path):
    config = load_config()
    config.update(root=str(tmp_path), profile_dir=str(tmp_path / 'edge_profile'))
    return config


class FakeDog:
    def __init__(self, config=None, release=None):
        self.config = config
        self.alive = True
        self.stopped = False
        self.started = False
        self.release = release
        self.joining = threading.Event()

    def is_alive(self):
        return self.alive

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        if self.release is None:
            self.alive = False

    def join(self, timeout=None):
        self.joining.set()
        if self.release and self.release.wait(timeout):
            self.alive = False


def wait(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Bridge lifecycle did not finish')


def test_snapshot_whitelists_fields_and_scope(tmp_path):
    store = ready_store(tmp_path)
    store.update('a:10:1', evidence='PRIVATE_SERVER_PROOF', note='cookie=PRIVATE_NOTE https://x.test/view?token=PRIVATE_URL')
    store.set('current', {'course': '当前课程', 'title': '任务', 'playbackRate': 2,
                          'cookie': 'PRIVATE_CURRENT', 'course_sign': 'PRIVATE_CURRENT_SIGN'})
    store.merge_course('a', '11', [dict(sample(leaf='2'), classroom_id='11')])
    state = WebInterface(store).snapshot()
    text = json.dumps(state, ensure_ascii=False)
    assert 'PRIVATE_' not in text
    assert 'evidence' not in text and 'profile_dir' not in text
    assert state['counts']['tasks'] == 1
    assert state['current']['playbackRate'] == 2
    assert state['courses'][0]['selected']
    assert state['state']['primary']['action'] == 'start'
    assert state['tasks'][0]['progress'] == 0


def test_start_scans_implicitly_pause_resume_and_filters(tmp_path):
    store = ready_store(tmp_path)
    ui = WebInterface(store)
    assert ui.action('primary')['ok']
    assert store.get('desired') == 'run' and store.get('run_mode') == 'start'
    assert ui.snapshot()['state']['primary']['action'] == 'pause'
    assert not ui.action('select_courses', {'mode': 'clear'})['ok']
    assert store.selected_courses() == ['10']
    assert ui.action('primary')['ok'] and store.get('desired') == 'paused'
    assert ui.action('primary')['ok'] and store.get('run_mode') == 'resume'
    assert ui.action('task_filter', {'type': 'slides', 'unfinished_only': False})['ok']
    assert ui.snapshot()['filters'] == {'task_type': 'slides', 'unfinished_only': False}
    assert not ui.action('task_filter', {'type': '<script>'})['ok']


def test_empty_selection_never_runs_and_current_course_group(tmp_path):
    store = ready_store(tmp_path)
    ui = WebInterface(store)
    assert ui.action('select_courses', {'mode': 'clear'})['ok']
    response = ui.action('primary')
    assert response['navigate'] == 'courses'
    assert store.get('desired') == 'idle'
    assert not ui.action('start')['ok']
    assert ui.action('select_courses', {'mode': 'current'})['ok']
    assert store.selected_courses() == ['10']
    assert ui.action('select_course', {'id': '11', 'selected': True})['ok']
    assert store.selected_courses() == ['10', '11']
    assert not ui.action('select_course', {'id': 'foreign', 'selected': True})['ok']


def test_cancelled_first_login_can_connect_again(tmp_path):
    store = Store(tmp_path)
    store.set('desired', 'login')
    store.set('login_resume', {'action': 'catalog'})
    ui = WebInterface(store)
    assert ui.snapshot()['state']['phase'] == 'login_required'
    assert ui.snapshot()['state']['primary']['action'] == 'pause'
    ui.action('primary')
    assert store.get('login_resume') is None
    assert ui.snapshot()['state']['primary']['action'] == 'load_courses'
    ui.action('primary')
    assert store.get('desired') == 'catalog'
    assert store.get('scan_progress')['status'] == 'queued'


def test_all_failed_primary_retries_only_scoped_failed_tasks(tmp_path):
    store = ready_store(tmp_path)
    store.update('a:10:1', state='failed', attempts=3)
    store.merge_course('a', '11', [dict(sample(leaf='2'), classroom_id='11', state='failed', attempts=3)])
    ui = WebInterface(store)
    assert ui.snapshot()['state']['primary']['action'] == 'retry_failed'
    assert ui.action('primary')['ok']
    assert store.get('desired') == 'run'
    chosen = store.scoped_rows()[0]
    assert chosen['state'] == 'pending' and chosen['attempts'] == 0
    assert next(t for t in store.rows() if t['classroom_id'] == '11')['state'] == 'failed'


def test_retry_rejects_manual_foreign_or_nonfailed_and_never_completes(tmp_path):
    store = ready_store(tmp_path)
    ui = WebInterface(store)
    assert not ui.action('retry_tasks', {'ids': ['a:10:1']})['ok']
    assert not ui.action('retry_tasks', {'ids': ['foreign']})['ok']
    assert not ui.action('set', {'state': 'completed', 'evidence': 'fake'})['ok']
    assert store.rows()[0]['state'] == 'pending'


def test_scan_error_empty_state_does_not_claim_completion(tmp_path):
    store = ready_store(tmp_path)
    store.set('scan_progress', {'status': 'failed', 'message': '课程读取失败'})
    store.set('scan_errors', [{'course': '当前课程', 'reason': 'HTTP 400', 'cookie': 'PRIVATE_ERROR'}])
    store.set('workflow', {'phase': 'error', 'message': '请重试'})
    state = WebInterface(store).snapshot()
    assert state['state']['phase'] == 'error'
    assert state['counts']['completed'] == 0 and state['counts']['errors'] == 1
    assert '未成功完成' in state['empty']['tasks']
    assert state['scan_errors'] == [{'course': '当前课程', 'message': 'HTTP 400'}]


def test_pausing_and_unknown_total_scan_are_visible(tmp_path):
    store = ready_store(tmp_path)
    store.set('desired', 'scan')
    store.set('scan_progress', {'status': 'queued', 'total': 0, 'completed': 0})
    ui = WebInterface(store)
    state = ui.snapshot()
    assert state['state']['phase'] == 'scanning'
    assert state['progress']['indeterminate']
    ui.action('pause')
    assert store.get('scan_progress')['status'] == 'cancelled'
    store.set('worker_busy', True)
    assert ui.snapshot()['state']['phase'] == 'pausing'
    assert not ui.snapshot()['state']['primary']['enabled']


@pytest.mark.parametrize('payload', [None, {'url': 'https://x:y@example.com/web'}, {'url': 'https://example.com/web?token=x'}, {'url': 12}])
def test_invalid_site_never_stops_existing_worker(tmp_path, payload):
    config = config_at(tmp_path)
    dog = FakeDog()
    ui = WebInterface(ready_store(tmp_path), dog, base_config=config)
    # None has no URL and is rejected rather than interpreted as a restart.
    response = ui.action('switch_site', payload)
    assert not response['ok']
    assert not dog.stopped and not ui.switching


def test_switch_waits_old_worker_and_keeps_snapshot_responsive(tmp_path):
    config = config_at(tmp_path)
    store = ready_store(tmp_path)
    release = threading.Event()
    old = FakeDog(release=release)
    created = []
    def factory(cfg):
        dog = FakeDog(cfg)
        created.append(dog)
        return dog
    ui = WebInterface(store, old, base_config=config, watchdog_factory=factory)
    try:
        assert ui.action('switch_site', {'url': PRESETS[1]})['ok']
        assert old.joining.wait(1)
        assert not created
        assert ui.snapshot()['state']['phase'] == 'switching'
        assert not ui.action('start')['ok']
        assert not (tmp_path / 'sites').exists()
        release.set()
        wait(lambda: not ui.switching)
        assert ui.active_site == PRESETS[1] and created[0].started
        assert ui.store.selected_courses() == [] and ui.store.rows() == []
        assert read_sites(config)[0] == PRESETS[1]
        assert ui.action('switch_site', {'url': PRESETS[0]})['ok']
        wait(lambda: not ui.switching)
        assert ui.store.path == store.path
        assert ui.store.selected_courses() == ['10']
    finally:
        release.set()
        ui.close()
        wait(lambda: ui.closed)


def test_close_during_switch_aborts_new_worker(tmp_path):
    config = config_at(tmp_path)
    release = threading.Event()
    dog = FakeDog(release=release)
    created = []
    ui = WebInterface(ready_store(tmp_path), dog, base_config=config,
                      watchdog_factory=lambda cfg: created.append(cfg))
    class Window:
        destroyed = False
        def destroy(self):
            self.destroyed = True
    window = Window()
    ui.bind_window(window)
    ui.action('switch_site', {'url': PRESETS[1]})
    assert dog.joining.wait(1)
    assert ui.on_closing() is False
    release.set()
    wait(lambda: ui.closed)
    assert window.destroyed and not created
    assert read_sites(config)[0] == PRESETS[0]
    assert ui.on_closing() is True


def test_stopped_watchdog_has_recovery_and_start_failure_is_readable(tmp_path):
    config = config_at(tmp_path)
    dog = FakeDog();dog.alive = False
    def fail(config):
        raise OSError('PRIVATE_PROFILE_PATH')
    ui = WebInterface(ready_store(tmp_path), dog, base_config=config, watchdog_factory=fail)
    assert ui.snapshot()['state']['primary']['action'] == 'recover'
    assert ui.action('primary')['ok']
    wait(lambda: not ui.switching)
    assert 'PRIVATE_' not in json.dumps(ui.snapshot())
    assert ui.snapshot()['state']['primary']['action'] == 'recover'
    ui.close();wait(lambda: ui.closed)


def test_concurrent_toggle_keeps_valid_selection(tmp_path):
    store = ready_store(tmp_path)
    ui = WebInterface(store)
    threads = [threading.Thread(target=ui.action, args=('course_toggle', {'id': '11'})) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.selected_courses() == ['10']
    assert not ui.action('start', ['invalid'])['ok']


def test_packaged_version_is_read_from_application_root(tmp_path, monkeypatch):
    (tmp_path / 'VERSION.txt').write_text('0.6.0\n', encoding='utf8')
    monkeypatch.setattr(bridge, 'app_root', lambda: tmp_path)
    assert WebInterface(Store(tmp_path)).snapshot()['version'] == '0.6.0'


def test_new_worker_exits_after_settings_failure_without_locking_api(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    store = ready_store(tmp_path)
    release = threading.Event()
    old = FakeDog()
    new = FakeDog(release=release)
    def failed_save(*args):
        raise OSError('PRIVATE_SETTINGS_PATH')
    monkeypatch.setattr(bridge, 'save_site', failed_save)
    ui = WebInterface(store, old, base_config=config, watchdog_factory=lambda cfg: new)
    try:
        ui.action('switch_site', {'url': PRESETS[1]})
        assert new.joining.wait(1)
        assert new.stopped
        assert ui.snapshot()['state']['phase'] == 'switching'
        release.set()
        wait(lambda: not ui.switching)
        assert ui.store is store and ui.watchdog is old
        assert ui.active_site == PRESETS[0] and store.selected_courses() == ['10']
        assert 'PRIVATE_' not in json.dumps(ui.snapshot())
    finally:
        release.set()
        ui.close();wait(lambda: ui.closed)
