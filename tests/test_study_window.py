import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.courses import timestamp, window_reason
from core.store import Store
from task.scanner import scan
from task.scheduler import run_queue
from tests.test_core import sample
from tests.test_scan_progress import FakeAdapter, setup
from tests.test_web_bridge import ready_store
from gui.web_interface import WebInterface
from browser.site_adapter import SiteAdapter


def test_time_bounds_and_legacy_storage(tmp_path):
    start = timestamp('2026-11-01T00:00:00+08:00')
    bounds = {'start': start * 1000, 'end': start + 60}
    assert window_reason(bounds, now=start - 1) == '课程尚未开放'
    assert not window_reason(bounds, now=start)
    assert window_reason(bounds, now=start + 60) == '课程已截止'
    assert window_reason(bounds, {'deadline': start + 5}, start + 5) == '任务已截止'
    assert window_reason(bounds, {'available_from': start + 5}, start) == '任务尚未开放'
    store = Store(tmp_path)
    store.merge_course('a', '10', [sample()])
    with store.db() as db:
        db.execute('ALTER TABLE tasks DROP COLUMN available_from')
    reopened = Store(tmp_path)
    row, = reopened.rows('a')
    assert row['available_from'] is None and not window_reason(None, row)
    assert Store(tmp_path).rows('a') == [row]


def test_visible_cards_tasks_and_counts_follow_same_clock(tmp_path):
    store = ready_store(tmp_path)
    now = time.time()
    store.select_courses(['10', '11'])
    store.merge_course('a', '10', [sample(), dict(sample(leaf='2'), deadline=now-1),
                                     dict(sample(leaf='3'), available_from=now+300)])
    snapshot = WebInterface(store).snapshot()
    assert [c['id'] for c in snapshot['courses']] == ['10']
    assert [t['id'] for t in snapshot['tasks']] == ['a:10:1']
    assert snapshot['courses'][0]['task_count'] == 1
    assert snapshot['counts']['selected'] == 1


def test_all_closed_chapter_needs_no_schedule_and_keeps_existing_proof(tmp_path):
    store, _, _ = setup(tmp_path)
    old = dict(sample(), state='completed', progress=1, evidence='previous-server-proof')
    store.merge_course('a', '10', [old])
    adapter = SiteAdapter(None, {})
    adapter.account_id = 'a'
    adapter.context = AsyncMock(return_value={})
    adapter.get = AsyncMock(return_value={'course_chapter': [{'section_leaf_list': [
        {'id': 1, 'leaf_type': 0, 'score_deadline': (time.time()-30)*1000}]}]})
    adapter.schedule = AsyncMock(side_effect=AssertionError('expired chapter requested schedule'))
    rows = asyncio.run(adapter.scan_course({'classroom_id': '10', 'name': 'Course'}))
    store.merge_course('a', '10', rows)
    assert not adapter.schedule.called and not store.scoped_rows('a')
    assert store.rows('a')[0]['evidence'] == old['evidence']


@pytest.mark.parametrize('explicit', [False, True])
def test_old_selection_and_blocked_expired_task_do_not_stop_active_work(tmp_path, explicit):
    store, config, logger = setup(tmp_path)
    adapter = FakeAdapter(store)
    now = time.time()
    store.set('course_catalog:a', [{'classroom_id': '10', 'end': now - 1}, {'classroom_id': '11'}])
    store.merge_course('a', '10', [sample()])
    result = asyncio.run(scan(adapter, store, config, logger))
    assert result['tasks'] == 1 and result['failed'] == 0
    assert store.available_selection('a') == ['11']
    old, active = sorted(store.rows('a'), key=lambda row: row['classroom_id'])
    store.update(old['key'], attempts=config['max_task_attempts'], state='failed')
    store.set('blocked_task', old['key'])
    if explicit:
        # A rescan may already have removed the expired item from the chapter.
        store.merge_course('a', '10', [])
        store.set('task_scope', {'ids': [old['key'], active['key']], 'account': 'a', 'courses': ['10', '11']})
    adapter.status = AsyncMock(return_value=(True, 1, 'server-proof'))
    browser = SimpleNamespace(page=SimpleNamespace(goto=AsyncMock()), open_task=AsyncMock())
    store.set('desired', 'run')
    result = asyncio.run(run_queue(adapter, browser, store, config, logger))
    assert result['confirmed'] == 1 and result['remaining'] == 0
    assert adapter.status.await_args.args[0]['key'] == active['key']
    assert adapter.status.await_count == 1 and not browser.open_task.called
    assert store.get('blocked_task') is None
    assert [t['key'] for t in store.scoped_rows('a')] == [active['key']]


@pytest.mark.parametrize('course_expires', [True, False])
def test_deadline_during_server_confirmation_skips_without_replay(tmp_path, monkeypatch, course_expires):
    store, config, logger = setup(tmp_path)
    adapter = FakeAdapter(store)
    asyncio.run(scan(adapter, store, config, logger))
    first = sample()
    deadline = time.time() + .65
    if course_expires:
        store.set('course_catalog:a', [{'classroom_id': '10', 'end': deadline}, {'classroom_id': '11'}])
    else:
        first['deadline'] = deadline
    store.merge_course('a', '10', [first, dict(sample(leaf='3'), available_from=deadline + 300)])
    config['poll_seconds'] = .02
    confirmed_query = []

    async def status(task):
        if task['key'] != first['key']:
            return True, 1, 'server-proof'
        if confirmed_query:
            confirmed_query.append('waiting')
            await asyncio.Future()
        confirmed_query.append('preflight')
        return False, 0, ''

    adapter.status = status
    page = SimpleNamespace(is_closed=lambda: False, goto=AsyncMock())
    browser = SimpleNamespace(open_task=AsyncMock(return_value=page), pause_media=AsyncMock(), page=page)
    monkeypatch.setattr('task.executor.VideoHandler', Mock(return_value=SimpleNamespace(
        tick=AsyncMock(return_value={'ready': True}))))
    for _ in range(2):
        store.set('desired', 'run')
        asyncio.run(run_queue(adapter, browser, store, config, logger))
        assert store.get('desired') == 'idle'
    assert confirmed_query == ['preflight', 'waiting']
    assert browser.open_task.await_count == 1 and browser.pause_media.await_count >= 1
    rows = {row['key']: row for row in store.rows('a')}
    assert rows[first['key']]['attempts'] == 0 and not rows[first['key']]['evidence']
    assert rows['a:11:1']['evidence'] == 'server-proof'
    assert [t['key'] for t in store.scoped_rows('a')] == ['a:11:1']
    assert any(e['status'] == '跳过任务' and '截止' in e['reason'] for e in store.events())
