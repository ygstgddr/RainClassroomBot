import asyncio
import pytest
from core.config import load_config
from core.store import Store
from core.errors import AdaptationRequired, Interrupted, LoginRequired
from core.logging_setup import make_logger
from task.scanner import scan
from task.scheduler import run_queue
from tests.test_core import sample


class FakeAdapter:
    def __init__(self, store, failures=()):
        self.store, self.failures = store, failures

    async def account(self):
        return {'id': 'a', 'name': '测试'}

    async def courses(self):
        return [{'classroom_id': str(i), 'name': f'课程{i}'} for i in (10, 11)]

    async def scan_course(self, course):
        cid = course['classroom_id']
        self.scan_progress(course, '读取服务器进度')
        p = self.store.get('scan_progress')
        assert p['current'][0]['phase'] == '读取服务器进度'
        if cid == '11':
            assert p['completed'] == 1
            assert len(self.store.get('scan_errors')) == int('10' in self.failures)
        if cid in self.failures:
            raise AdaptationRequired('接口 HTTP 400')
        t = sample()
        t.update(classroom_id=cid, key=f'a:{cid}:1')
        return [t]


def setup(tmp_path):
    store = Store(tmp_path)
    store.set('desired', 'scan')
    store.select_courses(['10', '11'], 'a')
    config = load_config()
    config.update(root=str(tmp_path), scan_concurrency=1)
    return store, config, make_logger(tmp_path, 'scan-regression-' + tmp_path.name)


def test_partial_scan_reports_errors_and_incremental_progress(tmp_path):
    store, config, logger = setup(tmp_path)
    result = asyncio.run(scan(FakeAdapter(store, ('10',)), store, config, logger))
    assert result['status'] == 'partial'
    assert (result['completed'], result['failed'], result['tasks']) == (2, 1, 1)
    assert len(store.rows()) == 1
    assert store.get('scan_progress')['current'] == []


def test_all_failed_scan_is_not_empty_success_and_cannot_execute(tmp_path):
    store, config, logger = setup(tmp_path)
    result = asyncio.run(scan(FakeAdapter(store, ('10', '11')), store, config, logger))
    assert result['status'] == 'failed'
    assert store.get('scanned_account') is None
    store.set('desired', 'run')
    asyncio.run(run_queue(None, None, store, config, logger))
    assert store.get('desired') == 'paused'
    assert '无法判断' in result['message']


def test_cancel_scan_has_terminal_progress(tmp_path):
    store, config, logger = setup(tmp_path)
    store.set('desired', 'paused')
    with pytest.raises(Interrupted):
        asyncio.run(scan(FakeAdapter(store), store, config, logger))
    assert store.get('scan_progress')['status'] == 'cancelled'
    assert store.get('scan_progress')['completed'] == 0


def test_login_failure_is_visible_not_spinning_forever(tmp_path):
    store, config, logger = setup(tmp_path)
    class LoggedOut(FakeAdapter):
        async def account(self):
            raise LoginRequired('请登录')
    with pytest.raises(LoginRequired):
        asyncio.run(scan(LoggedOut(store), store, config, logger))
    assert store.get('scan_progress')['status'] == 'failed'
    assert store.get('scan_progress')['message'] == '请登录'


def test_empty_chapter_skips_inapplicable_schedule():
    from browser.site_adapter import SiteAdapter
    class EmptyAdapter(SiteAdapter):
        def __init__(self):
            super().__init__(None, {})
        async def context(self, cid):
            return {'cid': cid, 'classroom_id': cid, 'sign': 'test-sign'}
        async def get(self, path, params=None, headers=None, auth_probe=False):
            assert path.endswith('/chapter')
            return {'course_chapter': []}
        async def schedule(self, cid):
            raise AssertionError('空课程不应调用进度接口')
    assert asyncio.run(EmptyAdapter().scan_course({'classroom_id': '1', 'name': '空课程'})) == []
