"""长江站没有初始进度记录的回归；缺项始终不能成为完成证据。"""
import asyncio

import pytest

from browser.site_adapter import SiteAdapter, leaf_completion
from core.config import load_config
from core.errors import AdaptationRequired
from core.logging_setup import make_logger
from core.store import Store
from task.scheduler import run_queue
from tests.test_core import sample


class SparseAdapter(SiteAdapter):
    def __init__(self):
        super().__init__(None, {})
        self.values = {}
        self.types = [0, 3, 7, 4]

    async def account(self):
        self.account_id = 'a'
        return {'id': 'a'}

    async def courses(self):
        return []

    async def context(self, cid):
        return {}

    async def get(self, path, *args, **kwargs):
        if path.endswith('/chapter'):
            return {'course_chapter': [{'section_leaf_list': [
                {'id': i + 1, 'leaf_type': typ} for i, typ in enumerate(self.types)]}]}
        if path.endswith('/schedule'):
            return {'leaf_schedules': dict(self.values)}
        raise AssertionError(path)


def test_missing_video_article_are_pending_and_rescan_repairs_old_state(tmp_path):
    adapter = SparseAdapter()
    tasks = asyncio.run(adapter.scan_course({'classroom_id': '10', 'name': '测试课程'}))
    assert [t['state'] for t in tasks] == ['pending', 'pending', 'needs_adapter', 'manual']
    assert [t['progress'] for t in tasks[:2]] == [0, 0]
    assert all(not t['evidence'] for t in tasks)
    assert all('尚无进度记录' in t['note'] for t in tasks[:2])
    store = Store(tmp_path)
    store.merge_course('a', '10', [dict(tasks[0], state='needs_adapter')])
    store.merge_course('a', '10', tasks)
    assert next(t for t in store.rows('a') if t['leaf_id'] == '1')['state'] == 'pending'


@pytest.mark.parametrize('typ', [0, 3])
def test_missing_status_waits_for_explicit_fresh_completion(typ):
    async def scenario():
        adapter = SparseAdapter()
        task = dict(sample(), leaf_type=typ)
        assert await adapter.status(task) == (False, 0, '')
        adapter.values['1'] = .4
        assert await adapter.status(task) == (False, .4, '')
        adapter.values['1'] = 1
        done, progress, evidence = await adapter.status(task)
        assert done is True and progress == 1 and '"value": 1' in evidence
        # 不能缓存之前的完成响应，也不能把稍后消失的记录当成完成。
        adapter.values.clear()
        assert await adapter.status(task) == (False, 0, '')
    asyncio.run(scenario())


@pytest.mark.parametrize('value', [None, True, '0', '1', 100, {'done': 1, 'total': 1}])
@pytest.mark.parametrize('typ', [0, 3])
def test_explicit_unknown_values_still_block_instead_of_defaulting_to_zero(typ, value):
    async def scenario():
        adapter = SparseAdapter()
        adapter.types = [typ]
        adapter.values['1'] = value
        task = (await adapter.scan_course({'classroom_id': '10', 'name': '测试课程'}))[0]
        assert task['state'] == 'needs_adapter'
        with pytest.raises(AdaptationRequired):
            await adapter.status(task)
    asyncio.run(scenario())


@pytest.mark.parametrize('typ', [1, 2, 4, 5, 6, 7, 8, 9, 10, 99])
def test_missing_other_types_keep_unknown_semantics(typ):
    assert leaf_completion(typ, {}, '1') == (None, None)


@pytest.mark.parametrize('data', [None, {}, {'leaf_schedules': None}, {'leaf_schedules': []}])
def test_invalid_schedule_response_is_not_treated_as_empty_progress(data):
    class InvalidAdapter(SparseAdapter):
        async def get(self, *args, **kwargs):
            return data
    with pytest.raises(AdaptationRequired):
        asyncio.run(InvalidAdapter().schedule('10'))


def test_queue_and_workflow_report_no_execution_and_state_counts(tmp_path):
    from core.workflow import Workflow

    store = Store(tmp_path)
    store.set('desired', 'run')
    store.set('account', {'id': 'a'})
    store.select_courses(['10'], 'a')
    store.set('scanned_account', 'a')
    store.set('scanned_courses', ['10'])
    store.set('scan_progress', {'status': 'completed'})
    store.set('run_mode', 'resume')
    store.merge_course('a', '10', [dict(sample(), state='needs_adapter'),
                                  dict(sample(leaf='2'), state='manual', kind='discussion')])
    adapter = SparseAdapter()
    config = load_config()
    logger = make_logger(tmp_path, 'sparse')
    summary = asyncio.run(run_queue(adapter, None, store, config, logger))
    assert summary['processed'] == summary['confirmed'] == 0
    assert summary['remaining'] == 2
    assert '本轮未执行任务' in summary['message']
    assert '需要适配 1 项' in summary['message'] and '需手动完成 1 项' in summary['message']
    assert store.events()[0]['reason'] == summary['message']
    store.set('desired', 'run')
    asyncio.run(Workflow(adapter, None, store, config, logger).perform('run'))
    assert store.get('workflow')['message'] == summary['message']
