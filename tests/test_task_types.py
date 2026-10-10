import asyncio
from browser.site_adapter import SiteAdapter
from core.task_types import type_label, AUTOMATED_KINDS


def test_legacy_manual_rows_show_precise_types():
    assert type_label({'kind': 'manual', 'leaf_type': 7}) == 'PPT / 课件阅读'
    assert type_label({'kind': 'manual', 'leaf_type': 6}) == '作业提交'
    assert type_label({'kind': 'manual', 'leaf_type': 5}) == '考试'
    assert '99' in type_label({'kind': 'manual', 'leaf_type': 99})


def test_only_supported_reading_types_are_automated_and_unknown_progress_blocks_ppt():
    class Adapter(SiteAdapter):
        async def context(self, cid):
            return {}
        async def get(self, *args, **kwargs):
            return {'course_chapter': [{'section_leaf_list': [
                {'id': i+1, 'leaf_type': i, 'name': str(i)} for i in range(11)]}]}
        async def schedule(self, cid):
            return {str(i+1): ({'done': 1, 'total': 3} if i in (6, 7) else {'status': 0} if i in (5, 9) else 0)
                    for i in range(11)}
    adapter = Adapter(None, {})
    tasks = asyncio.run(adapter.scan_course({'classroom_id': '10', 'name': '测试'}))
    assert {t['kind'] for t in tasks if t['state'] == 'pending'} == AUTOMATED_KINDS
    assert next(t for t in tasks if t['leaf_type'] == 7)['progress'] == 1/3
    for typ in (5, 6, 9):
        task = next(t for t in tasks if t['leaf_type'] == typ)
        assert task['state'] == 'manual' and '作答' in task['note']
    async def missing(cid):
        return {}
    adapter.schedule = missing
    tasks = asyncio.run(adapter.scan_course({'classroom_id': '10', 'name': '测试'}))
    assert next(t for t in tasks if t['leaf_type'] == 7)['state'] == 'needs_adapter'
