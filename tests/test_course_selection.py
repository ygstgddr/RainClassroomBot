import asyncio
import time
import tkinter as tk
import pytest
from core.courses import timestamp, date_text, course_state
from core.errors import AdaptationRequired
from core.store import Store
from task.scanner import scan
from task.scheduler import run_queue
from tests.test_scan_progress import setup, FakeAdapter
from tests.test_core import sample
from main import demo_data
from gui.interface import Interface


def test_dates_are_actual_school_time_and_unknown_is_not_current():
    assert timestamp(1787155200000) == 1787155200
    assert date_text(1787155200000) == '2026-08-20 00:00'
    for value in (None, '', False, 0, -1, 'invalid', float('nan'), float('inf')):
        assert timestamp(value) is None
    assert course_state(dict(start=100, end=200), 150) == '进行中'
    assert course_state(dict(start=100, end=200), 201) == '已结束'
    assert course_state(dict(start=100, end=200), 99) == '未开课'
    assert course_state(dict(start=200, end=100), 150) == '时间异常'
    assert course_state(dict(start=100), 150) == '时间未知'


def test_selection_persists_per_account_and_empty_never_means_all(tmp_path):
    store = Store(tmp_path)
    store.select_courses(['10', '11'], 'a')
    store.select_courses(['11'], 'b')
    assert Store(tmp_path).selected_courses('a') == ['10', '11']
    assert store.selected_courses('b') == ['11']
    store.merge_course('a', '10', [sample()])
    store.select_courses([], 'a')
    assert store.scoped_rows('a') == []
    assert len(store.rows('a')) == 1
    assert not store.scope_scanned('a')


def test_only_selected_course_is_requested_and_history_is_retained(tmp_path):
    store, config, logger = setup(tmp_path)
    store.select_courses(['10'], 'a')
    old = dict(sample(), key='a:11:1', classroom_id='11')
    store.merge_course('a', '11', [old])
    class Selected(FakeAdapter):
        async def scan_course(self, course):
            assert course['classroom_id'] == '10'
            return await super().scan_course(course)
    result = asyncio.run(scan(Selected(store), store, config, logger))
    assert result['total'] == 1
    assert store.get('course_count') == 2
    assert len(store.rows()) == 2
    assert len(store.scoped_rows()) == 1
    assert store.scope_scanned('a')
    store.select_courses(['11'], 'a')
    store.set('desired', 'run')
    asyncio.run(run_queue(Selected(store), None, store, config, logger))
    assert store.get('desired') == 'paused'
    assert not store.scope_scanned('a')


def test_no_selection_does_not_scan_chapters(tmp_path):
    store, config, logger = setup(tmp_path)
    store.select_courses([], 'a')
    class NoChapters(FakeAdapter):
        async def scan_course(self, course):
            raise AssertionError('must not scan')
    with pytest.raises(AdaptationRequired, match='勾选课程'):
        asyncio.run(scan(NoChapters(store), store, config, logger))
    assert not store.rows()


def test_catalog_loads_dates_without_scanning_and_keeps_failed_courses(tmp_path):
    store, config, logger = setup(tmp_path)
    store.set('desired', 'catalog')
    class Catalog(FakeAdapter):
        async def course_metadata(self, course):
            if course['classroom_id'] == '11':
                raise AdaptationRequired('时间接口暂不可用')
            return dict(course, start=1787155200, end=1801411199, date_error='')
        async def scan_course(self, course):
            raise AssertionError('catalog must not scan tasks')
    result = asyncio.run(scan(Catalog(store), store, config, logger, catalog_only=True))
    assert result['status'] == 'partial'
    assert result['total'] == 2
    assert len(store.catalog()) == 2
    assert store.catalog()[0]['start'] == 1787155200
    assert store.catalog()[1]['date_error'] == '时间接口暂不可用'
    assert store.selected_courses() == ['10', '11']
    assert not store.scope_scanned()
    assert not store.rows()


def test_metadata_whitelists_dates_and_does_not_persist_sign():
    from browser.site_adapter import SiteAdapter
    class Metadata(SiteAdapter):
        async def get(self, *args, **kwargs):
            return dict(class_start=1787155200000, class_end=1801411199000, course_sign='secret', create_time=1)
    result = asyncio.run(Metadata(None, {}).course_metadata(dict(classroom_id='10', name='课程')))
    assert result['start'] == 1787155200
    assert 'course_sign' not in result
    assert 'create_time' not in result


def test_scheduler_never_executes_cached_unselected_tasks(tmp_path, monkeypatch):
    store, config, logger = setup(tmp_path)
    store.set('account', {'id': 'a'})
    store.select_courses(['10'], 'a')
    store.set('scanned_account', 'a')
    store.set('scanned_courses', ['10'])
    store.set('desired', 'run')
    store.merge_course('a', '10', [sample()])
    store.merge_course('a', '11', [dict(sample(), key='a:11:1', classroom_id='11')])
    called = []
    async def execute(task, *args):
        called.append(task['classroom_id'])
        return 'test-server-evidence'
    class Browser:
        page = None
        async def goto(self, *args, **kwargs):
            pass
    browser = Browser()
    browser.page = browser
    monkeypatch.setattr('task.scheduler.execute', execute)
    asyncio.run(run_queue(FakeAdapter(store), browser, store, config, logger))
    assert called == ['10']
    assert next(t for t in store.rows() if t['classroom_id'] == '11')['state'] == 'pending'


def test_gui_selects_by_id_dates_and_blocks_changes_until_worker_stops(tmp_path):
    store = Store(tmp_path)
    demo_data(store)
    now = time.time()
    store.set('course_catalog:demo', [
        dict(classroom_id='1', name='同名课程', start=now-100, end=now+100),
        dict(classroom_id='2', name='同名课程', start=now-200, end=now-100),
        dict(classroom_id='3', name='时间未知')])
    root = tk.Tk()
    root.withdraw()
    try:
        ui = Interface(root, store)
        ui.select_group('current')
        assert store.selected_courses() == ['1']
        assert not store.scope_scanned()
        ui.toggle_course('2')
        assert store.selected_courses() == ['1', '2']
        store.set('worker_busy', True)
        store.set('desired', 'paused')
        ui.select_group('none')
        assert store.selected_courses() == ['1', '2']
        store.set('worker_busy', False)
        ui.select_group('none')
        ui.command('scan')
        assert store.get('desired') == 'paused'
        ui.refresh()
        assert not ui.tree.get_children()
        assert len(store.rows()) == 4
        ui.select_group('all')
        assert store.selected_courses() == ['1', '2', '3']
    finally:
        root.destroy()
