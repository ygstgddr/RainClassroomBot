"""请求/磁盘优化回归：仍以新服务器响应为准，暂停不等待慢查询。"""
import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from browser.site_adapter import SiteAdapter
from core.errors import Interrupted
from core.store import Store
from task.executor import execute
from tests.test_core import sample


def test_unchanged_store_values_do_not_commit_or_change_updated(tmp_path):
    store = Store(tmp_path)
    store.merge_course('a', '10', [sample()])
    store.set('current', {'phase': 'playing'})
    updated = store.rows('a')[0]['updated']
    with sqlite3.connect(store.path) as observer:
        initial_version = observer.execute('PRAGMA data_version').fetchone()[0]
        for _ in range(20):
            store.set('current', {'phase': 'playing'})
            store.update('a:10:1', duration=None, progress=0)
        assert observer.execute('PRAGMA data_version').fetchone()[0] == initial_version
        assert store.rows('a')[0]['updated'] == updated
        store.update('a:10:1', progress=.5)
        assert observer.execute('PRAGMA data_version').fetchone()[0] != initial_version
        assert store.rows('a')[0]['progress'] == .5


def test_store_conditional_write_observes_other_process_changes(tmp_path):
    first, second = Store(tmp_path), Store(tmp_path)
    first.set('desired', 'run')
    second.set('desired', 'paused')
    first.set('desired', 'run')
    assert second.get('desired') == 'run'
    with pytest.raises(ValueError):
        first.update('a:10:1', state='completed')


def test_course_dates_and_chapters_reuse_same_details_without_caching_completion():
    class Adapter(SiteAdapter):
        def __init__(self):
            super().__init__(None, {})
            self.paths = []

        async def get(self, path, *args, **kwargs):
            self.paths.append(path)
            if '/classrooms/' in path:
                return {'course_sign': 'local-sign', 'uv_id': 1, 'class_start': 1700000000}
            if path.endswith('/chapter'):
                return {'course_chapter': [{'section_leaf_list': [{'id': 1, 'leaf_type': 0}]}]}
            if path.endswith('/schedule'):
                return {'leaf_schedules': {'1': 0}}
            raise AssertionError(path)

    async def scenario():
        adapter = Adapter()
        course = {'classroom_id': '10', 'name': 'local course'}
        await adapter.course_metadata(course)
        assert (await adapter.scan_course(course))[0]['state'] == 'pending'
        await adapter.schedule('10')
        assert adapter.paths.count('/v2/api/web/classrooms/10') == 1
        assert adapter.paths.count('/mooc-api/v1/lms/learn/course/schedule') == 2
        assert adapter.course_headers['10'] == {'university-id': '1'}
    asyncio.run(scenario())


def harness(tmp_path, monkeypatch, adapter, tick):
    store = Store(tmp_path)
    store.set('desired', 'run')
    task = sample()
    store.merge_course('a', '10', [task])
    page = SimpleNamespace(is_closed=lambda: False)
    browser = SimpleNamespace(page=page, paused=False)

    async def open_task(task):
        return page

    async def pause_media():
        browser.paused = True

    class Handler:
        def __init__(self, *args):
            pass

        async def tick(self):
            return await tick(store)

    browser.open_task, browser.pause_media = open_task, pause_media
    monkeypatch.setattr('task.executor.VideoHandler', Handler)
    config = dict(poll_seconds=.03, navigation_timeout_seconds=1,
                  task_timeout_seconds=10, confirmation_timeout_seconds=2)
    return store, task, browser, config


def test_media_monitor_and_pause_remain_responsive_during_slow_status(tmp_path, monkeypatch):
    async def scenario():
        ticks = []

        class Adapter:
            def __init__(self):
                self.calls = 0
                self.query_started = asyncio.Event()
                self.cancelled = False

            async def status(self, task):
                self.calls += 1
                if self.calls > 1:
                    self.query_started.set()
                    try:
                        await asyncio.sleep(3)
                    except asyncio.CancelledError:
                        self.cancelled = True
                        raise
                return False, 0, ''

        async def tick(store):
            ticks.append(time.monotonic())
            return {'phase': 'playing', 'ready': False, 'duration': 30}

        adapter = Adapter()
        store, task, browser, config = harness(tmp_path, monkeypatch, adapter, tick)
        running = asyncio.create_task(execute(task, adapter, browser, store, config))
        await asyncio.wait_for(adapter.query_started.wait(), .5)
        before = len(ticks)
        await asyncio.sleep(.12)
        assert len(ticks) >= before + 2
        store.set('desired', 'paused')
        with pytest.raises(Interrupted):
            await asyncio.wait_for(running, .2)
        assert adapter.cancelled and browser.paused
        assert store.rows('a')[0]['state'] == 'pending'
        assert adapter.calls == 2  # 不并发积累进度请求。
    asyncio.run(scenario())


def test_preflight_is_not_immediately_repeated_after_opening_page(tmp_path, monkeypatch):
    async def scenario():
        class Adapter:
            calls = 0

            async def status(self, task):
                self.calls += 1
                return False, 0, ''

        async def tick(store):
            store.set('desired', 'paused')
            return {'phase': 'playing', 'ready': False}

        adapter = Adapter()
        store, task, browser, config = harness(tmp_path, monkeypatch, adapter, tick)
        config['poll_seconds'] = 5
        with pytest.raises(Interrupted):
            await execute(task, adapter, browser, store, config)
        assert adapter.calls == 1
    asyncio.run(scenario())


def test_pause_during_preflight_cancels_request_without_opening_task(tmp_path, monkeypatch):
    async def scenario():
        class Adapter:
            started = asyncio.Event()
            cancelled = False

            async def status(self, task):
                self.started.set()
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                return True, 1, 'must not be returned after pause'

        async def forbidden(store):
            pytest.fail('暂停后不应启动媒体处理')

        adapter = Adapter()
        store, task, browser, config = harness(tmp_path, monkeypatch, adapter, forbidden)

        async def forbidden_open(task):
            pytest.fail('开始前检查中暂停，不应打开学习任务')

        browser.open_task = forbidden_open
        running = asyncio.create_task(execute(task, adapter, browser, store, config))
        await asyncio.wait_for(adapter.started.wait(), .5)
        store.set('desired', 'paused')
        with pytest.raises(Interrupted):
            await asyncio.wait_for(running, .25)
        assert adapter.cancelled
        assert store.rows('a')[0]['state'] == 'pending'
    asyncio.run(scenario())


def test_finished_media_requires_query_started_after_ready(tmp_path, monkeypatch):
    async def scenario():
        starts, ready_at = [], []
        ticks = 0

        class Adapter:
            async def status(self, task):
                starts.append(time.monotonic())
                number = len(starts)
                if number == 1:
                    return False, 0, ''
                await asyncio.sleep(.12 if number == 2 else .01)
                return True, 1, f'fresh response {number}'

        async def tick(store):
            nonlocal ticks
            ticks += 1
            ready = ticks >= 3
            if ready and not ready_at:
                ready_at.append(time.monotonic())
            return {'phase': 'ended' if ready else 'playing', 'ready': ready}

        adapter = Adapter()
        store, task, browser, config = harness(tmp_path, monkeypatch, adapter, tick)
        assert await execute(task, adapter, browser, store, config) == 'fresh response 3'
        assert starts[1] < ready_at[0] <= starts[2]
        assert browser.paused
    asyncio.run(scenario())
