"""Cold startup is passive; only verified responses unlock cached course data."""
import asyncio
from core.sites import PRESETS, prepare_site
from core.workflow import Workflow
from core.logging_setup import make_logger
from gui.web_interface import WebInterface
from tests.test_web_bridge import ready_store, config_at, FakeDog, wait
from tests.test_workflow import Adapter, Browser


def setup_ui(tmp_path, factory=None):
    config = config_at(tmp_path)
    ready_store(tmp_path)
    store = prepare_site(config, connect=False)
    ui = WebInterface(store, base_config=config, watchdog_factory=factory or FakeDog, defer_login=True)
    return ui, config


def test_cached_account_cannot_start_browser_or_tasks_before_explicit_login(tmp_path):
    created = []
    ui, config = setup_ui(tmp_path, lambda cfg: created.append(cfg))
    state = ui.snapshot()
    assert state['state']['login_gate'] and state['state']['login_status'] == 'idle'
    assert not state['account']['logged_in'] and ui.watchdog is None and not created
    for action in ('start', 'load_courses', 'typing_select', 'open_browser'):
        assert not ui.action(action)['ok']
    assert ui.store.selected_courses() == ['10']
    ui.close(); wait(lambda: ui.closed)
    assert not created


def test_explicit_login_is_idempotent_and_requires_live_session_confirmation(tmp_path):
    created = []
    def factory(cfg):
        dog = FakeDog(cfg); created.append(dog); return dog
    ui, config = setup_ui(tmp_path, factory)
    try:
        assert ui.action('login')['ok']
        wait(lambda: not ui.switching)
        assert len(created) == 1 and created[0].started
        assert created[0].config['headless'] is False
        assert ui.snapshot()['state']['login_gate']
        assert ui.action('login')['ok'] and len(created) == 1
        ui.store.set('authenticated_session', 'old-session')
        assert ui.snapshot()['state']['login_gate']
        flow = Workflow(Adapter(), Browser(), ui.store, created[0].config, make_logger(config['root'], 'test'))
        asyncio.run(flow.perform('catalog'))
        state = ui.snapshot()
        assert not state['state']['login_gate'] and state['account']['logged_in']
        assert ui.store.selected_courses() == ['10']
        ui.store.set('desired', 'catalog')
        flow.request_login('catalog', 'Expired')
        assert ui.snapshot()['state']['login_gate']
        assert ui.store.get('authenticated_session') is None
    finally:
        ui.close(); wait(lambda: ui.closed)


def test_site_selection_before_login_does_not_create_worker(tmp_path):
    created = []
    ui, config = setup_ui(tmp_path, lambda cfg: created.append(cfg))
    assert ui.action('switch_site', {'url': PRESETS[1]})['ok']
    wait(lambda: not ui.switching)
    assert ui.active_site == PRESETS[1] and not created and ui.watchdog is None
    assert ui.snapshot()['state']['login_status'] == 'idle'
    ui.close(); wait(lambda: ui.closed)


def test_failed_launch_can_retry_and_closing_never_launches_a_worker(tmp_path):
    def failed(cfg): raise OSError('private path')
    ui, _ = setup_ui(tmp_path, failed)
    ui.action('login'); wait(lambda: not ui.switching)
    assert ui.snapshot()['state']['login_status'] == 'error'
    ui.watchdog_factory = FakeDog
    ui.action('login'); wait(lambda: not ui.switching)
    assert ui.watchdog.started
    ui.close(); wait(lambda: ui.closed)
    assert not ui.action('login')['ok']


def test_connection_error_retries_even_when_worker_is_still_alive(tmp_path):
    created = []
    def factory(cfg):
        dog = FakeDog(cfg); created.append(dog); return dog
    ui, _ = setup_ui(tmp_path, factory)
    try:
        ui.action('login'); wait(lambda: not ui.switching)
        ui.store.set('workflow', {'phase': 'error', 'message': 'Connection failed'})
        assert ui.store.get('desired') == 'catalog' and ui.watchdog.is_alive()
        assert ui.snapshot()['state']['login_status'] == 'error'
        ui.action('login'); wait(lambda: not ui.switching)
        assert len(created) == 2 and created[1].started
        assert not created[0].is_alive()
        assert ui.snapshot()['state']['login_status'] == 'connecting'
    finally:
        ui.close(); wait(lambda: ui.closed)


def test_can_switch_site_while_waiting_for_login_without_opening_another_edge(tmp_path):
    ui, _ = setup_ui(tmp_path)
    try:
        ui.action('login'); wait(lambda: not ui.switching)
        dog = ui.watchdog
        ui.store.set('desired', 'login')
        ui.store.set('worker_busy', True)
        state = ui.snapshot()['state']
        assert state['login_status'] == 'waiting' and not state['selection_locked']
        assert ui.action('switch_site', {'url': PRESETS[1]})['ok']
        wait(lambda: not ui.switching)
        assert not dog.is_alive() and ui.watchdog is None
        assert ui.snapshot()['state']['login_status'] == 'idle'
    finally:
        ui.close(); wait(lambda: ui.closed)
