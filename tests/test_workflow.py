"""一键流程的回归测试：真实 Store / 扫描器 / 调度器，网站操作由夹具提供。"""
import asyncio

import pytest

from core.config import load_config
from core.errors import AdaptationRequired, Interrupted, LoginRequired
from core.logging_setup import make_logger
from core.store import Store
from core.workflow import Workflow
from tests.test_core import sample


class Browser:
    def __init__(self):
        self.page = self
        self.frames = ()
        self.pauses = 0
        self.closed = False
        self.starts = []

    async def goto(self, *args, **kwargs):
        pass

    async def pause_media(self):
        self.pauses += 1

    async def start(self, headed=False):
        self.starts.append(headed)

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed

    async def evaluate(self, script):
        return 'complete'


class Adapter:
    def __init__(self):
        self.user = 'a'
        self.account_id = None
        self.failures = set()
        self.scanned = []
        self.account_hook = None
        self.scan_hook = None
        self.logged_in = True

    async def account(self):
        if self.account_hook:
            self.account_hook()
        if not self.logged_in:
            raise LoginRequired('请登录')
        if self.account_id and self.account_id != self.user:
            raise LoginRequired('账号已切换')
        self.account_id = self.user
        return {'id': self.user, 'name': '测试账号'}

    async def courses(self):
        return [{'classroom_id': cid, 'name': '测试课程'} for cid in ('10', '11')]

    async def course_metadata(self, course):
        return dict(course, start=100, end=200)

    async def scan_course(self, course):
        cid = course['classroom_id']
        self.scanned.append(cid)
        if self.scan_hook:
            self.scan_hook()
        if cid in self.failures:
            raise AdaptationRequired('课程接口失败')
        return [dict(sample(self.user), classroom_id=cid, key=f'{self.user}:{cid}:1')]


@pytest.fixture
def flow(tmp_path):
    config = load_config()
    config.update(root=str(tmp_path), scan_concurrency=1)
    store = Store(tmp_path)
    store.set('account', {'id': 'a', 'name': '测试账号'})
    store.select_courses(['10'], 'a')
    store.set('desired', 'run')
    return Workflow(Adapter(), Browser(), store, config,
                    make_logger(tmp_path, 'workflow-' + tmp_path.name))


def test_start_scans_latest_selected_tasks_then_executes_with_server_evidence(flow, monkeypatch):
    calls = []
    flow.store.merge_course('a', '11', [dict(sample(), key='a:11:1', classroom_id='11')])
    # 即便上次的范围被标记扫描完成，重新开始仍读取最新服务器任务。
    flow.store.set('scanned_account', 'a')
    flow.store.set('scanned_courses', ['10'])

    async def execute(task, *args):
        calls.append(task['key'])
        assert flow.store.get('workflow')['phase'] == 'running'
        assert flow.adapter.scanned == ['10']
        return '{"server": true}'

    monkeypatch.setattr('task.scheduler.execute', execute)
    asyncio.run(flow.perform('run'))
    assert calls == ['a:10:1']
    rows = {task['key']: task for task in flow.store.rows('a')}
    assert rows['a:10:1']['state'] == 'completed'
    assert rows['a:11:1']['state'] == 'pending'
    assert flow.store.get('desired') == 'idle'
    assert flow.store.get('workflow')['phase'] == 'ready'
    assert flow.store.get('run_mode') == 'resume'


def test_resume_of_valid_scope_executes_without_rescanning(flow, monkeypatch):
    flow.store.merge_course('a', '10', [sample()])
    flow.store.set('scanned_account', 'a')
    flow.store.set('scanned_courses', ['10'])
    flow.store.set('scan_progress', {'status': 'completed'})
    flow.store.set('run_mode', 'resume')
    calls = []

    async def execute(task, *args):
        calls.append(task['key'])
        return '{"server": true}'

    monkeypatch.setattr('task.scheduler.execute', execute)
    asyncio.run(flow.perform('run'))
    assert flow.adapter.scanned == []
    assert calls == ['a:10:1']
    assert flow.store.rows('a')[0]['state'] == 'completed'


@pytest.mark.parametrize('invalid', ['selection_changed', 'partial', 'failed', 'cancelled'])
def test_resume_with_changed_scope_or_incomplete_scan_must_rescan(flow, monkeypatch, invalid):
    flow.store.set('scanned_account', 'a')
    flow.store.set('scanned_courses', ['10'])
    flow.store.set('scan_progress', {'status': 'completed' if invalid == 'selection_changed' else invalid})
    flow.store.set('run_mode', 'resume')
    if invalid == 'selection_changed':
        flow.store.select_courses(['11'], 'a')
    calls = []

    async def execute(task, *args):
        calls.append(task['classroom_id'])
        return '{"server": true}'

    monkeypatch.setattr('task.scheduler.execute', execute)
    asyncio.run(flow.perform('run'))
    expected = ['11'] if invalid == 'selection_changed' else ['10']
    assert flow.adapter.scanned == expected
    assert calls == expected
    assert flow.store.get('scan_progress')['status'] == 'completed'


@pytest.mark.parametrize('mode', [None, 'start'])
def test_start_or_legacy_run_without_mode_refreshes_even_a_valid_scan(flow, monkeypatch, mode):
    flow.store.set('scanned_account', 'a')
    flow.store.set('scanned_courses', ['10'])
    flow.store.set('scan_progress', {'status': 'completed'})
    if mode is not None:
        flow.store.set('run_mode', mode)

    async def execute(task, *args):
        return '{"server": true}'

    monkeypatch.setattr('task.scheduler.execute', execute)
    asyncio.run(flow.perform('run'))
    assert flow.adapter.scanned == ['10']


@pytest.mark.parametrize('failures', [('10',), ('10', '11')])
def test_incomplete_scan_never_runs_old_or_partial_queue(flow, monkeypatch, failures):
    flow.store.select_courses(['10', '11'], 'a')
    flow.store.merge_course('a', '10', [sample()])
    flow.adapter.failures = set(failures)

    async def forbidden(*args):
        pytest.fail('扫描不完整时不应执行任何任务')

    monkeypatch.setattr('core.workflow.run_queue', forbidden)
    asyncio.run(flow.perform('run'))
    assert flow.store.get('desired') == 'paused'
    assert not flow.store.scope_scanned('a')
    assert flow.store.get('workflow')['phase'] == 'error'
    assert not any(t['state'] == 'completed' for t in flow.store.rows('a'))


def test_empty_selection_never_means_all_courses(flow):
    flow.store.select_courses([], 'a')
    asyncio.run(flow.perform('run'))
    assert flow.adapter.scanned == []
    assert flow.store.get('desired') == 'idle'
    assert '选择' in flow.store.get('workflow')['message']


@pytest.mark.parametrize('action', ['catalog', 'scan', 'run'])
def test_same_account_login_resumes_original_action_after_worker_restart(flow, action):
    flow.store.set('desired', action)
    assert flow.request_login(action, '登录失效')
    flow.adapter.logged_in = False
    assert not asyncio.run(flow.poll_login())
    assert flow.store.get('desired') == 'login'
    resumed = Workflow(flow.adapter, flow.browser, Store(flow.config['root']), flow.config, flow.logger)
    flow.adapter.logged_in = True
    assert asyncio.run(resumed.poll_login())
    assert flow.store.get('desired') == action
    assert flow.store.get('login_resume') is None


@pytest.mark.parametrize('while_waiting', [False, True])
def test_changed_account_refreshes_catalog_without_reusing_run_intent(flow, while_waiting):
    flow.store.select_courses(['11'], 'b')
    flow.adapter.user = 'b'
    if while_waiting:
        flow.request_login('run', '请登录')
        asyncio.run(flow.poll_login())
    else:
        asyncio.run(flow.perform('run'))
    assert flow.store.get('desired') == 'catalog'
    assert flow.adapter.scanned == []
    assert flow.store.get('account')['id'] == 'b'
    asyncio.run(flow.perform('catalog'))
    assert flow.store.get('desired') == 'idle'
    assert flow.adapter.scanned == []
    assert flow.store.selected_courses('a') == ['10']
    assert flow.store.selected_courses('b') == ['11']


@pytest.mark.parametrize('stop', ['paused', 'shutdown', 'catalog'])
def test_pause_close_or_new_action_during_login_probe_wins(flow, stop):
    flow.request_login('run', '请登录')
    flow.adapter.account_hook = lambda: flow.store.set('desired', stop)
    with pytest.raises(Interrupted):
        asyncio.run(flow.poll_login())
    assert flow.store.get('desired') == stop
    assert flow.adapter.scanned == []


@pytest.mark.parametrize('stop', ['paused', 'shutdown'])
def test_login_error_cannot_overwrite_stop_requested_during_network_call(flow, stop):
    flow.store.set('desired', stop)
    assert not flow.request_login('run', '迟到的网络响应')
    assert flow.store.get('desired') == stop
    assert flow.store.get('login_resume') is None


def test_pause_during_scan_never_starts_execution(flow, monkeypatch):
    flow.adapter.scan_hook = lambda: flow.store.set('desired', 'paused')

    async def forbidden(*args):
        pytest.fail('暂停扫描后不应开始执行')

    monkeypatch.setattr('core.workflow.run_queue', forbidden)
    with pytest.raises(Interrupted):
        asyncio.run(flow.perform('run'))
    assert flow.store.get('desired') == 'paused'
    assert flow.store.get('scan_progress')['status'] == 'cancelled'


def test_visible_security_challenge_does_not_auto_resume(flow, monkeypatch):
    async def visible(frame):
        return True

    monkeypatch.setattr('core.workflow.visible_security_challenge', visible)
    flow.browser.frames = [object()]
    flow.request_login('run', '请完成验证')
    assert not asyncio.run(flow.poll_login())
    assert flow.store.get('desired') == 'login'
    assert '验证' in flow.store.get('workflow')['message']


def test_headed_recovery_of_headless_config_waits_for_login_without_restart_loop(flow, monkeypatch):
    from worker import worker_async

    flow.config['headless'] = True
    flow.store.set('force_headed', True)
    calls = 0

    def login_first_call_fails():
        nonlocal calls
        calls += 1
        flow.adapter.logged_in = calls > 1

    flow.adapter.account_hook = login_first_call_fails
    monkeypatch.setattr('worker.EdgeManager', lambda config: flow.browser)
    monkeypatch.setattr('worker.SiteAdapter', lambda browser, config: flow.adapter)

    async def finish(*args):
        assert flow.adapter.scanned == ['10']
        flow.store.set('desired', 'shutdown')

    monkeypatch.setattr('core.workflow.run_queue', finish)
    asyncio.run(asyncio.wait_for(worker_async(flow.config), timeout=5))
    assert calls >= 3
    assert flow.browser.starts == [True]
    assert flow.browser.closed
    assert flow.store.get('desired') == 'shutdown'


@pytest.mark.parametrize('pause_before_login', [False, True])
def test_real_edge_worker_login_then_one_click_scan_execute_or_keep_paused(tmp_path, monkeypatch, pause_before_login):
    """完整工作循环连接本地网站，不访问真实账号；正常登录页面写 Cookie。"""
    from browser.edge_manager import EdgeManager
    from tests.mock_site import MockSite
    from tests.test_edge import config_for
    from worker import worker_async

    async def wait_for(predicate, timeout=35):
        async def polling():
            while not predicate():
                await asyncio.sleep(.05)
        await asyncio.wait_for(polling(), timeout)

    async def scenario(site):
        config = config_for(tmp_path, site.url)
        config['headless'] = False  # 已是可见登录窗口，worker 无需交由 watchdog 重启。
        store = Store(tmp_path)
        store.set('account', {'id': '7', 'name': '本地验收账号'})
        store.select_courses(['10'], '7')
        store.set('desired', 'run')
        browser = EdgeManager(config)
        monkeypatch.setattr('worker.EdgeManager', lambda config: browser)
        process = asyncio.create_task(worker_async(config))
        try:
            await wait_for(lambda: store.get('desired') == 'login')
            assert store.get('login_resume')['action'] == 'run'
            assert not site.finished
            if pause_before_login:
                store.set('desired', 'paused')
                await wait_for(lambda: store.get('login_resume') is None)
            await browser.page.goto(site.url + '/login', wait_until='domcontentloaded')
            if pause_before_login:
                # 跨过一个完整登录探测周期，证明旧的运行意图不会复活。
                await asyncio.sleep(3.5)
                assert store.get('desired') == 'paused'
                assert not site.finished
                assert not store.rows('7')
                assert not any('/course/chapter' in path for method, path in site.requests)
            else:
                await wait_for(lambda: store.get('desired') == 'idle')
                assert set(site.finished) == {'1', '2'}
                assert len(store.rows('7')) == 2
                assert all(task['state'] == 'completed' and task['evidence'] for task in store.rows('7'))
                assert store.get('scan_progress')['status'] == 'completed'
                assert ('GET', '/mooc-api/v1/lms/learn/course/chapter') in site.requests
                assert [(method, path) for method, path in site.requests if method == 'POST'] == [
                    ('POST', '/observed-read'), ('POST', '/observed-ended')]
        finally:
            store.set('desired', 'shutdown')
            await asyncio.wait_for(process, timeout=15)
        assert browser.context is None

    with MockSite() as site:
        asyncio.run(scenario(site))
